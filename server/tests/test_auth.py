"""Account store, password hashes, signed sessions, the failure limit and the CLI."""

from __future__ import annotations

import json
import time

import pytest

from app import auth
from app.config import settings


@pytest.fixture(autouse=True)
def _fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_ITERATIONS", 1_000)
    auth.reset_limits()


def test_off_without_accounts() -> None:
    assert not auth.enabled()
    auth.set_user("huan", "correct horse", create=True)
    assert auth.enabled()


def test_store_is_private_and_hashed() -> None:
    auth.set_user("huan", "correct horse", create=True)
    assert (settings.users_file.stat().st_mode & 0o777) == 0o600
    assert "correct horse" not in settings.users_file.read_text()


def test_bad_usernames_and_passwords_are_refused() -> None:
    with pytest.raises(auth.AuthError, match="Username"):
        auth.set_user("two words", "correct horse", create=True)
    with pytest.raises(auth.AuthError, match="at least 8"):
        auth.set_user("huan", "short", create=True)


def test_tampered_expired_and_rotated_sessions_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    auth.set_user("huan", "correct horse", create=True)
    token = auth.issue_session("huan")
    assert auth.session_user(token) == "huan"
    body, signature = token.rsplit(".", 1)
    assert auth.session_user(body + "." + "0" * len(signature)) is None
    assert auth.session_user("garbage") is None

    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: 10**10)
        assert auth.session_user(token) is None

    auth.set_user("huan", "new password!", create=False)
    assert auth.session_user(token) is None  # a password change signs everyone out

    auth.set_user("ghost", "whatever123", create=True)
    ghost = auth.issue_session("ghost")
    auth.remove_user("ghost")
    assert auth.session_user(ghost) is None


def test_login_and_failure_limit() -> None:
    auth.set_user("huan", "correct horse", create=True)
    assert auth.session_user(auth.login("huan", "correct horse", "a")) == "huan"
    for _ in range(10):
        with pytest.raises(auth.AuthError, match="Wrong"):
            auth.login("huan", "wrong password", "a")
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.login("huan", "correct horse", "a")  # even the right password waits
    assert auth.login("huan", "correct horse", "b")  # another client is not blocked


def test_command_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(auth.getpass, "getpass", lambda _prompt: "correct horse")
    assert auth.main(["add", "huan"]) == 0
    assert auth.main(["add", "huan"]) == 1
    assert auth.main(["list"]) == 0
    assert "huan" in capsys.readouterr().out
    monkeypatch.setattr(auth.getpass, "getpass", lambda _prompt: "short")
    assert auth.main(["passwd", "huan"]) == 1
    assert auth.main(["remove", "huan"]) == 0
    assert not auth.enabled()
    assert json.loads(settings.users_file.read_text())["users"] == []
