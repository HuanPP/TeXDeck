from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from . import agy, auth, chatgpt, claude_code
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
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{data}"},
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
        self.model = chatgpt_model()
        self._responses = chatgpt.ResponsesClient(self.model)
        self._on_usage = on_usage

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
        self.model = claude_model()

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
            content = claude_code.complete(messages, callback, "" if self.model == "default" else self.model)
        except claude_code.ClaudeCodeError as exc:
            raise LlmError(str(exc)) from exc
        if not content.strip():
            raise LlmError("Model returned empty content")
        return content


class AgyClient:
    """Same complete() contract as LlmClient, backed by the agy CLI in a sandbox.

    temperature is accepted and ignored: agy does not expose it.
    """

    def __init__(self, on_usage: UsageCallback | None = None) -> None:
        self._on_usage = on_usage
        self.model = agy_model()

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
            return agy.complete(messages, callback, "" if self.model == "default" else self.model)
        except agy.AgyError as exc:
            raise LlmError(str(exc)) from exc


# --------------------------------------------------------------------------
# Which model to use. The page picks one of three providers; the choice and
# any API key entered on the page live in settings.model_state_file, or with
# accounts in each account's own directory. Values from .env stay as defaults
# (the API key only without accounts: it is the server's, not each user's),
# and TEXDECK_LLM_PROVIDER other than "auto" pins the provider.
# --------------------------------------------------------------------------

PROVIDERS = ("chatgpt", "claude-code", "agy", "api")
_state_lock = threading.Lock()


def _state_path() -> Path:
    home = auth.user_dir()
    return home / "model-state.json" if home else settings.model_state_file


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # may hold an API key
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _set_choice(provider: str) -> None:
    with _state_lock:
        data = _read_state()
        data["provider"] = provider
        _write_state(data)


@dataclass(frozen=True, slots=True)
class ApiConfig:
    api_key: str
    base_url: str
    model: str
    saved: bool  # entered on the page (and removable there), not only from .env


def _server_api_key() -> str:
    return "" if auth.current_user.get() else settings.llm_api_key


def api_config() -> ApiConfig:
    saved = _read_state().get("api")
    saved = saved if isinstance(saved, dict) else {}
    return ApiConfig(
        api_key=str(saved.get("api_key") or _server_api_key()),
        base_url=str(saved.get("base_url") or settings.llm_base_url),
        model=str(saved.get("model") or settings.llm_model),
        saved=bool(saved),
    )


def save_api_config(api_key: str, base_url: str, model: str) -> None:
    """Store an API key from the page and switch to it; a blank key keeps the current one."""
    api_key, base_url, model = api_key.strip(), base_url.strip(), model.strip()
    if not api_key:
        api_key = api_config().api_key
    if not (api_key and base_url and model):
        raise LlmError("API key, base URL and model are all required")
    with _state_lock:
        data = _read_state()
        data["api"] = {"api_key": api_key, "base_url": base_url, "model": model}
        data["provider"] = "api"
        _write_state(data)


def _chosen_model(provider: str) -> str:
    models = _read_state().get("models")
    return str(models.get(provider) or "") if isinstance(models, dict) else ""


def chatgpt_model() -> str:
    return _chosen_model("chatgpt") or settings.chatgpt_model


def claude_model() -> str:
    return _chosen_model("claude-code") or settings.claude_code_model or "default"


def agy_model() -> str:
    return _chosen_model("agy") or settings.agy_model or "default"


def set_model(provider: str, model: str) -> None:
    """Remember which model a provider uses (the API key's model lives with the key)."""
    model = model.strip()
    if provider not in ("chatgpt", "claude-code", "agy"):
        raise LlmError(f"Cannot pick a model for {provider} here")
    if not model or len(model) > 100 or any(ch.isspace() for ch in model):
        raise LlmError("Invalid model name")
    with _state_lock:
        data = _read_state()
        models = data.get("models") if isinstance(data.get("models"), dict) else {}
        data["models"] = {**models, provider: model}
        _write_state(data)


def remove_api_config() -> None:
    with _state_lock:
        data = _read_state()
        data.pop("api", None)
        if data.get("provider") == "api" and not _server_api_key():
            data.pop("provider", None)
        _write_state(data)


def provider_locked() -> bool:
    return settings.llm_provider != "auto"


def _usable(provider: str) -> bool:
    if provider == "chatgpt":
        return chatgpt.load_credential() is not None
    if provider == "api":
        return bool(api_config().api_key)
    if provider == "agy":
        return not agy.availability() and agy.signed_in()
    return provider == "claude-code"  # checked when used; a logged-out CLI reports itself


