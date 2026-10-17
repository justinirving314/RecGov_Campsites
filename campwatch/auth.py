"""Single-password login with a signed, httpOnly session cookie."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status

COOKIE_NAME = "campwatch_session"
SESSION_DAYS = 90


def make_token(secret: bytes, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(secret, str(expires))}"


def token_valid(secret: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, signature = token.split(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, expires)):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())


def password_ok(expected: str, given: str) -> bool:
    return hmac.compare_digest(expected.encode(), given.encode())


def require_login(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.auth_required:
        return
    if not token_valid(settings.session_secret, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in first")


def _sign(secret: bytes, value: str) -> str:
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()
