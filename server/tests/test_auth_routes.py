"""With an account, every API call and the progress socket need a session."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth
from app.main import app


@pytest.fixture(autouse=True)
def _fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_ITERATIONS", 1_000)
    auth.reset_limits()


@pytest.fixture
def http() -> TestClient:
    return TestClient(app)


def _sign_in(http: TestClient, password: str = "correct horse") -> int:
    return http.post("/api/auth/session", json={"username": "huan", "password": password}).status_code


def test_open_without_accounts(http: TestClient) -> None:
    assert http.get("/api/model").status_code == 200
    assert http.get("/api/auth/session").json() == {"enabled": False, "user": None}


def test_accounts_lock_the_api(http: TestClient) -> None:
    auth.set_user("huan", "correct horse", create=True)
    assert http.get("/api/model").status_code == 401
    assert http.get("/api/templates").status_code == 401
    assert http.get("/health").status_code == 200
    assert http.get("/api/auth/session").json() == {"enabled": True, "user": None}

    assert _sign_in(http, "wrong password") == 401
    assert _sign_in(http) == 200
    assert http.get("/api/model").status_code == 200
    assert http.get("/api/auth/session").json() == {"enabled": True, "user": "huan"}

    http.delete("/api/auth/session")
    assert http.get("/api/model").status_code == 401


def test_cookie_flags(http: TestClient) -> None:
    auth.set_user("huan", "correct horse", create=True)
    plain = http.post("/api/auth/session", json={"username": "huan", "password": "correct horse"})
    cookie = plain.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie
    proxied = http.post("/api/auth/session", json={"username": "huan", "password": "correct horse"},
                        headers={"X-Forwarded-Proto": "https"})
    assert "secure" in proxied.headers["set-cookie"].lower()


def test_failed_sign_ins_are_limited_per_client(http: TestClient) -> None:
    auth.set_user("huan", "correct horse", create=True)
    for _ in range(10):
        assert _sign_in(http, "wrong password") == 401
    assert _sign_in(http) == 429
    other = TestClient(app)
    status = other.post("/api/auth/session", json={"username": "huan", "password": "correct horse"},
                        headers={"CF-Connecting-IP": "203.0.113.9"}).status_code
    assert status == 200


def _ws_close_reason(http: TestClient) -> str:
    with pytest.raises(WebSocketDisconnect) as closed:
        with http.websocket_connect("/ws/generations/20260101-000000-abcd") as ws:
            ws.receive_json()
    return closed.value.reason


def test_websocket_needs_a_session(http: TestClient) -> None:
    auth.set_user("huan", "correct horse", create=True)
    assert _ws_close_reason(http) == "Sign in first"
    _sign_in(http)
    assert _ws_close_reason(http).startswith("Generation not found")  # past the sign-in check