def select_provider(provider: str) -> None:
    if provider_locked():
        raise LlmError(f"The model is fixed by TEXDECK_LLM_PROVIDER={settings.llm_provider}")
    if provider not in PROVIDERS:
        raise LlmError(f"Unknown provider: {provider}")
    if provider == "chatgpt" and not _usable("chatgpt"):
        raise LlmError("Sign in with ChatGPT first")
    if provider == "api" and not _usable("api"):
        raise LlmError("Enter an API key first")
    if provider == "claude-code":
        if claude_code.find_command() is None:
            raise LlmError("Claude Code is not installed on this computer")
        if not claude_code.auth_status(refresh=True)["logged_in"]:
            raise LlmError("Claude Code is not signed in; connect it first")
    if provider == "agy":
        reason = agy.availability()
        if reason:
            raise LlmError(reason)
        if not agy.signed_in():
            raise LlmError("agy is not signed in; connect it first")
    _set_choice(provider)


def active_provider() -> str:
    """Return "chatgpt", "claude-code", "agy" or "api".

    The page's choice wins while it is usable; otherwise a ChatGPT sign-in,
    otherwise the API key. Claude Code and agy are only ever used once chosen.
    """
    if provider_locked():
        return settings.llm_provider
    choice = _read_state().get("provider")
    if choice in PROVIDERS and _usable(choice):
        return choice
    return "chatgpt" if _usable("chatgpt") else "api"


def active_model() -> str:
    provider = active_provider()
    if provider == "chatgpt":
        return chatgpt_model()
    if provider == "claude-code":
        return claude_model()
    if provider == "agy":
        return agy_model()
    return api_config().model


def _key_hint(key: str) -> str:
    return f"\u2022\u2022\u2022\u2022{key[-4:]}" if len(key) >= 8 else ("\u2022\u2022\u2022\u2022" if key else "")


def model_status() -> dict[str, Any]:
    api = api_config()
    return {
        "provider": active_provider(),
        "model": active_model(),
        "locked": provider_locked(),
        "chatgpt": {**chatgpt.status(), "model": chatgpt_model()},
        "claude": {**claude_code.status(), "model": claude_model(), "models": claude_code.MODELS},
        "agy": {**agy.status(), "model": agy_model()},
        "api": {
            "configured": bool(api.api_key),
            "base_url": api.base_url,
            "model": api.model,
            "key_hint": _key_hint(api.api_key),
            "saved": api.saved,
        },
    }


# A finished sign-in switches to that provider (unless TEXDECK_LLM_PROVIDER pins one).
def _signed_in(provider: str) -> Callable[[], None]:
    return lambda: None if provider_locked() else _set_choice(provider)


chatgpt.on_signed_in = _signed_in("chatgpt")
claude_code.on_signed_in = _signed_in("claude-code")
agy.on_signed_in = _signed_in("agy")


def ensure_model_ready() -> None:
    """Fail fast, before a job starts, when the provider in use cannot run."""
    provider = active_provider()
    if provider == "chatgpt":
        if chatgpt.load_credential() is None:
            raise LlmError("ChatGPT is not signed in; connect it from the model button at the top")
    elif provider == "claude-code":
        if claude_code.find_command() is None:
            raise LlmError("Claude Code is not installed on this computer; pick another model at the top")
        if not claude_code.auth_status()["logged_in"]:
            raise LlmError("Claude Code is not signed in; connect it again from the model button at the top")
    elif provider == "agy":
        reason = agy.availability()
        if reason:
            raise LlmError(f"{reason}; pick another model at the top")
        if not agy.signed_in():
            raise LlmError("agy is not signed in; connect it again from the model button at the top")
    elif not api_config().api_key:
        raise LlmError("No model connected: click the model button at the top and connect one")


def create_client(
    on_usage: UsageCallback | None = None,
) -> LlmClient | ChatGptClient | ClaudeCodeClient | AgyClient:
    provider = active_provider()
    if provider == "agy":
        return AgyClient(on_usage=on_usage)
    if provider == "claude-code":
        if claude_code.find_command() is None:
            raise LlmError(
                "Claude Code is selected but the claude CLI was not found; "
                "install Claude Code or set TEXDECK_CLAUDE_CODE_COMMAND"
            )
        return ClaudeCodeClient(on_usage=on_usage)
    if provider == "chatgpt":
        if chatgpt.load_credential() is None:
            raise LlmError("TEXDECK_LLM_PROVIDER is chatgpt but nobody is signed in; sign in first")
        return ChatGptClient(on_usage=on_usage)
    api = api_config()
    if not api.api_key:
        raise LlmError("No model connected: click the model button at the top and connect one")
    client = openai.OpenAI(
        api_key=api.api_key,
        base_url=api.base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    return LlmClient(client, on_usage=on_usage, model=api.model)
