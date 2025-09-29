import runpod
import magic
import boto3
from botocore.config import Config
from typing import Optional, Tuple, Dict, List, Any, Union
import json
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import websocket
import uuid
import tempfile
import socket
import traceback

# API check configuration
COMFY_API_AVAILABLE_INTERVAL_MS: int = 50
COMFY_API_AVAILABLE_MAX_RETRIES: int = 500

# Websocket reconnection configuration
WEBSOCKET_RECONNECT_ATTEMPTS: int = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S: int = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

# Enable verbose websocket trace logs if WEBSOCKET_TRACE=true
if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    websocket.enableTrace(True)

COMFY_HOST: str = "127.0.0.1:8188"
REFRESH_WORKER: bool = os.environ.get("REFRESH_WORKER", "false").lower() == "true"


def get_boto_client(
    endpoint_url: str, 
    access_key_id: str, 
    secret_access_key: str, 
    region: Optional[str] = None
) -> boto3.client:
    """
    Create and configure S3 client for bucket operations.

    Args:
        endpoint_url: The S3 endpoint URL.
        access_key_id: AWS access key ID for authentication.
        secret_access_key: AWS secret access key for authentication.
        region: Optional AWS region name.

    Returns:
        Configured boto3 S3 client.
    """
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}
        ),
        region_name=region,
    )


def get_boto_upload_client() -> Tuple[boto3.client, str]:
    """
    Get S3 client and bucket name for upload operations.

    Returns:
        Tuple containing the S3 client and bucket name.

    Raises:
        Exception: If any required environment variable is missing.
    """
    for var in [
        "BUCKET_ENDPOINT_URL",
        "BUCKET_ACCESS_KEY_ID", 
        "BUCKET_SECRET_ACCESS_KEY",
        "BUCKET_NAME"
    ]:
        value = os.environ.get(var)
        if not value:
            raise Exception(f"{var} is None")

    return get_boto_client(
        os.environ.get("BUCKET_ENDPOINT_URL",), 
        os.environ.get("BUCKET_ACCESS_KEY_ID",), 
        os.environ.get("BUCKET_SECRET_ACCESS_KEY"), 
        os.environ.get("BUCKET_REGION", None)
    ), os.environ.get("BUCKET_NAME")


def get_boto_download_client() -> Tuple[boto3.client, str]:
    """
    Get S3 client and bucket name for download operations.

    Returns:
        Tuple containing the S3 client and bucket name.
        Falls back to upload client if download-specific vars are not set.
    """
    for var in [
        "BUCKET_DOWNLOAD_ENDPOINT_URL",
        "BUCKET_DOWNLOAD_ACCESS_KEY_ID", 
        "BUCKET_DOWNLOAD_SECRET_ACCESS_KEY",
        "BUCKET_DOWNLOAD_NAME"
    ]:
        value = os.environ.get(var)
        if not value:
            return get_boto_upload_client()

    return get_boto_client(
        os.environ.get("BUCKET_DOWNLOAD_ENDPOINT_URL",), 
        os.environ.get("BUCKET_DOWNLOAD_ACCESS_KEY_ID",), 
        os.environ.get("BUCKET_DOWNLOAD_SECRET_ACCESS_KEY"), 
        os.environ.get("BUCKET_DOWNLOAD_REGION", None)
    ), os.environ.get("BUCKET_DOWNLOAD_NAME")


def boto_download_media(file_key: str) -> bytes:
    """
    Download media file from S3 bucket.

    Args:
        file_key: The S3 key of the file to download.

    Returns:
        The raw bytes of the downloaded file.

    Raises:
        Exception: If download fails or credentials are invalid.
    """
    client, bucket = get_boto_download_client()
    response = client.get_object(Bucket=bucket, Key=file_key)
    return response['Body'].read()


def boto_upload_media(job_id: str, media_location: str) -> Tuple[str, str]:
    """
    Upload media file to S3 bucket and return presigned URL.

    Args:
        job_id: The job ID to use as prefix in S3 key.
        media_location: Local file path of the media to upload.

    Returns:
        Tuple of (presigned_url, file_key) where:
        - presigned_url: Temporary URL for accessing the uploaded file.
        - file_key: The S3 key where the file was stored.

    Raises:
        Exception: If upload fails or file cannot be read.
    """
    client, bucket = get_boto_upload_client()

    image_name: str = str(uuid.uuid4())[:8]
    file_extension: str = os.path.splitext(media_location)[1]

    with open(media_location, "rb") as input_file:
        output: bytes = input_file.read()

    mime = magic.Magic(mime=True)
    mime_type: str = mime.from_buffer(output)

    file_key: str = f"{job_id}/{image_name}{file_extension}"
    client.put_object(
        Bucket=bucket,
        Key=file_key,
        Body=output,
        ContentType=mime_type,
    )

    presigned_url: str = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": file_key},
        ExpiresIn=604800,
    )

    return presigned_url, file_key


