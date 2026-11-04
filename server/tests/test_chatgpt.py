"""ChatGPT sign-in flow and Responses client against a mocked OpenAI."""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import pytest
import zstandard
from fastapi.testclient import TestClient

from app import chatgpt
from app.config import settings
from app.llm import ChatGptClient, LlmError, active_model, active_provider, create_client


def _jwt(claims: dict[str, Any]) -> str:
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def _params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _token_transport(seen: list[dict[str, str]], *, scope: str | None = None, nonce: str | None = None,
                     client_id: str = "client_abc") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        seen.append(form)
        assert str(request.url) == chatgpt.TOKEN_URL
        claims = {
            "iss": chatgpt.ISSUER,
            "aud": client_id,
            "sub": "user-1",
            "exp": time.time() + 600,
            "nonce": nonce if nonce is not None else _last_nonce.get("nonce", ""),
            "email": "me@example.com",
        }
        return httpx.Response(200, json={
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": _jwt(claims),
            "expires_in": 3600,
            "scope": scope if scope is not None else chatgpt.SCOPE,
        })
    return httpx.MockTransport(handler)


_last_nonce: dict[str, str] = {}


def _start() -> dict[str, str]:
    params = _params(chatgpt.start_login(listen=False))
    _last_nonce["nonce"] = params["nonce"]
    return params


def test_first_sign_in_uses_registration_client_and_agent_name() -> None:
    params = _start()
    assert params["client_id"] == chatgpt.REGISTRATION_CLIENT_ID
    assert params["agent_name_hint"] == "TeXDeck"
    assert params["code_challenge_method"] == "S256"
    assert chatgpt.TOKEN_SHARING_SCOPE in params["scope"].split()
    assert params["resource"] == chatgpt.RESOURCE
    assert params["ext_agent_host_id"].startswith("urn:uuid:")


def test_full_sign_in_saves_credential_and_reuses_client_id(_isolated_chatgpt_auth: Path) -> None:
    params = _start()
    seen: list[dict[str, str]] = []
    cred = chatgpt.complete_login(
        {"code": "c1", "state": params["state"], "client_id": "client_abc"},
        _token_transport(seen),
    )
    assert cred.client_id == "client_abc" and cred.email == "me@example.com"
    assert seen[0]["grant_type"] == "authorization_code"
    assert seen[0]["client_id"] == "client_abc"
    assert seen[0]["redirect_uri"] == params["redirect_uri"]
    assert _isolated_chatgpt_auth.stat().st_mode & 0o777 == 0o600
    assert chatgpt.status()["signed_in"] is True

    # Second sign-in presents the registered client and drops the name hint.
    again = _start()
    assert again["client_id"] == "client_abc"
    assert "agent_name_hint" not in again
    assert again["ext_agent_host_id"] == params["ext_agent_host_id"]


def test_callback_with_wrong_state_is_rejected_and_attempt_survives() -> None:
    params = _start()
    with pytest.raises(chatgpt.ChatGptError, match="different sign-in"):
        chatgpt.complete_login({"code": "c", "state": "nope", "client_id": "x"})
    # The genuine callback still completes afterwards.
    chatgpt.complete_login(
        {"code": "c", "state": params["state"], "client_id": "client_abc"}, _token_transport([])
    )


def test_missing_token_sharing_scope_fails() -> None:
    params = _start()
    with pytest.raises(chatgpt.ChatGptError, match="token sharing"):
        chatgpt.complete_login(
            {"code": "c", "state": params["state"], "client_id": "client_abc"},
            _token_transport([], scope="openid email"),
        )
    assert chatgpt.load_credential() is None


def test_id_token_nonce_mismatch_fails() -> None:
    params = _start()
    with pytest.raises(chatgpt.ChatGptError, match="different attempt"):
        chatgpt.complete_login(
            {"code": "c", "state": params["state"], "client_id": "client_abc"},
            _token_transport([], nonce="other"),
        )


def test_oauth_error_is_reported() -> None:
    params = _start()
    with pytest.raises(chatgpt.ChatGptError, match="access_denied"):
        chatgpt.complete_login({"error": "access_denied", "state": params["state"]})


def test_paste_back_url() -> None:
    params = _start()
    url = f"http://127.0.0.1:1455/auth/callback?code=c&state={params['state']}&client_id=client_abc"
    cred = chatgpt.complete_login_from_url(url, _token_transport([]))
    assert cred.access == "access-1"


def _sign_in(expires_in: float = 3600) -> None:
    chatgpt.save_credential(chatgpt.Credential(
        access="access-1", refresh="refresh-1", expires=time.time() + expires_in, client_id="client_abc",
    ))


def test_expired_credential_is_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(expires_in=-10)
    seen: list[dict[str, str]] = []
    original = chatgpt.refresh_credential
    monkeypatch.setattr(
        chatgpt, "refresh_credential", lambda cred, transport=None: original(cred, _token_transport(seen))
    )
    cred = chatgpt.valid_credential()
    assert seen[0]["grant_type"] == "refresh_token"
    assert seen[0]["refresh_token"] == "refresh-1"
    assert cred.access == "access-1" and cred.expires > time.time() + 3000


