"""访问口令接口。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request, Response, status

from app import __version__
from app.core.config import settings
from app.core.security import check_password, client_fingerprint, issue_token, token_valid
from app.schemas import LoginIn, SessionOut

router = APIRouter(tags=["auth"])

#: 简易登录频率限制：每指纹 5 分钟内最多 10 次失败
_WINDOW = 300
_MAX_FAILS = 10
_fails: dict[str, deque[float]] = defaultdict(deque)


def _too_many(fp: str) -> bool:
    now = time.time()
    q = _fails[fp]
    while q and now - q[0] > _WINDOW:
        q.popleft()
    return len(q) >= _MAX_FAILS


def _record_fail(fp: str) -> None:
    _fails[fp].append(time.time())


@router.get("/session", response_model=SessionOut)
def read_session(request: Request, response: Response) -> SessionOut:
    token = request.cookies.get(settings.cookie_name)
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(
        authenticated=token_valid(token),
        data_mode=settings.data_mode,
        app_base_path=settings.app_base_path,
        version=__version__,
        now=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


@router.post("/login", response_model=SessionOut)
def login(payload: LoginIn, request: Request, response: Response) -> SessionOut:
    fp = client_fingerprint(request)
    if _too_many(fp):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试次数过多，请 5 分钟后再试",
        )
    if not check_password(payload.password):
        _record_fail(fp)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="口令不正确")

    _fails.pop(fp, None)
    response.set_cookie(
        key=settings.cookie_name,
        value=issue_token(),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return SessionOut(
        authenticated=True,
        data_mode=settings.data_mode,
        app_base_path=settings.app_base_path,
        version=__version__,
        now=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


@router.post("/logout", response_model=SessionOut)
def logout(response: Response) -> SessionOut:
    response.delete_cookie(settings.cookie_name, path="/")
    return SessionOut(authenticated=False, data_mode=settings.data_mode)
