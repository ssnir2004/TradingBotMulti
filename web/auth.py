"""Signed-cookie session auth, identical pattern to TradingBot's own
web/auth.py: no session store, the cookie itself carries the username,
signed with SESSION_SECRET so it can't be forged, and expires after
MAX_AGE_SECONDS. Role is deliberately NOT stored in the cookie - every
request re-reads it from the users table, so an admin demotion (or
deactivation) takes effect on the very next request, not just the next
login.
"""
import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src import db

PROJECT_DIR = Path(__file__).resolve().parent.parent
_env = dotenv_values(PROJECT_DIR / ".env")
SECRET = _env.get("SESSION_SECRET") or os.environ.get("SESSION_SECRET")
if not SECRET:
    raise RuntimeError(
        "SESSION_SECRET is not set. Add a long random value to .env before running the "
        "dashboard (e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`)."
    )

COOKIE_NAME = "tbm_session"
MAX_AGE_SECONDS = 7 * 24 * 60 * 60

_serializer = URLSafeTimedSerializer(SECRET)


def make_session_cookie(username: str) -> str:
    return _serializer.dumps({"username": username})


def read_username(request: Request) -> str | None:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    try:
        data = _serializer.loads(cookie, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("username")


def require_user(request: Request) -> dict:
    """FastAPI dependency for page/API routes: 401s if not logged in or
    the account was deactivated since login."""
    username = read_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get_user_by_username(username)
    if user is None or not user["is_active"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user
