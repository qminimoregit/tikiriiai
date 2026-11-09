"""Admin login: one username/password from .env, signed session cookie, login rate limit."""

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings

log = logging.getLogger(__name__)

COOKIE = "tikiriiai_session"
# Reachable without logging in: the login page and what it needs, the shared call-client files, health, Meta's WhatsApp webhook (checks its own signature), and the
# PBX hook (which checks PBX_ALLOWED_IPS and PBX_HOOK_TOKEN itself, see routers/pbx.py).
PUBLIC_PATHS = ("/login.html", "/css/", "/favicon", "/public/", "/api/auth/", "/api/pbx/", "/api/whatsapp/", "/api/health")
MAX_FAILURES = 5
LOCKOUT_SECONDS = 5 * 60

_failures: dict[str, list[float]] = defaultdict(list)
_random_secret = secrets.token_hex(32)


def _secret() -> bytes:
    # Without SESSION_SECRET a random key is used: everyone has to sign in again after a restart.
    return (get_settings().session_secret or _random_secret).encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(username: str) -> str:
    expires = int(time.time()) + get_settings().session_days * 86400
    payload = f"{username}|{expires}"
    return f"{payload}|{_sign(payload)}"


def valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        username, expires, sig = token.rsplit("|", 2)
    except ValueError:
        return False
    if not hmac.compare_digest(sig, _sign(f"{username}|{expires}")):
        return False
    return username == get_settings().admin_username and expires.isdigit() and int(expires) > time.time()


def is_logged_in(cookies) -> bool:
    return valid_token(cookies.get(COOKIE))


def same_origin(ws: WebSocket) -> bool:
    """Browsers always send Origin on WebSockets; it must match the page's host (blocks cross-site hijacking)."""
    origin = ws.headers.get("origin")
    if not origin:
        return True  # non-browser client (tests, scripts); the cookie check still applies
    return urlparse(origin).netloc == ws.headers.get("host", "")


def ws_authorized(ws: WebSocket) -> bool:
    return is_logged_in(ws.cookies) and same_origin(ws)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(PUBLIC_PATHS) or is_logged_in(request.cookies):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Please sign in"}, status_code=401)
        return RedirectResponse(f"/login.html?next={path}", status_code=303)


class LoginIn(BaseModel):
    username: str
    password: str


router = APIRouter(prefix="/api/auth", tags=["auth"])


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login")
def login(data: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    now = time.time()
    _failures[ip] = [t for t in _failures[ip] if now - t < LOCKOUT_SECONDS]
    if len(_failures[ip]) >= MAX_FAILURES:
        raise HTTPException(429, "Too many wrong attempts. Please wait 5 minutes and try again.")

    s = get_settings()
    user_ok = hmac.compare_digest(data.username.strip().encode(), s.admin_username.encode())
    pass_ok = hmac.compare_digest(data.password.encode(), s.admin_password.encode())
    if not (user_ok and pass_ok and s.admin_password):
        _failures[ip].append(now)
        log.warning("failed admin login from %s", ip)
        raise HTTPException(401, "Wrong username or password")

    _failures.pop(ip, None)
    response.set_cookie(
        COOKIE,
        make_token(s.admin_username),
        max_age=s.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    from .db import SessionLocal
    from .snapshot import get_profile

    if not is_logged_in(request.cookies):
        return {"logged_in": False}
    with SessionLocal() as db:
        profile = get_profile(db)
        return {"logged_in": True, "username": get_settings().admin_username, "timezone": profile.timezone,
                "company": profile.company_name}
