"""口令网关与会话。

两道防线：
1. **隐藏路径**：整个应用挂在 monitor.example.edu.cn/<APP_BASE_PATH>/ 下，路径本身就是口令；
2. **访问口令**：未通过口令校验的请求一律 401，且不返回任何业务数据。
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

from fastapi import Cookie, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings

_SALT = "student-monitor-gate"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_SALT)


def check_password(raw: str) -> bool:
    """常量时间比较，避免时序侧信道。"""
    return hmac.compare_digest((raw or "").encode("utf-8"), settings.gate_password.encode("utf-8"))


def issue_token() -> str:
    return _serializer().dumps({"ok": True, "t": int(time.time())})


def token_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=settings.session_ttl_hours * 3600)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_session(request: Request, smon_session: str | None = Cookie(default=None)) -> None:
    """FastAPI 依赖：校验口令会话。"""
    if token_valid(smon_session):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未通过访问口令校验",
        headers={"X-Gate": "locked"},
    )


def client_fingerprint(request: Request) -> str:
    """给简单的登录频率限制用。"""
    fwd = request.headers.get("x-forwarded-for", "")
    ip = (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "?")
    ua = request.headers.get("user-agent", "")
    return sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]
