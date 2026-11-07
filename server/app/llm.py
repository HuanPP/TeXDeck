from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from . import chatgpt, claude_code
from .config import settings

ProgressCallback = Callable[[str, int], None]  # (stage description, percent 0-100)
UsageCallback = Callable[[int, int], None]  # (prompt_tokens, completion_tokens)


@dataclass(slots=True)
class UsageTally:
    """Per-job token accumulator; wire .add as an on_usage callback."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens


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
    def __init__(
        self,
        client: openai.OpenAI,
        on_usage: UsageCallback | None = None,
        model: str | None = None,
    ) -> None:
        # on_usage is per-instance and clients are created per job (create_client),
        # so no usage state leaks across jobs.
        self._client = client
        self._on_usage = on_usage
        self.model = model or settings.llm_model

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        on_usage: UsageCallback | None = None,
    ) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
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

        # Report usage as soon as the call returns: tokens are billed even when
        # the content below turns out to be empty. Some OpenAI-compatible
        # providers omit usage entirely — count the call with zero tokens then.
        callback = on_usage if on_usage is not None else self._on_usage
        if callback is not None:
            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            callback(prompt_tokens, completion_tokens)

        if not response.choices:
            raise LlmError("Model returned no choices")
        content = response.choices[0].message.content
        if not content or not str(content).strip():
            raise LlmError("Model returned empty content")
        return str(content)


class ChatGptClient:
    """Same complete() contract as LlmClient, backed by a ChatGPT sign-in.

    temperature is accepted and ignored: the reasoning models a ChatGPT plan
    serves reject it.
    """

    def __init__(self, on_usage: UsageCallback | None = None) -> None:
        self._responses = chatgpt.ResponsesClient(settings.chatgpt_model)
        self._on_usage = on_usage
        self.model = settings.chatgpt_model

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        on_usage: UsageCallback | None = None,
    ) -> str:
        del temperature
        callback = on_usage if on_usage is not None else self._on_usage
        try:
            content = self._responses.complete(messages, callback)
        except chatgpt.ChatGptError as exc:
            raise LlmError(str(exc)) from exc
        if not content.strip():
            raise LlmError("Model returned empty content")
        return content


class ClaudeCodeClient:
    """Same complete() contract as LlmClient, backed by the local Claude Code CLI.

    temperature is accepted and ignored: `claude -p` does not expose it.
    """

    def __init__(self, on_usage: UsageCallback | None = None) -> None:
        self._on_usage = on_usage
        self.model = settings.claude_code_model or "default"

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        on_usage: UsageCallback | None = None,
    ) -> str:
        del temperature
        callback = on_usage if on_usage is not None else self._on_usage
        try:
            content = claude_code.complete(messages, callback)
        except claude_code.ClaudeCodeError as exc:
            raise LlmError(str(exc)) from exc
        if not content.strip():
            raise LlmError("Model returned empty content")
        return content


def active_provider() -> str:
    """Return "chatgpt", "claude-code" or "api", resolving TEXDECK_LLM_PROVIDER=auto by sign-in state."""
    if settings.llm_provider == "auto":
        return "chatgpt" if chatgpt.load_credential() is not None else "api"
    return settings.llm_provider


def active_model() -> str:
    provider = active_provider()
    if provider == "chatgpt":
        return settings.chatgpt_model
    if provider == "claude-code":
        return settings.claude_code_model or "default"
    return settings.llm_model


def create_client(
    on_usage: UsageCallback | None = None,
) -> LlmClient | ChatGptClient | ClaudeCodeClient:
    if active_provider() == "claude-code":
        if claude_code.find_command() is None:
            raise LlmError(
                "TEXDECK_LLM_PROVIDER is claude-code but the claude CLI was not found; "
                "install Claude Code or set TEXDECK_CLAUDE_CODE_COMMAND"
            )
        return ClaudeCodeClient(on_usage=on_usage)
    if active_provider() == "chatgpt":
        if chatgpt.load_credential() is None:
            raise LlmError("TEXDECK_LLM_PROVIDER is chatgpt but nobody is signed in; sign in first")
        return ChatGptClient(on_usage=on_usage)
    if not settings.llm_api_key:
        raise LlmError(
            "No model configured: sign in with ChatGPT, or set TEXDECK_LLM_API_KEY in server/.env"
        )
    client = openai.OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    return LlmClient(client, on_usage=on_usage)
