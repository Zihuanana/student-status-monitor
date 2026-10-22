"""SQLAlchemy 声明式基类（单独成文件，避免与引擎模块循环导入）。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """统一使用朴素 UTC 时间入库，展示层再转本地时区。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