def test_loopback_listener_serves_callback() -> None:
    params = _params(chatgpt.start_login(listen=True))
    redirect = params["redirect_uri"]
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/auth/callback")
    stray = httpx.get(f"{redirect}?code=c&state=wrong", timeout=5)
    assert stray.status_code == 400 and "different sign-in" in stray.text
    assert httpx.get(redirect.replace("/auth/callback", "/favicon.ico"), timeout=5).status_code == 404
    assert chatgpt.status()["pending"] is True


def test_messages_convert_to_responses_input() -> None:
    instructions, items = chatgpt.to_responses_input([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": [
            {"type": "text", "text": "Page 1"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "fix it"},
    ])
    assert instructions == "be terse"
    assert items[0]["content"] == [
        {"type": "input_text", "text": "Page 1"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]
    assert items[1] == {"role": "assistant", "content": [{"type": "output_text", "text": "prior"}]}
    assert items[2]["content"] == [{"type": "input_text", "text": "fix it"}]


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _ok_stream(text: str = "\\documentclass{beamer}") -> bytes:
    half = len(text) // 2
    return _sse(
        {"type": "response.created", "response": {}},
        {"type": "response.output_text.delta", "delta": text[:half]},
        {"type": "response.output_text.delta", "delta": text[half:]},
        {"type": "response.completed", "response": {
            "status": "completed", "usage": {"input_tokens": 120, "output_tokens": 30}}},
    )


def test_streamed_completion_with_usage_and_zstd_body() -> None:
    _sign_in()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer access-1"
        assert request.headers["content-encoding"] == "zstd"
        bodies.append(json.loads(zstandard.ZstdDecompressor().decompress(request.content)))
        return httpx.Response(200, content=_ok_stream())

    client = chatgpt.ResponsesClient("gpt-test", transport=httpx.MockTransport(handler))
    usage: list[tuple[int, int]] = []
    out = client.complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        lambda p, c: usage.append((p, c)),
    )
    assert out == "\\documentclass{beamer}"
    assert usage == [(120, 30)]
    body = bodies[0]
    assert body["model"] == "gpt-test" and body["stream"] is True and body["store"] is False
    assert body["instructions"] == "s"
    assert "temperature" not in body


def test_401_refreshes_once_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(401, text="expired")
        return httpx.Response(200, content=_ok_stream("ok"))

    def fake_refresh(cred: chatgpt.Credential, transport: Any = None) -> chatgpt.Credential:
        fresh = chatgpt.Credential(access="access-2", refresh="r2", expires=time.time() + 3600,
                                   client_id=cred.client_id)
        chatgpt.save_credential(fresh)
        return fresh

    monkeypatch.setattr(chatgpt, "refresh_credential", fake_refresh)
    client = chatgpt.ResponsesClient("m", transport=httpx.MockTransport(handler))
    assert client.complete([{"role": "user", "content": "u"}], None) == "ok"
    assert calls == ["Bearer access-1", "Bearer access-2"]


def test_plan_limit_is_not_retried() -> None:
    _sign_in()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text='{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}')

    client = chatgpt.ResponsesClient("m", transport=httpx.MockTransport(handler))
    with pytest.raises(chatgpt.ChatGptError, match="usage_limit_exceeded"):
        client.complete([{"role": "user", "content": "u"}], None)
    assert calls == [1]


def test_incomplete_response_fails_loudly() -> None:
    _sign_in()
    stream = _sse(
        {"type": "response.output_text.delta", "delta": "\\begin{document"},
        {"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "usage": {}}},
    )
    client = chatgpt.ResponsesClient("m", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=stream)))
    with pytest.raises(chatgpt.ChatGptError, match="max_output_tokens"):
        client.complete([{"role": "user", "content": "u"}], None)


def test_provider_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    assert active_provider() == "api" and active_model() == settings.llm_model
    _sign_in()
    assert active_provider() == "chatgpt" and active_model() == settings.chatgpt_model
    assert isinstance(create_client(), ChatGptClient)
    monkeypatch.setattr(settings, "llm_provider", "api")
    assert active_provider() == "api"
    chatgpt.sign_out()
    monkeypatch.setattr(settings, "llm_provider", "chatgpt")
    with pytest.raises(LlmError, match="sign in"):
        create_client()


def test_model_routes() -> None:
    from app.main import app

    http = TestClient(app)
    assert http.get("/api/model").json()["signed_in"] is False
    url = http.post("/api/auth/chatgpt/login").json()["url"]
    assert url.startswith(chatgpt.AUTHORIZE_URL)
    bad = http.post("/api/auth/chatgpt/callback", json={"url": "http://127.0.0.1/auth/callback?state=x&code=y"})
    assert bad.status_code == 400
    _sign_in()
    assert http.get("/api/model").json()["provider"] == "chatgpt"
    assert http.delete("/api/auth/chatgpt").json()["signed_in"] is False
