"""Sign in with ChatGPT: OpenAI's token-sharing OAuth flow plus a Responses client.

The flow follows OpenAI's agent token-sharing protocol as implemented in
opencode (MIT, packages/core/src/plugin/provider/chatgpt.ts): the first sign-in
uses the shared "dynamic_agent_client" id and OpenAI registers a client owned by
the user, returning its id on the callback; later sign-ins reuse that id. The
resulting access token is a normal bearer token for https://api.openai.com/v1,
billed against the user's ChatGPT plan.

Everything here is for one local user on one machine. Tokens are stored in a
single JSON file (mode 0600) and never leave the process except as the bearer
header on calls to OpenAI.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import zstandard

from . import auth
from .config import settings

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
REGISTRATION_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "TeXDeck"
TOKEN_SHARING_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {TOKEN_SHARING_SCOPE}"
USER_AGENT = "TeXDeck/0.6.1"
LOGIN_TIMEOUT_SECONDS = 600
# Error codes OpenAI returns when the plan itself refuses the call; retrying cannot help.
NON_RETRYABLE_CODES = (
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_user_not_eligible",
    "subscription_sharing_unsupported_capability",
    "subscription_sharing_route_not_supported",
    "subscription_sharing_invalid_user",
    "chatpass_v2_scope_not_authorized",
    "chatpass_v2_invalid_authorization_context",
)


class ChatGptError(RuntimeError):
    """Sign-in or request failure; message is user-facing English."""


# --------------------------------------------------------------------------
# Credential file
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Credential:
    access: str
    refresh: str
    expires: float  # epoch seconds
    client_id: str
    email: str = ""
    scopes: list[str] = field(default_factory=list)


_file_lock = threading.Lock()


def _auth_path() -> Path:
    home = auth.user_dir()
    return home / "chatgpt-auth.json" if home else Path(settings.chatgpt_auth_file)


def _read_store() -> dict[str, Any]:
    path = _auth_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(data: dict[str, Any]) -> None:
    path = _auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_credential() -> Credential | None:
    raw = _read_store().get("credential")
    if not isinstance(raw, dict):
        return None
    try:
        return Credential(**raw)
    except TypeError:
        return None


def save_credential(cred: Credential) -> None:
    with _file_lock:
        data = _read_store()
        data["credential"] = asdict(cred)
        # Reauthorization must present the client OpenAI registered for this user.
        data["client_id"] = cred.client_id
        _write_store(data)


def sign_out() -> None:
    """Forget tokens; keep host id and registered client id for the next sign-in."""
    with _file_lock:
        data = _read_store()
        if data.pop("credential", None) is not None:
            _write_store(data)


def _host_id() -> str:
    with _file_lock:
        data = _read_store()
        host = data.get("host_id")
        if isinstance(host, str) and host:
            return host
        host = f"urn:uuid:{uuid.uuid4()}"
        data["host_id"] = host
        _write_store(data)
        return host


def _saved_client_id() -> str | None:
    value = _read_store().get("client_id")
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------
# OAuth helpers
# --------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(
    *,
    redirect: str,
    challenge: str,
    state: str,
    nonce: str,
    client_id: str | None,
    host_id: str,
) -> str:
    params: dict[str, str] = {"client_id": client_id or REGISTRATION_CLIENT_ID}
    if not client_id:
        params["agent_name_hint"] = AGENT_NAME
    params.update(
        {
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise ChatGptError("ChatGPT sign-in returned a malformed ID token.") from exc
    if not isinstance(claims, dict):
        raise ChatGptError("ChatGPT sign-in returned a malformed ID token.")
    return claims


def check_id_token(token: str, client_id: str, nonce: str) -> dict[str, Any]:
    """Validate the ID token's claims.

    The token comes straight from the token endpoint over TLS, which OpenID
    Connect Core 3.1.3.7 accepts in place of signature verification; the claim
    checks below bind it to this client and this sign-in attempt.
    """
    claims = _jwt_claims(token)
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if claims.get("iss") != ISSUER:
        raise ChatGptError("ChatGPT sign-in returned an ID token from an unexpected issuer.")
    if client_id not in audiences:
        raise ChatGptError("ChatGPT sign-in returned an ID token for a different client.")
    if claims.get("nonce") != nonce:
        raise ChatGptError("ChatGPT sign-in returned an ID token for a different attempt.")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time() - 60:
        raise ChatGptError("ChatGPT sign-in returned an expired ID token.")
    if not str(claims.get("sub") or "").strip():
        raise ChatGptError("ChatGPT sign-in returned an ID token without a subject.")
    return claims


def _token_request(form: dict[str, str], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=30, transport=transport) as http:
            res = http.post(TOKEN_URL, data=form, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise ChatGptError(f"Could not reach OpenAI sign-in: {exc}") from exc
    if res.status_code != 200:
        raise ChatGptError(f"OpenAI sign-in rejected the request (HTTP {res.status_code}): {res.text[:300]}")
    try:
        data = res.json()
    except ValueError as exc:
        raise ChatGptError("OpenAI sign-in returned a non-JSON response.") from exc
    if not data.get("access_token") or not data.get("refresh_token"):
        raise ChatGptError("OpenAI sign-in response is missing tokens.")
    return data


def _credential_from(tokens: dict[str, Any], client_id: str, email: str, scopes: list[str]) -> Credential:
    scope = str(tokens.get("scope") or "")
    return Credential(
        access=tokens["access_token"],
        refresh=tokens["refresh_token"],
        expires=time.time() + float(tokens.get("expires_in") or 3600),
        client_id=client_id,
        email=email,
        scopes=scope.split() if scope else scopes,
    )


_refresh_lock = threading.Lock()


def refresh_credential(cred: Credential, transport: httpx.BaseTransport | None = None) -> Credential:
    tokens = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": cred.client_id,
            "refresh_token": cred.refresh,
            "resource": RESOURCE,
        },
        transport,
    )
    fresh = _credential_from(tokens, cred.client_id, cred.email, cred.scopes)
    save_credential(fresh)
    return fresh


def valid_credential(*, force_refresh: bool = False) -> Credential:
    """Return a credential good for at least a minute, refreshing if needed."""
    with _refresh_lock:
        cred = load_credential()
        if cred is None:
            raise ChatGptError("Not signed in with ChatGPT.")
        if force_refresh or cred.expires <= time.time() + 60:
            try:
                cred = refresh_credential(cred)
            except ChatGptError as exc:
                raise ChatGptError(f"ChatGPT session expired; sign in again. ({exc})") from exc
        return cred


# --------------------------------------------------------------------------
# Login flow: loopback listener plus paste-back fallback
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingLogin:
    verifier: str
    state: str
    nonce: str
    redirect: str
    client_id: str | None
    started: float
    user: str | None = None  # account signing in; the loopback callback finishes as them
    server: HTTPServer | None = None
    error: str = ""


_login_lock = threading.Lock()
_pending_by_user: dict[str | None, _PendingLogin] = {}  # one attempt per account


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><meta charset='utf-8'><title>TeXDeck</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(body)}</p></body>"
    ).encode("utf-8")


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib hook name)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/auth/callback":
            self.send_error(404)
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        with _login_lock:
            owner = next((p.user for p in _pending_by_user.values() if p.state == params.get("state")), None)
        try:
            auth.run_as(owner, complete_login, params)
        except ChatGptError as exc:
            self._reply(400, _page("Sign-in failed", str(exc)))
        else:
            self._reply(200, _page("Signed in to TeXDeck", "You can close this tab and return to TeXDeck."))

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


def _stop_server(pending: _PendingLogin | None) -> None:
    if pending is not None and pending.server is not None:
        server, pending.server = pending.server, None
        threading.Thread(target=server.shutdown, daemon=True).start()


def start_login(*, listen: bool = True) -> str:
    """Begin a sign-in attempt and return the URL to open in the browser.

    A loopback listener on 127.0.0.1 catches the redirect when the browser runs
    on this machine. When it does not (Docker, a remote host), the user pastes
    the URL the browser was sent to into complete_login_from_url instead.
    """
    verifier, challenge = generate_pkce()
    state = _b64url(secrets.token_bytes(32))
    nonce = _b64url(secrets.token_bytes(32))
    server: HTTPServer | None = None
    port = 1455
    if listen:
        try:
            server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
            port = server.server_address[1]
        except OSError:
            server = None
    redirect = f"http://127.0.0.1:{port}/auth/callback"
    client_id = _saved_client_id()
    pending = _PendingLogin(
        verifier=verifier,
        state=state,
        nonce=nonce,
        redirect=redirect,
        client_id=client_id,
        started=time.time(),
        user=auth.current_user.get(),
        server=server,
    )
    with _login_lock:
        _stop_server(_pending_by_user.get(pending.user))
        _pending_by_user[pending.user] = pending
    if server is not None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, _expire, args=(pending,))
        timer.daemon = True
        timer.start()
    return build_authorize_url(
        redirect=redirect,
        challenge=challenge,
        state=state,
        nonce=nonce,
        client_id=client_id,
        host_id=_host_id(),
    )


def _expire(pending: _PendingLogin) -> None:
    with _login_lock:
        if _pending_by_user.get(pending.user) is pending:
            _stop_server(pending)
            del _pending_by_user[pending.user]


# Called after a successful sign-in; app.llm uses it to switch to ChatGPT.
on_signed_in: Callable[[], None] | None = None


def complete_login(
    params: dict[str, str],
    transport: httpx.BaseTransport | None = None,
) -> Credential:
    """Finish the pending sign-in from the callback's query parameters."""
    user = auth.current_user.get()
    with _login_lock:
        pending = _pending_by_user.get(user)
        if pending is None:
            raise ChatGptError("No ChatGPT sign-in is in progress; start again.")
        if time.time() - pending.started > LOGIN_TIMEOUT_SECONDS:
            _stop_server(pending)
            del _pending_by_user[user]
            raise ChatGptError("The sign-in attempt timed out; start again.")
        if params.get("state") != pending.state:
            raise ChatGptError("This callback belongs to a different sign-in attempt.")
        # One shot: whatever happens next, this attempt is consumed.
        del _pending_by_user[user]

    def fail(message: str) -> ChatGptError:
        pending.error = message
        _stop_server(pending)
        return ChatGptError(message)

    error = params.get("error_description") or params.get("error")
    if error:
        raise fail(f"OpenAI refused the sign-in: {error}")
    code = params.get("code")
    if not code:
        raise fail("The callback has no authorization code.")
    returned = params.get("client_id")
    if pending.client_id and returned and returned != pending.client_id:
        raise fail("ChatGPT returned a different client than this connection; sign in again.")
    client_id = pending.client_id or returned
    if not client_id:
        raise fail("ChatGPT sign-in did not return a client ID; sign in again.")

    try:
        tokens = _token_request(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": pending.verifier,
                "redirect_uri": pending.redirect,
                "resource": RESOURCE,
            },
            transport,
        )
        if TOKEN_SHARING_SCOPE not in str(tokens.get("scope") or "").split():
            raise ChatGptError(
                "ChatGPT sign-in finished without token sharing. Sign in again and allow "
                "token sharing, or use an API key instead."
            )
        id_token = tokens.get("id_token")
        if not id_token:
            raise ChatGptError("ChatGPT sign-in did not return an ID token.")
        claims = check_id_token(id_token, client_id, pending.nonce)
    except ChatGptError as exc:
        raise fail(str(exc)) from exc

    cred = _credential_from(tokens, client_id, str(claims.get("email") or ""), [])
    save_credential(cred)
    _stop_server(pending)
    if on_signed_in is not None:
        on_signed_in()
    return cred


