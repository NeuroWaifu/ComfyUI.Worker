# ComfyUI Worker for RunPod

A production-ready serverless worker implementation for running ComfyUI workflows on RunPod's infrastructure. This worker enables scalable AI image generation through ComfyUI's powerful node-based interface.

## Features

- **Serverless Architecture**: Designed for RunPod's serverless platform with automatic scaling
- **WebSocket Monitoring**: Real-time workflow execution tracking with automatic reconnection
- **S3 Storage Integration**: Optional S3-compatible storage for inputs and outputs
- **Multiple Input Types**: Supports base64, web URLs, and S3 references for input media
- **GPU Acceleration**: Full NVIDIA CUDA support for optimal performance
- **Error Handling**: Comprehensive error handling with detailed validation messages
- **ComfyUI Manager**: Pre-installed for easy custom node management

### Workflow Processing Pipeline

1. **Input Validation**: Validates workflow JSON and media inputs
2. **Media Upload**: Processes input media from various sources
3. **Workflow Execution**: Queues and monitors workflow via WebSocket
4. **Output Collection**: Retrieves generated images
5. **Storage**: Returns results as base64 or uploads to S3

## Installation

### Deploy to RunPod Serverless

1. **Use Pre-built Image**:
   ```
   ghcr.io/neurowaifu/neurowaifu.worker.comfyui:latest
   ```
   
   Or pull directly:
   ```bash
   docker pull ghcr.io/neurowaifu/neurowaifu.worker.comfyui:latest
   ```

