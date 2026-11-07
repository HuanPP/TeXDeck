"""Optional sign-in for a TeXDeck exposed on a network.

Off until an account exists, so local and desktop use need nothing. Accounts
live in settings.users_file (written 0600) with PBKDF2 password hashes; the
browser holds an HMAC-signed session cookie. Manage accounts on the server:

    python -m app.auth add <username>
    python -m app.auth passwd <username>
    python -m app.auth remove <username>
    python -m app.auth list
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .config import settings

COOKIE = "texdeck_session"
SESSION_SECONDS = 30 * 24 * 3600
_ITERATIONS = 600_000
_lock = threading.Lock()


class AuthError(RuntimeError):
    """Sign-in failed; message is user-facing English."""


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


def _users() -> list[dict[str, Any]]:
    users = _read().get("users")
    return [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []


def enabled() -> bool:
    return bool(_users())


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


def set_user(username: str, password: str, *, create: bool) -> None:
    """Add a user or change a password. Either way every session is signed out."""
    username = username.strip()
    if not username or len(username) > 64 or any(ch.isspace() for ch in username):
        raise AuthError("Username must be 1-64 characters without spaces")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters")
    with _lock:
        data = _read()
        users = _users()
        existing = next((u for u in users if u.get("username") == username), None)
        if create and existing:
            raise AuthError(f"User already exists: {username}")
        if not create and not existing:
            raise AuthError(f"No such user: {username}")
        if existing:
            existing["password_hash"] = hash_password(password)
        else:
            users.append({
                "username": username,
                "password_hash": hash_password(password),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        data["users"] = users
        data["secret"] = secrets.token_hex(32)  # rotating it ends existing sessions
        _write(data)


def remove_user(username: str) -> None:
    with _lock:
        data = _read()
        users = [u for u in _users() if u.get("username") != username]
        if len(users) == len(_users()):
            raise AuthError(f"No such user: {username}")
        data["users"] = users
        data["secret"] = secrets.token_hex(32)
        _write(data)


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
# Login with a failure limit (per client and overall)
# --------------------------------------------------------------------------

_WINDOW_SECONDS = 15 * 60
_PER_CLIENT = 10
_OVERALL = 50
_failures: dict[str, deque[float]] = {}
_all_failures: deque[float] = deque()


def _prune(queue: deque[float], now: float) -> None:
    while queue and now - queue[0] > _WINDOW_SECONDS:
        queue.popleft()


def login(username: str, password: str, client: str) -> str:
    """Check credentials and return a session token."""
    now = time.monotonic()
    with _lock:
        mine = _failures.setdefault(client, deque())
        _prune(mine, now)
        _prune(_all_failures, now)
        if len(mine) >= _PER_CLIENT or len(_all_failures) >= _OVERALL:
            raise AuthError("Too many failed sign-ins; wait a few minutes and try again")
    user = next((u for u in _users() if u.get("username") == username.strip()), None)
    # Hash even for unknown users so timing does not reveal which names exist.
    stored = user.get("password_hash", "") if user else hash_password("unused-dummy-password")
    if user is None or not _check_password(password, stored):
        with _lock:
            _failures.setdefault(client, deque()).append(now)
            _all_failures.append(now)
        raise AuthError("Wrong username or password")
    with _lock:
        _failures.pop(client, None)
    return issue_session(user["username"])


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
    usage = "usage: python -m app.auth {add|passwd|remove} <username> | list"
    try:
        if argv[:1] == ["list"]:
            for user in _users():
                print(user.get("username"))
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
