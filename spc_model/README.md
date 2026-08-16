# SPC Model Container

This folder is a standalone Python SPC service and Docker build context. It
contains the calculation API, data models, AI summary endpoint, and runtime
dependencies required to build the image without any parent repository files.

## Build

```bash
cd spc_model
docker build --tag spc-model:local .
```

## Run

```bash
docker run --rm --name spc-model --publish 8000:8000 spc-model:local
```

The container runs as a non-root user and provides a health check at:

```text
http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

The SPC calculation endpoints work without Ollama. Until Ollama is configured,
only `POST /spc/ai-summary` will return an upstream-service error.

When the dashboard and model containers share a Docker network, configure the
dashboard container with:

```env
SPC_API_BASE=http://spc-model:8000
```

Ollama can be connected later at runtime without rebuilding this image by
setting `OLLAMA_BASE_URL` and `OLLAMA_MODEL` on the model container.