2. **Create RunPod Serverless Endpoint**:
   - Go to [RunPod Serverless](https://www.runpod.io/console/serverless)
   - Click "New Endpoint"
   - Select your GPU type
   - Enter the Docker image URL
   - Configure environment variables (see Configuration section)
   - **Important**: Attach a Network Volume with your models

### Local Development

For local testing and development:

```bash
# Clone the repository
git clone https://github.com/NeuroWaifu/NeuroWaifu.Worker.ComfyUI.git
cd NeuroWaifu.Worker.ComfyUI

# Build and run locally
docker-compose up

# Or build manually
docker build -t comfyui-worker .
```

## Configuration

### Environment Variables

#### S3 Storage Configuration

Upload bucket configuration (required for S3 storage):
- `BUCKET_ENDPOINT_URL`: S3 endpoint URL for uploads
- `BUCKET_ACCESS_KEY_ID`: AWS access key with bucket write permissions
- `BUCKET_SECRET_ACCESS_KEY`: Corresponding AWS secret access key
- `BUCKET_NAME`: Bucket name for uploads
- `BUCKET_REGION`: AWS region (optional)

Download bucket configuration (optional, falls back to upload bucket):
- `BUCKET_DOWNLOAD_ENDPOINT_URL`: S3 endpoint URL for downloads
- `BUCKET_DOWNLOAD_ACCESS_KEY_ID`: Access key for download bucket
- `BUCKET_DOWNLOAD_SECRET_ACCESS_KEY`: Secret key for download bucket
- `BUCKET_DOWNLOAD_NAME`: Bucket name for downloads
- `BUCKET_DOWNLOAD_REGION`: AWS region (optional)

**Note**: If S3 variables are not configured, images are returned as base64-encoded strings in the API response.

#### Worker Configuration

- `REFRESH_WORKER`: Controls worker pod restart after each job (default: "false")
  - When `true`, ensures a clean state for subsequent jobs
- `WEBSOCKET_RECONNECT_ATTEMPTS`: Number of WebSocket reconnection attempts (default: 5)
- `WEBSOCKET_RECONNECT_DELAY_S`: Delay between reconnection attempts in seconds (default: 3)
- `WEBSOCKET_TRACE`: Enable low-level WebSocket frame tracing (default: "false")
  - Use `true` only when diagnosing connection issues

#### ComfyUI Configuration

- `COMFY_LOG_LEVEL`: ComfyUI internal logging verbosity (default: "DEBUG")
  - Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
  - Recommended: Use `INFO` for production, `DEBUG` for troubleshooting
- `SERVE_API_LOCALLY`: Enable local HTTP server for development (default: "false")
  - Simulates RunPod environment for testing

## API Reference

### Endpoints

The worker supports both synchronous and asynchronous execution:

- `/runsync` - Synchronous endpoint, waits for completion before responding
- `/run` - Asynchronous endpoint, returns immediately with job ID

### Request Format

```json
{
  "input": {
    "workflow": {
      // ComfyUI workflow JSON
    },
    "media": [
      {
        "name": "input.png",
        "type": "base64|web|s3",
        "media": "data:image/png;base64,..." // base64 or URL or S3 key
      }
    ]
  }
}
```

### Media Input Types

1. **Base64**: Direct base64 encoded images
   ```json
   {
     "name": "image.png",
     "type": "base64",
     "media": "data:image/png;base64,iVBORw0KGgoAAAANS..."
   }
   ```

2. **Web URL**: Public HTTP/HTTPS URLs
   ```json
   {
     "name": "image.jpg",
     "type": "web",
     "media": "https://example.com/image.jpg"
   }
   ```

3. **S3 Reference**: S3 bucket keys
   ```json
   {
     "name": "image.png",
     "type": "s3",
     "media": "path/to/image.png"
   }
   ```

### Response Format

#### Success Response

```json
{
  "media": [
    {
      "filename": "ComfyUI_00001_.png",
      "type": "s3_url|base64",
      "data": "https://...", // S3 presigned URL or base64 data
      "s3_file_key": "job-id/image-id.png" // only for S3
    }
  ],
  "errors": [] // optional warnings
}
```

#### Error Response

```json
{
  "error": "Error message",
  "details": ["Detailed error 1", "Detailed error 2"] // optional
}
```

## Model Management

Models are stored in `/runpod-volume` with the following structure:

```
/runpod-volume/
├── models/
│   ├── checkpoints/
│   ├── loras/
│   ├── vae/
│   ├── controlnet/
│   ├── upscale_models/
│   └── ...
```

Configure model paths in `src/extra_model_paths.yaml`.

## Customization

### Important Notes

This worker provides a **clean ComfyUI installation without any pre-installed models**. You must provide your own models using RunPod's Network Volume feature.

### Adding Models via Network Volume

This worker uses **Network Volume exclusively** for model management:

1. **Create Network Volume**:
   - Create a RunPod network volume in the same region as your endpoint
   - Recommended size: 50-100GB depending on your models
   - This is **required** - the worker won't function without models

2. **Required Directory Structure**:
   ```
   /runpod-volume/
   ├── models/
   │   ├── checkpoints/       # Stable Diffusion models (required)
   │   ├── clip/             # CLIP models
   │   ├── clip_vision/      # CLIP vision models
   │   ├── controlnet/       # ControlNet models
   │   ├── embeddings/       # Textual inversions
   │   ├── loras/           # LoRA models
   │   ├── upscale_models/  # ESRGAN models
   │   └── vae/             # VAE models
   ```

3. **Upload Your Models**:
   - Use RunPod's web interface to upload models to your Network Volume
   - Or use any S3-compatible tool if your volume supports it
   - Ensure models are placed in the correct subdirectories
   - Example: SDXL models go to `/models/checkpoints/`

4. **Attach Volume to Endpoint**: 
   - During endpoint creation, attach the network volume at `/runpod-volume`
   - Models will be automatically detected by ComfyUI at startup

### Adding Custom Nodes

To add custom nodes, you need to create a custom Docker image:

#### 1. Custom Dockerfile

```dockerfile
# Base on this worker image
FROM ghcr.io/neurowaifu/neurowaifu.worker.comfyui:latest

# Install custom nodes using the helper script
RUN comfy-node-install \
    comfyui-kjnodes \
    comfyui-ic-light \
    comfyui-advanced-controlnet
```

#### 2. Build and Push

```bash
# Build your custom image
docker build -t my-custom-comfyui-worker .

# Push to your registry
docker push ghcr.io/yourusername/my-custom-comfyui-worker:latest
```

### ComfyUI Manager

ComfyUI-Manager is pre-installed for easier node management. However, nodes installed via the Manager UI are **temporary** and will be lost when the worker restarts. For permanent custom nodes, use the Dockerfile approach above.

### Performance Optimizations

You can customize ComfyUI startup arguments via environment variables:

```bash
# For high VRAM GPUs (24GB+)
COMFY_ARGS="--highvram"

# For low VRAM GPUs (8-12GB)
COMFY_ARGS="--lowvram --use-split-cross-attention"

# For CPU-only execution (not recommended)
COMFY_ARGS="--cpu"
```

### Workflow Validation

Since this is a clean installation, ensure your workflows only use:
- Nodes from the base ComfyUI installation
- Custom nodes you've added via Dockerfile
- Models you've uploaded to the Network Volume

**Remember**: This worker provides maximum flexibility but requires you to manage all models and most customizations yourself via Network Volume and custom Docker images.

## Development

### Local Testing

```bash
# Start ComfyUI and worker locally
docker-compose up

# ComfyUI UI: http://localhost:8188
# Worker API: http://localhost:8000
```

### Running Tests

```python
# Use the test handler
python test_handler.py

# Or send test requests to the API
curl -X POST http://localhost:8000/runsync \
  -H "Content-Type: application/json" \
  -d @test_resources/example_request.json
```

## Deployment

### GitHub Actions Workflow

The included GitHub workflow (`.github/workflows/release.yml`) automatically:

1. Builds Docker images on push to dev branches
2. Creates versioned releases on tags
3. Pushes images to GitHub Container Registry

### Image Tags

- `latest`: Latest stable release
- `v1.0.0`: Specific version
- `dev`: Latest development build
- `dev-{sha}`: Specific development commit

## Troubleshooting

### Common Issues

1. **ComfyUI not reachable**: Check if ComfyUI started correctly on port 8188
2. **WebSocket disconnections**: Increase `WEBSOCKET_RECONNECT_ATTEMPTS`
3. **S3 upload failures**: Verify S3 credentials and bucket permissions
4. **Model not found**: Ensure models are in correct directories per `extra_model_paths.yaml`

### Debug Mode

Enable verbose logging:
```bash
# ComfyUI log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
export COMFY_LOG_LEVEL=DEBUG

# Enable WebSocket frame tracing
export WEBSOCKET_TRACE=true
```

## Security Considerations

- Never expose ComfyUI directly to the internet
- Use secure S3 credentials with minimal required permissions
- Validate all workflow inputs to prevent malicious payloads
- Consider network isolation for production deployments

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See the LICENSE file for details.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## Acknowledgments

This worker is based on the official [RunPod ComfyUI Worker](https://github.com/runpod-workers/worker-comfyui) implementation. We've modified it to support our specific use cases with S3 storage integration.

## Support

For issues and feature requests, please use the GitHub issue tracker.