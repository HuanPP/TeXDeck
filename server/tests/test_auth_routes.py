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
    assert http.get("/api/auth/session").json() == {"enabled": False, "user": None, "admin": False,
                                                    "registration": False}


def test_accounts_lock_the_api(http: TestClient) -> None:
    auth.set_user("huan", "correct horse", create=True)
    assert http.get("/api/model").status_code == 401
    assert http.get("/api/templates").status_code == 401
    assert http.get("/health").status_code == 200
    assert http.get("/api/auth/session").json()["user"] is None

    assert _sign_in(http, "wrong password") == 401
    assert _sign_in(http) == 200
    assert http.get("/api/model").status_code == 200
    # Accounts made before invites existed: the first one is the admin.
    assert http.get("/api/auth/session").json() == {"enabled": True, "user": "huan", "admin": True,
                                                    "registration": False}

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



# --------------------------------------------------------------------------
# Invite-code sign-up and per-account data
# --------------------------------------------------------------------------


@pytest.fixture
def signup(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "registration", True)


def _register(http: TestClient, invite: str, username: str, password: str = "correct horse"):
    return http.post("/api/auth/register", json={"invite": invite, "username": username, "password": password})


def test_registration_requires_sign_in_before_any_account(signup, http: TestClient) -> None:
    assert http.get("/api/auth/session").json() == {"enabled": True, "user": None, "admin": False,
                                                    "registration": True}
    assert http.get("/api/model").status_code == 401


def test_bootstrap_code_makes_the_admin_once(signup, http: TestClient) -> None:
    assert _register(http, "wrong", "first").status_code == 400
    r = _register(http, auth.BOOTSTRAP_INVITE, "first")
    assert r.status_code == 200 and r.json()["admin"] is True
    assert http.get("/api/model").status_code == 200  # signed in by sign-up
    # Spent: nobody else can use it.
    assert _register(TestClient(app), auth.BOOTSTRAP_INVITE, "second").status_code == 400


def test_only_the_admin_hands_out_single_use_codes(signup, http: TestClient) -> None:
    _register(http, auth.BOOTSTRAP_INVITE, "admin")
    code = http.post("/api/auth/invites").json()[0]["code"]

    guest = TestClient(app)
    assert _register(guest, code, "bob").json() == {"enabled": True, "user": "bob", "admin": False,
                                                    "registration": True}
    assert _register(TestClient(app), code, "eve").status_code == 400  # used
    assert guest.post("/api/auth/invites").status_code == 403
    assert guest.get("/api/auth/invites").status_code == 403

    listed = http.get("/api/auth/invites").json()
    assert listed[0]["used_by"] == "bob"
    spare = http.post("/api/auth/invites").json()[1]["code"]
    assert http.delete(f"/api/auth/invites/{spare}").json() == listed
    assert http.delete(f"/api/auth/invites/{code}").status_code == 404  # used codes stay on record


def test_sign_up_is_off_without_the_setting(http: TestClient) -> None:
    assert _register(http, auth.BOOTSTRAP_INVITE, "first").status_code == 400


def test_each_account_has_its_own_model_settings_and_decks(signup, http: TestClient, artifact_root) -> None:
    from app import store

    _register(http, auth.BOOTSTRAP_INVITE, "admin")
    code = http.post("/api/auth/invites").json()[0]["code"]
    bob = TestClient(app)
    _register(bob, code, "bob")

    saved = bob.put("/api/model/api", json={"api_key": "sk-bob-0000", "base_url": "https://x.test/v1",
                                            "model": "m"})
    assert saved.json()["api"]["key_hint"].endswith("0000")
    assert http.get("/api/model").json()["api"]["configured"] is False
    assert (auth.user_dir("bob") / "model-state.json").is_file()

    mine = store.new_generation(title="bob deck", template_key="navy", template_name="Navy",
                                instruction="", source_kind="text", source_names=[], owner="bob")
    old = store.new_generation(title="before accounts", template_key="navy", template_name="Navy",
                               instruction="", source_kind="text", source_names=[])
    assert [g["id"] for g in bob.get("/api/generations").json()] == [mine.id]
    assert [g["id"] for g in http.get("/api/generations").json()] == [old.id]
    assert http.get(f"/api/generations/{mine.id}").status_code == 404
    assert bob.delete(f"/api/generations/{old.id}").status_code == 404


def test_jobs_and_sign_ins_belong_to_the_account_that_started_them(tmp_path) -> None:
    import threading

    from app import chatgpt, claude_code, tasks

    seen: list[str | None] = []
    done = threading.Event()
    auth.run_as("bob", tasks.start, "20260101-000000-b0b0", lambda _p: (seen.append(auth.current_user.get()), done.set()))
    assert done.wait(5) and seen == ["bob"]

    home = auth.user_dir("bob")
    assert auth.run_as("bob", chatgpt._auth_path) == home / "chatgpt-auth.json"
    assert auth.run_as("bob", claude_code._env)["CLAUDE_CONFIG_DIR"] == str(home / "claude")
    assert claude_code._env() is None  # without accounts: the CLI's own login