def _comfy_server_status() -> Dict[str, Any]:
    """
    Check ComfyUI server reachability via HTTP.

    Returns:
        Dictionary containing:
        - reachable: Boolean indicating if server is reachable.
        - status_code: HTTP status code if reachable.
        - error: Error message if not reachable.
    """
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(
    ws_url: str, 
    max_attempts: int, 
    delay_s: int, 
    initial_error: Exception
) -> websocket.WebSocket:
    """
    Attempts to reconnect to the WebSocket server after a disconnect.

    Args:
        ws_url: The WebSocket URL (including client_id).
        max_attempts: Maximum number of reconnection attempts.
        delay_s: Delay in seconds between attempts.
        initial_error: The error that triggered the reconnect attempt.

    Returns:
        The newly connected WebSocket object.

    Raises:
        websocket.WebSocketConnectionClosedException: If reconnection fails after all attempts.
    """
    print(
        f"worker-comfyui - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error: Exception = initial_error
    
    for attempt in range(max_attempts):
        srv_status: Dict[str, Any] = _comfy_server_status()
        if not srv_status["reachable"]:
            print(
                f"worker-comfyui - ComfyUI HTTP unreachable – aborting websocket reconnect: {srv_status.get('error', 'status '+str(srv_status.get('status_code')))}"
            )
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        print(
            f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (ComfyUI HTTP reachable, status {srv_status.get('status_code')})"
        )
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print(f"worker-comfyui - Websocket reconnected successfully.")
            return new_ws
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_reconnect_error = reconn_err
            print(
                f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}"
            )
            if attempt < max_attempts - 1:
                print(
                    f"worker-comfyui - Waiting {delay_s} seconds before next attempt..."
                )
                time.sleep(delay_s)
            else:
                print(f"worker-comfyui - Max reconnection attempts reached.")

    print("worker-comfyui - Failed to reconnect websocket after connection closed.")
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )


def validate_input(job_input: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Validates the input for the handler function.

    Args:
        job_input: The input data to validate.

    Returns:
        A tuple containing the validated data and an error message, if any.
        The structure is (validated_data, error_message).
    """
    if job_input is None:
        return None, "Please provide input"

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    workflow: Any = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    media_list: Optional[List[Dict[str, Any]]] = job_input.get("media")
    if media_list is not None:
        if not isinstance(media_list, list) or not all(
            "name" in media and "media" in media for media in media_list
        ):
            return (
                None,
                "'media' must be a list of objects with 'name' and 'media' keys",
            )

    return {"workflow": workflow, "media": media_list}, None


def check_server(url: str, retries: int = 500, delay: int = 50) -> bool:
    """
    Check if a server is reachable via HTTP GET request.

    Args:
        url: The URL to check.
        retries: The number of times to attempt connecting to the server.
        delay: The time in milliseconds to wait between retries.

    Returns:
        True if the server is reachable within the given number of retries, otherwise False.
    """
    print(f"worker-comfyui - Checking API server at {url}...")
    for i in range(retries):
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print(f"worker-comfyui - API is reachable")
                return True
        except requests.Timeout:
            pass
        except requests.RequestException as e:
            pass

        time.sleep(delay / 1000)

    print(
        f"worker-comfyui - Failed to connect to server at {url} after {retries} attempts."
    )
    return False


def upload_media(media_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Upload media files to ComfyUI.

    Args:
        media_list: List of media dictionaries, each containing:
            - name: Filename for the uploaded media.
            - media: The media data (base64, URL, or S3 key).
            - type: Media type ('base64', 'web', or 's3').

    Returns:
        Dictionary containing:
        - status: 'success' or 'error'.
        - message: Description of the result.
        - details: List of success messages or errors.
    """
    if not media_list:
        return {"status": "success", "message": "No media to upload", "details": []}

    responses: List[str] = []
    upload_errors: List[str] = []

    print(f"worker-comfyui - Uploading {len(media_list)} media(s)...")

    for media in media_list:
        try:
            name: str = media["name"]
            media_data: str = media["media"]
            media_type: str = media["type"]

            if media_type == "base64":
                if "," in media_data:
                    base64_data: str = media_data.split(",", 1)[1]
                else:
                    base64_data = media_data
                blob: bytes = base64.b64decode(base64_data)
            elif media_type == "web":
                response = requests.get(media_data, timeout=60)
                response.raise_for_status()
                blob = response.content
            elif media_type == "s3":
                blob = boto_download_media(media_data)
            else:
                raise Exception(f"Unknown media type {media_type}")
            
            mime = magic.Magic(mime=True)
            mime_type: str = mime.from_buffer(blob)
            files: Dict[str, Any] = {
                "image": (name, BytesIO(blob), mime_type),
                "overwrite": (None, "true"),
            }

            response = requests.post(
                f"http://{COMFY_HOST}/upload/image", files=files, timeout=30
            )
            response.raise_for_status()

            responses.append(f"Successfully uploaded {name}")
            print(f"worker-comfyui - Successfully uploaded {name}")

        except base64.binascii.Error as e:
            error_msg = f"Error decoding base64 for {media.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.Timeout:
            error_msg = f"Timeout uploading {media.get('name', 'unknown')}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.RequestException as e:
            error_msg = f"Error uploading {media.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error uploading {media.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)

    if upload_errors:
        print(f"worker-comfyui - media(s) upload finished with errors")
        return {
            "status": "error",
            "message": "Some media failed to upload",
            "details": upload_errors,
        }

    print(f"worker-comfyui - media(s) upload complete")
    return {
        "status": "success",
        "message": "All media uploaded successfully",
        "details": responses,
    }


def get_available_models() -> Dict[str, List[str]]:
    """
    Get list of available models from ComfyUI.

    Returns:
        Dictionary containing available models by type.
        Currently only returns 'checkpoints' key with list of checkpoint names.
        Returns empty dict if request fails.
    """
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info: Dict[str, Any] = response.json()

        available_models: Dict[str, List[str]] = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )

        return available_models
    except Exception as e:
        print(f"worker-comfyui - Warning: Could not fetch available models: {e}")
        return {}