def complete_login_from_url(url: str, transport: httpx.BaseTransport | None = None) -> Credential:
    parsed = urllib.parse.urlsplit(url.strip())
    params = dict(urllib.parse.parse_qsl(parsed.query))
    if not params:
        raise ChatGptError("That URL has no sign-in parameters; copy the full address from the browser.")
    return complete_login(params, transport)


def status() -> dict[str, Any]:
    cred = load_credential()
    with _login_lock:
        pending = _pending_by_user.get(auth.current_user.get())
    return {
        "signed_in": cred is not None,
        "email": cred.email if cred else "",
        "pending": pending is not None,
    }


# --------------------------------------------------------------------------
# Responses API client
# --------------------------------------------------------------------------


def _text_part(text: str, role: str) -> dict[str, str]:
    return {"type": "output_text" if role == "assistant" else "input_text", "text": text}


def to_responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert chat-completions messages to (instructions, input items)."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if role == "system":
            instructions.append(content if isinstance(content, str) else "\n".join(
                part.get("text", "") for part in content if part.get("type") == "text"
            ))
            continue
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            parts.append(_text_part(content, role))
        else:
            for part in content:
                kind = part.get("type")
                if kind == "text":
                    parts.append(_text_part(part["text"], role))
                elif kind == "image_url":
                    parts.append({"type": "input_image", "image_url": part["image_url"]["url"]})
                else:
                    raise ChatGptError(f"Unsupported message part type: {kind}")
        items.append({"role": role, "content": parts})
    return "\n\n".join(instructions), items


