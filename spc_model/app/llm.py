from __future__ import annotations

import os

import httpx


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")


class LlmError(RuntimeError):
    pass


async def generate_with_ollama(prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=240.0) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 700,
                    },
                },
            )
    except httpx.HTTPError as exc:
        raise LlmError(f"ollama_request_failed: {exc}") from exc

    if response.status_code >= 400:
        raise LlmError(f"ollama_http_{response.status_code}: {response.text}")

    payload = response.json()
    text = str(payload.get("response") or "").strip()
    if not text:
        raise LlmError("ollama_empty_response")
    return text