def queue_workflow(workflow: Dict[str, Any], client_id: str) -> Dict[str, Any]:
    """
    Queue a workflow to be processed by ComfyUI.

    Args:
        workflow: A dictionary containing the workflow to be processed.
        client_id: The client ID for the websocket connection.

    Returns:
        The JSON response from ComfyUI after processing the workflow.

    Raises:
        ValueError: If the workflow validation fails with detailed error information.
    """
    payload: Dict[str, Any] = {"prompt": workflow, "client_id": client_id}
    data: bytes = json.dumps(payload).encode("utf-8")

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )

    if response.status_code == 400:
        print(f"worker-comfyui - ComfyUI returned 400. Response body: {response.text}")
        try:
            error_data: Dict[str, Any] = response.json()
            print(f"worker-comfyui - Parsed error data: {error_data}")

            error_message: str = "Workflow validation failed"
            error_details: List[str] = []

            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                    if error_info.get("type") == "prompt_outputs_failed_validation":
                        error_message = "Workflow validation failed"
                else:
                    error_message = str(error_info)

            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(
                                f"Node {node_id} ({error_type}): {error_msg}"
                            )
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")

            if error_data.get("type") == "prompt_outputs_failed_validation":
                error_message = error_data.get("message", "Workflow validation failed")
                available_models = get_available_models()
                if available_models.get("checkpoints"):
                    error_message += f"\n\nThis usually means a required model or parameter is not available."
                    error_message += f"\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                else:
                    error_message += "\n\nThis usually means a required model or parameter is not available."
                    error_message += "\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(error_message)

            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(
                    f"• {detail}" for detail in error_details
                )

                if any(
                    "not in list" in detail and "ckpt_name" in detail
                    for detail in error_details
                ):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += f"\n\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                    else:
                        detailed_message += "\n\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(detailed_message)
            else:
                raise ValueError(f"{error_message}. Raw response: {response.text}")

        except (json.JSONDecodeError, KeyError) as e:
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            )

    response.raise_for_status()
    return response.json()


