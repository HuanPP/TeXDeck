"""Accounts for a TeXDeck exposed on a network.

Off until an account exists or TEXDECK_REGISTRATION is on, so local and
desktop use need nothing. Accounts live in settings.users_file (written 0600)
with PBKDF2 password hashes; the browser holds an HMAC-signed session cookie.

With registration on, people sign up with an invite code. While there are no
accounts the code is BOOTSTRAP_INVITE, and whoever uses it becomes the admin;
after that only the admin creates codes, each good for one sign-up.

Each account keeps its own model choice, API key and ChatGPT / Claude
sign-ins under user_dir(); `current_user` says whose request or job is
running. Manage accounts on the server:

    python -m app.auth add <username>
    python -m app.auth passwd <username>
    python -m app.auth remove <username>
    python -m app.auth invite
    python -m app.auth list
"""

from __future__ import annotations

import base64
import contextvars
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .config import settings

COOKIE = "texdeck_session"
SESSION_SECONDS = 30 * 24 * 3600
BOOTSTRAP_INVITE = "hi_my_texdeck"
_ITERATIONS = 600_000
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_lock = threading.Lock()

# The account whose request or job is running; None without accounts.
current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("texdeck_user", default=None)

T = TypeVar("T")


class AuthError(RuntimeError):
    """Sign-in failed; message is user-facing English."""


def run_as(username: str | None, fn: Callable[..., T], *args: Any) -> T:
    """Call fn with `username` as the current user (for threads a request starts)."""
    def call() -> T:
        current_user.set(username)
        return fn(*args)
    return contextvars.copy_context().run(call)


def user_dir(username: str | None = None) -> Path | None:
    """Where an account keeps its model settings and sign-ins; None without accounts."""
    username = username or current_user.get()
    if not username:
        return None
    return settings.users_file.parent / "accounts" / username


# --------------------------------------------------------------------------
# Account store
# --------------------------------------------------------------------------


def _read() -> dict[str, Any]:
    try:
        data = json.loads(settings.users_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    path = settings.users_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _users(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    users = (data if data is not None else _read()).get("users")
    return [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []


def enabled() -> bool:
    return settings.registration or bool(_users())


def is_admin(username: str | None) -> bool:
    """The account registered with the bootstrap code; for stores made before
    invites existed, the first account."""
    users = _users()
    if not username or not users:
        return False
    flagged = [u for u in users if u.get("admin")]
    return any(u.get("username") == username for u in flagged or users[:1])


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(candidate.hex(), digest)


def _check_new(username: str, password: str) -> None:
    if not _USERNAME.fullmatch(username):
        raise AuthError("Username must be 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters")


def _add(data: dict[str, Any], username: str, password: str, *, admin: bool = False) -> None:
    """Append a user to `data` (caller holds _lock and writes it)."""
    users = _users(data)
    if any(u.get("username") == username for u in users):
        raise AuthError(f"User already exists: {username}")
    user: dict[str, Any] = {
        "username": username,
        "password_hash": hash_password(password),
        "created_at": _now(),
    }
    if admin:
        user["admin"] = True
    data["users"] = users + [user]
    data.setdefault("secret", secrets.token_hex(32))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_user(username: str, password: str, *, create: bool) -> None:
    """Add a user, or change a password (which signs every session out)."""
    username = username.strip()
    _check_new(username, password)
    with _lock:
        data = _read()
        if create:
            _add(data, username, password)
        else:
            existing = next((u for u in _users(data) if u.get("username") == username), None)
            if existing is None:
                raise AuthError(f"No such user: {username}")
            existing["password_hash"] = hash_password(password)
            data["secret"] = secrets.token_hex(32)  # rotating it ends existing sessions
        _write(data)


def remove_user(username: str) -> None:
    with _lock:
        data = _read()
        users = [u for u in _users(data) if u.get("username") != username]
        if len(users) == len(_users(data)):
            raise AuthError(f"No such user: {username}")
        data["users"] = users
        data["secret"] = secrets.token_hex(32)
        _write(data)


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------


def _invites(data: dict[str, Any]) -> list[dict[str, Any]]:
    invites = data.get("invites")
    return [i for i in invites if isinstance(i, dict)] if isinstance(invites, list) else []


def create_invite(by: str) -> dict[str, Any]:
    if not is_admin(by):
        raise AuthError("Only the admin can create invite codes")
    invite = {"code": secrets.token_urlsafe(9), "created_by": by, "created_at": _now(), "used_by": "", "used_at": ""}
    with _lock:
        data = _read()
        data["invites"] = _invites(data) + [invite]
        _write(data)
    return invite


def list_invites() -> list[dict[str, Any]]:
    return _invites(_read())


def revoke_invite(code: str) -> None:
    """Delete an unused code."""
    with _lock:
        data = _read()
        invites = _invites(data)
        kept = [i for i in invites if i.get("code") != code or i.get("used_by")]
        if len(kept) == len(invites):
            raise AuthError("No such unused invite code")
        data["invites"] = kept
        _write(data)


def _redeem(data: dict[str, Any], code: str, username: str) -> bool:
    """Spend `code` for `username`; True when it makes the admin."""
    if not _users(data):
        if not hmac.compare_digest(code, BOOTSTRAP_INVITE):
            raise AuthError("Invalid or used invite code")
        return True
    for invite in _invites(data):
        if not invite.get("used_by") and hmac.compare_digest(str(invite.get("code")), code):
            invite.update(used_by=username, used_at=_now())
            return False
    raise AuthError("Invalid or used invite code")


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def _secret() -> bytes:
    secret = _read().get("secret")
    if not isinstance(secret, str) or not secret:
        raise AuthError("Account store has no session secret; re-run `python -m app.auth add`")
    return secret.encode("ascii")


def _sign(payload: bytes) -> str:
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def issue_session(username: str) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + SESSION_SECONDS}).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_sign(payload)}"


