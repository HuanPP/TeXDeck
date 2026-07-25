from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openai

from .config import settings

ProgressCallback = Callable[[str, int], None]  # (stage description, percent 0-100)


class LlmError(RuntimeError):
    """LLM call failed; message is user-facing English."""


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_block(image_path: Path) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        raise LlmError(f"Image file not found: {path}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{data}"},
    }


class LlmClient:
    def __init__(self, client: openai.OpenAI) -> None:
        self._client = client

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> str:
        try:
            response = self._client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                temperature=temperature,
                max_tokens=settings.llm_max_tokens,
            )
        except openai.AuthenticationError as exc:
            raise LlmError(f"Authentication failed: {exc}") from exc
        except openai.RateLimitError as exc:
            raise LlmError(f"Rate limited: {exc}") from exc
        except openai.APITimeoutError as exc:
            raise LlmError(f"Timed out: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise LlmError(f"Connection failed: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LlmError(f"Request rejected: {exc}") from exc
        except openai.OpenAIError as exc:
            raise LlmError(f"Request rejected: {exc}") from exc

        if not response.choices:
            raise LlmError("Model returned no choices")
        content = response.choices[0].message.content
        if not content or not str(content).strip():
            raise LlmError("Model returned empty content")
        return str(content)


def create_client() -> LlmClient:
    if not settings.llm_api_key:
        raise LlmError(
            "TEXDECK_LLM_API_KEY is not set; put it in server/.env"
        )
    client = openai.OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    return LlmClient(client)