UsageSink = Callable[[int, int], None]


_models_cache: tuple[str, float, list[dict[str, str]]] | None = None


def list_models(transport: httpx.BaseTransport | None = None) -> list[dict[str, str]]:
    """Models this sign-in lists and that read images, in the server's order.

    A sign-in token gets the Codex-style catalogue, not the platform one:
    {"models": [{"slug", "display_name", "visibility", "priority",
    "input_modalities", ...}]}. Hidden entries ("hide") are internal. Cached
    for 10 minutes per token.
    """
    global _models_cache
    cred = valid_credential()
    if _models_cache and _models_cache[0] == cred.access and time.time() - _models_cache[1] < 600:
        return _models_cache[2]
    headers = {"Authorization": f"Bearer {cred.access}", "User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=httpx.Timeout(30), transport=transport) as http:
            res = http.get(f"{RESOURCE}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise ChatGptError(f"Could not load the model list: {exc}") from exc
    if res.status_code != 200:
        raise ChatGptError(f"Could not load the model list (HTTP {res.status_code})")
    entries = [
        item for item in res.json().get("models") or []
        if isinstance(item, dict) and item.get("slug") and item.get("visibility") == "list"
        and "image" in (item.get("input_modalities") or [])  # slides are built from page images
    ]
    entries.sort(key=lambda item: int(item.get("priority") or 0))
    models = [{"value": str(m["slug"]), "label": str(m.get("display_name") or m["slug"])} for m in entries]
    _models_cache = (cred.access, time.time(), models)
    return models


class ResponsesClient:
    """Streams one Responses call per complete(); returns the output text."""

    def __init__(self, model: str, transport: httpx.BaseTransport | None = None) -> None:
        self.model = model
        self._transport = transport
        # ChatGPT derives prompt-cache affinity from session-id; one per job.
        self._session = str(uuid.uuid4())
        self._compressor = zstandard.ZstdCompressor(level=3)

    def _body(self, messages: list[dict[str, Any]]) -> bytes:
        instructions, items = to_responses_input(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "stream": True,
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if settings.chatgpt_reasoning_effort:
            body["reasoning"] = {"effort": settings.chatgpt_reasoning_effort}
        # Sign-in tokens reject uncompressed bodies over ~1 MiB; page images exceed that.
        return self._compressor.compress(json.dumps(body).encode("utf-8"))

    def complete(self, messages: list[dict[str, Any]], on_usage: UsageSink | None) -> str:
        body = self._body(messages)
        cred = valid_credential()
        attempts = 0
        refreshed = False
        while True:
            attempts += 1
            try:
                return self._stream(body, cred, on_usage)
            except _Unauthorized:
                if refreshed:
                    raise ChatGptError("ChatGPT rejected the session; sign in again.") from None
                refreshed = True
                cred = valid_credential(force_refresh=True)
            except _Transient as exc:
                if attempts > settings.llm_max_retries:
                    raise ChatGptError(str(exc)) from None
                time.sleep(min(2**attempts, 10))

    def _stream(self, body: bytes, cred: Credential, on_usage: UsageSink | None) -> str:
        headers = {
            "Authorization": f"Bearer {cred.access}",
            "Content-Type": "application/json",
            "Content-Encoding": "zstd",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
            "session-id": self._session,
        }
        timeout = httpx.Timeout(settings.llm_timeout_seconds, connect=30)
        chunks: list[str] = []
        final: dict[str, Any] | None = None
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as http:
                with http.stream("POST", f"{RESOURCE}/responses", content=body, headers=headers) as res:
                    if res.status_code != 200:
                        _raise_for_status(res.status_code, res.read().decode("utf-8", "replace"))
                    for event in _sse_events(res.iter_lines()):
                        kind = event.get("type")
                        if kind == "response.output_text.delta":
                            chunks.append(str(event.get("delta") or ""))
                        elif kind in ("response.completed", "response.incomplete"):
                            final = event.get("response") or {}
                        elif kind in ("response.failed", "error"):
                            detail = (event.get("response") or {}).get("error") or event.get("error") or event
                            raise ChatGptError(f"Model request failed: {json.dumps(detail)[:500]}")
        except httpx.TimeoutException as exc:
            raise _Transient(f"Timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise _Transient(f"Connection failed: {exc}") from exc

        if final is None:
            raise ChatGptError("Model stream ended before the response completed.")
        usage = final.get("usage") or {}
        if on_usage is not None:
            on_usage(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
        text = "".join(chunks) or _output_text(final)
        if final.get("status") == "incomplete":
            reason = (final.get("incomplete_details") or {}).get("reason", "unknown")
            raise ChatGptError(f"Model stopped early ({reason}); the deck is incomplete.")
        return text


class _Unauthorized(Exception):
    pass


class _Transient(Exception):
    pass


def _raise_for_status(status: int, text: str) -> None:
    if status == 401:
        raise _Unauthorized()
    for code in NON_RETRYABLE_CODES:
        if code in text:
            raise ChatGptError(f"ChatGPT plan refused the request ({code}).")
    if status == 429:
        raise ChatGptError(f"Rate limited: {text[:300]}")
    if status >= 500:
        raise _Transient(f"OpenAI server error (HTTP {status}): {text[:300]}")
    raise ChatGptError(f"Request rejected (HTTP {status}): {text[:500]}")


def _sse_events(lines: Any) -> Any:
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                payload = "\n".join(data)
                data = []
                if payload != "[DONE]":
                    try:
                        yield json.loads(payload)
                    except ValueError:
                        continue
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and data[0] != "[DONE]":
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            return


def _output_text(response: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in response.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
    return "".join(texts)