def session_user(token: str | None) -> str | None:
    """The signed-in username for a cookie value, or None."""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if not hmac.compare_digest(_sign(payload), signature):
            return None
        claims = json.loads(payload)
    except (AuthError, ValueError):
        return None
    if not isinstance(claims, dict) or int(claims.get("exp") or 0) < time.time():
        return None
    username = claims.get("u")
    return username if any(u.get("username") == username for u in _users()) else None


# --------------------------------------------------------------------------
# Sign-in and sign-up, with a failure limit (per client and overall)
# --------------------------------------------------------------------------

_WINDOW_SECONDS = 15 * 60
_PER_CLIENT = 10
_OVERALL = 50
_failures: dict[str, deque[float]] = {}
_all_failures: deque[float] = deque()


def _prune(queue: deque[float], now: float) -> None:
    while queue and now - queue[0] > _WINDOW_SECONDS:
        queue.popleft()


def _check_limit(client: str) -> None:
    now = time.monotonic()
    with _lock:
        mine = _failures.setdefault(client, deque())
        _prune(mine, now)
        _prune(_all_failures, now)
        if len(mine) >= _PER_CLIENT or len(_all_failures) >= _OVERALL:
            raise AuthError("Too many failed attempts; wait a few minutes and try again")


def _record_failure(client: str) -> None:
    now = time.monotonic()
    with _lock:
        _failures.setdefault(client, deque()).append(now)
        _all_failures.append(now)


def login(username: str, password: str, client: str) -> str:
    """Check credentials and return a session token."""
    _check_limit(client)
    user = next((u for u in _users() if u.get("username") == username.strip()), None)
    # Hash even for unknown users so timing does not reveal which names exist.
    stored = user.get("password_hash", "") if user else hash_password("unused-dummy-password")
    if user is None or not _check_password(password, stored):
        _record_failure(client)
        raise AuthError("Wrong username or password")
    with _lock:
        _failures.pop(client, None)
    return issue_session(user["username"])


def register(invite: str, username: str, password: str, client: str) -> str:
    """Create an account with an invite code and return a session token."""
    if not settings.registration:
        raise AuthError("Sign-up is not enabled on this server")
    _check_limit(client)
    username = username.strip()
    _check_new(username, password)
    with _lock:
        data = _read()
        try:
            admin = _redeem(data, invite.strip(), username)
        except AuthError:
            failed = True
        else:
            failed = False
            _add(data, username, password, admin=admin)
            _write(data)
    if failed:
        _record_failure(client)
        raise AuthError("Invalid or used invite code")
    return issue_session(username)


def reset_limits() -> None:
    with _lock:
        _failures.clear()
        _all_failures.clear()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _ask_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise AuthError("Passwords do not match")
    return first


def main(argv: list[str]) -> int:
    usage = "usage: python -m app.auth {add|passwd|remove} <username> | invite | list"
    try:
        if argv[:1] == ["list"]:
            for user in _users():
                print(user.get("username"), "(admin)" if is_admin(user.get("username")) else "")
            return 0
        if argv[:1] == ["invite"]:
            admin = next((u.get("username") for u in _users() if is_admin(u.get("username"))), None)
            if admin is None:
                print(f"No accounts yet; sign up with the code {BOOTSTRAP_INVITE}")
                return 0
            print(create_invite(admin)["code"])
            return 0
        if len(argv) != 2 or argv[0] not in ("add", "passwd", "remove"):
            print(usage, file=sys.stderr)
            return 2
        command, username = argv
        if command == "remove":
            remove_user(username)
        else:
            set_user(username, _ask_password(), create=command == "add")
        print(f"{command}: {username} (accounts in {settings.users_file})")
        return 0
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