def get_history(prompt_id: str) -> Dict[str, Any]:
    """
    Retrieve the history of a given prompt using its ID.

    Args:
        prompt_id: The ID of the prompt whose history is to be retrieved.

    Returns:
        The history of the prompt, containing all the processing steps and results.
    """
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_image_data(filename: str, subfolder: str, image_type: str) -> Optional[bytes]:
    """
    Fetch image bytes from the ComfyUI /view endpoint.

    Args:
        filename: The filename of the image.
        subfolder: The subfolder where the image is stored.
        image_type: The type of the image (e.g., 'output').

    Returns:
        The raw image data, or None if an error occurs.
    """
    print(
        f"worker-comfyui - Fetching image data: type={image_type}, subfolder={subfolder}, filename={filename}"
    )
    data: Dict[str, str] = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url_values: str = urllib.parse.urlencode(data)
    try:
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
        response.raise_for_status()
        print(f"worker-comfyui - Successfully fetched image data for {filename}")
        return response.content
    except requests.Timeout:
        print(f"worker-comfyui - Timeout fetching image data for {filename}")
        return None
    except requests.RequestException as e:
        print(f"worker-comfyui - Error fetching image data for {filename}: {e}")
        return None
    except Exception as e:
        print(f"worker-comfyui - Unexpected error fetching image data for {filename}: {e}")
        return None


