"""API 路由汇总。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import admin, auth, students

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(students.router)
api_router.include_router(admin.router)


@api_router.get("/health", tags=["health"])
def health() -> dict:
    return {"ok": True}


__all__ = ["api_router"]