def handler(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handles a job using ComfyUI via websockets for status and image retrieval.

    Args:
        job: A dictionary containing job details and input parameters.

    Returns:
        A dictionary containing either an error message or a success status with generated media.
    """
    job_input: Any = job["input"]
    job_id: str = job["id"]

    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    workflow: Dict[str, Any] = validated_data["workflow"]
    input_media: Optional[List[Dict[str, Any]]] = validated_data.get("media")

    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {
            "error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."
        }

    if input_media:
        upload_result = upload_media(input_media)
        if upload_result["status"] == "error":
            return {
                "error": "Failed to upload one or more input media",
                "details": upload_result["details"],
            }

    ws: Optional[websocket.WebSocket] = None
    client_id: str = str(uuid.uuid4())
    prompt_id: Optional[str] = None
    output_data: List[Dict[str, Any]] = []
    errors: List[str] = []

    try:
        ws_url: str = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print(f"worker-comfyui - Websocket connected")

        try:
            queued_workflow = queue_workflow(workflow, client_id)
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(
                    f"Missing 'prompt_id' in queue response: {queued_workflow}"
                )
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
        except requests.RequestException as e:
            print(f"worker-comfyui - Error queuing workflow: {e}")
            raise ValueError(f"Error queuing workflow: {e}")
        except Exception as e:
            print(f"worker-comfyui - Unexpected error queuing workflow: {e}")
            if isinstance(e, ValueError):
                raise e
            else:
                raise ValueError(f"Unexpected error queuing workflow: {e}")

        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done: bool = False
        while True:
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message: Dict[str, Any] = json.loads(out)
                    if message.get("type") == "status":
                        status_data = message.get("data", {}).get("status", {})
                        print(
                            f"worker-comfyui - Status update: {status_data.get('exec_info', {}).get('queue_remaining', 'N/A')} items remaining in queue"
                        )
                    elif message.get("type") == "executing":
                        data = message.get("data", {})
                        if (
                            data.get("node") is None
                            and data.get("prompt_id") == prompt_id
                        ):
                            print(
                                f"worker-comfyui - Execution finished for prompt {prompt_id}"
                            )
                            execution_done = True
                            break
                    elif message.get("type") == "execution_error":
                        data = message.get("data", {})
                        if data.get("prompt_id") == prompt_id:
                            error_details = f"Node Type: {data.get('node_type')}, Node ID: {data.get('node_id')}, Message: {data.get('exception_message')}"
                            print(f"worker-comfyui - Execution error received: {error_details}")
                            errors.append(f"Workflow execution error: {error_details}")
                            break
                else:
                    continue
            except websocket.WebSocketTimeoutException:
                print(f"worker-comfyui - Websocket receive timed out. Still waiting...")
                continue
            except websocket.WebSocketConnectionClosedException as closed_err:
                try:
                    ws = _attempt_websocket_reconnect(
                        ws_url,
                        WEBSOCKET_RECONNECT_ATTEMPTS,
                        WEBSOCKET_RECONNECT_DELAY_S,
                        closed_err,
                    )
                    print("worker-comfyui - Resuming message listening after successful reconnect.")
                    continue
                except websocket.WebSocketConnectionClosedException as reconn_failed_err:
                    raise reconn_failed_err

            except json.JSONDecodeError:
                print(f"worker-comfyui - Received invalid JSON message via websocket.")

        if not execution_done and not errors:
            raise ValueError(
                "Workflow monitoring loop exited without confirmation of completion or error."
            )

        print(f"worker-comfyui - Fetching history for prompt {prompt_id}...")
        history = get_history(prompt_id)

        if prompt_id not in history:
            error_msg = f"Prompt ID {prompt_id} not found in history after execution."
            print(f"worker-comfyui - {error_msg}")
            if not errors:
                return {"error": error_msg}
            else:
                errors.append(error_msg)
                return {
                    "error": "Job processing failed, prompt ID not found in history.",
                    "details": errors,
                }

        prompt_history: Dict[str, Any] = history.get(prompt_id, {})
        outputs: Dict[str, Any] = prompt_history.get("outputs", {})

        if not outputs:
            warning_msg = f"No outputs found in history for prompt {prompt_id}."
            print(f"worker-comfyui - {warning_msg}")
            if not errors:
                errors.append(warning_msg)

        print(f"worker-comfyui - Processing {len(outputs)} output nodes...")
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                print(
                    f"worker-comfyui - Node {node_id} contains {len(node_output['images'])} image(s)"
                )
                for image_info in node_output["images"]:
                    filename = image_info.get("filename")
                    subfolder = image_info.get("subfolder", "")
                    img_type = image_info.get("type")

                    if img_type == "temp":
                        print(
                            f"worker-comfyui - Skipping image {filename} because type is 'temp'"
                        )
                        continue

                    if not filename:
                        warn_msg = f"Skipping image in node {node_id} due to missing filename: {image_info}"
                        print(f"worker-comfyui - {warn_msg}")
                        errors.append(warn_msg)
                        continue

                    image_bytes = get_image_data(filename, subfolder, img_type)

                    if image_bytes:
                        file_extension = os.path.splitext(filename)[1] or ".bin"

                        if os.environ.get("BUCKET_ENDPOINT_URL"):
                            try:
                                with tempfile.NamedTemporaryFile(
                                    suffix=file_extension, delete=False
                                ) as temp_file:
                                    temp_file.write(image_bytes)
                                    temp_file_path = temp_file.name
                                print(
                                    f"worker-comfyui - Wrote image bytes to temporary file: {temp_file_path}"
                                )

                                print(f"worker-comfyui - Uploading {filename} to S3...")        
                                s3_url, file_key = boto_upload_media(job_id, temp_file_path)

                                os.remove(temp_file_path)
                                print(f"worker-comfyui - Uploaded {filename} to S3: {s3_url}")
                                output_data.append(
                                    {
                                        "filename": filename,
                                        "type": "s3_url",
                                        "data": s3_url,
                                        "s3_file_key": file_key,
                                    }
                                )
                            except Exception as e:
                                error_msg = f"Error uploading {filename} to S3: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                                if "temp_file_path" in locals() and os.path.exists(temp_file_path):
                                    try:
                                        os.remove(temp_file_path)
                                    except OSError as rm_err:
                                        print(f"worker-comfyui - Error removing temp file {temp_file_path}: {rm_err}")
                        else:
                            try:
                                base64_image = base64.b64encode(image_bytes).decode("utf-8")
                                output_data.append(
                                    {
                                        "filename": filename,
                                        "type": "base64",
                                        "data": base64_image,
                                    }
                                )
                                print(f"worker-comfyui - Encoded {filename} as base64")
                            except Exception as e:
                                error_msg = f"Error encoding {filename} to base64: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                    else:
                        error_msg = f"Failed to fetch image data for {filename} from /view endpoint."
                        errors.append(error_msg)

            other_keys = [k for k in node_output.keys() if k != "images"]
            if other_keys:
                warn_msg = f"Node {node_id} produced unhandled output keys: {other_keys}."
                print(f"worker-comfyui - WARNING: {warn_msg}")
                print(
                    f"worker-comfyui - --> If this output is useful, please consider opening an issue on GitHub to discuss adding support."
                )

    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {e}"}
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {e}"}
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {"error": str(e)}
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            print(f"worker-comfyui - Closing websocket connection.")
            ws.close()

    final_result: Dict[str, Any] = {}

    if output_data:
        final_result["media"] = output_data

    if errors:
        final_result["errors"] = errors
        print(f"worker-comfyui - Job completed with errors/warnings: {errors}")

    if not output_data and errors:
        print(f"worker-comfyui - Job failed with no output media.")
        return {
            "error": "Job processing failed",
            "details": errors,
        }
    elif not output_data and not errors:
        print(f"worker-comfyui - Job completed successfully, but the workflow produced no media.")
        final_result["status"] = "success_no_media"
        final_result["media"] = []

    print(f"worker-comfyui - Job completed. Returning {len(output_data)} media.")
    return final_result


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})