"""考勤源二 / 三的占位适配器。

信息化处尚未提供这两个系统的接口文档。为让「三源 OR 逻辑」的链路先跑通，
这里提供一个**可配置的通用 REST 适配器**：等拿到文档后，只需在 .env 里声明
路径与字段映射（JSONPath 风格的点号路径），无需改动代码。

配置示例（.env）
    SRC2_ENABLED=true
    SRC2_BASE_URL=https://xxx.edu.cn
    SRC2_STAT_PATH=/api/attendance/list
    SRC2_DETAIL_PATH=/api/attendance/detail
    SRC2_AUTH_STYLE=none|header|query
    SRC2_FIELD_MAP={"scheduleGuid":"data.planId","userNo":"data.stuNo", ...}
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.adapters.base import NormalizedCourse, NormalizedEvent
from app.adapters.hanboer import parse_date, parse_dt
from app.core.config import settings
from app.core.logging_conf import get_logger

log = get_logger("adapter.generic")


def dig(obj: Any, path: str, default: Any = None) -> Any:
    """按 'a.b.0.c' 形式取值，支持列表下标。"""
    if not path:
        return default
    cur = obj
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            cur = cur.get(part, default)
        else:
            return default
    return cur if cur is not None else default


class GenericRestAdapter:
    """通用 REST 考勤源适配器（字段映射驱动）。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        enabled: bool,
        access_key: str = "",
        access_key_secret: str = "",
        stat_path: str = "",
        detail_path: str = "",
        auth_style: str = "none",
        field_map: str = "{}",
        list_path: str = "data",
        total_path: str = "total",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.enabled = enabled and bool(base_url)
        self.access_key = access_key
        self.access_key_secret = access_key_secret
        self.stat_path = stat_path
        self.detail_path = detail_path
        self.auth_style = auth_style
        self.list_path = list_path
        self.total_path = total_path
        try:
            import json

            self.field_map: dict[str, str] = json.loads(field_map or "{}")
        except Exception:
            log.warning("[%s] FIELD_MAP 不是合法 JSON，已忽略", name)
            self.field_map = {}

    def _headers(self) -> dict[str, str]:
        if self.auth_style == "header" and self.access_key:
            return {"X-Access-Key": self.access_key, "X-Access-Secret": self.access_key_secret}
        return {}

    def _query_auth(self) -> dict[str, str]:
        if self.auth_style == "query" and self.access_key:
            return {"accessKey": self.access_key, "secret": self.access_key_secret}
        return {}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            verify=settings.hb_verify_tls,
        ) as client:
            resp = await client.get(url, params={**clean, **self._query_auth()}, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def _map(self, row: dict[str, Any], key: str, default: Any = None) -> Any:
        field = self.field_map.get(key)
        return dig(row, field, default) if field else default

    async def iter_statistics(self, **kw: Any) -> list[NormalizedCourse]:
        if not self.enabled or not self.stat_path:
            return []
        payload = await self._get(self.stat_path, {"pageNumber": 1, "pageSize": 50})
        rows = dig(payload, self.list_path, []) or []
        out: list[NormalizedCourse] = []
        for row in rows:
            out.append(
                NormalizedCourse(
                    schedule_guid=str(self._map(row, "scheduleGuid", "")),
                    source=self.name,
                    course_name=str(self._map(row, "courseName", "") or ""),
                    teacher_name=str(self._map(row, "teacherName", "") or ""),
                    classroom_no=str(self._map(row, "classroomNo", "") or ""),
                    start_time=parse_dt(self._map(row, "startTime")),
                    end_time=parse_dt(self._map(row, "endTime")),
                    raw=row,
                )
            )
        return out

    async def iter_details(self, **kw: Any) -> list[NormalizedEvent]:
        if not self.enabled or not self.detail_path:
            return []
        payload = await self._get(self.detail_path, {"pageNumber": 1, "pageSize": 50, **kw})
        rows = dig(payload, self.list_path, []) or []
        out: list[NormalizedEvent] = []
        for row in rows:
            student_no = str(self._map(row, "userNo", "") or "").strip()
            class_date = parse_date(self._map(row, "attendDate"))
            if not student_no or class_date is None:
                continue
            out.append(
                NormalizedEvent(
                    source=self.name,
                    schedule_guid=str(self._map(row, "scheduleGuid", "") or ""),
                    student_no=student_no,
                    class_date=class_date,
                    attend_status=str(self._map(row, "attendStatus", "") or ""),
                    sign_in_time=parse_dt(self._map(row, "signInTime")),
                    node_no=str(self._map(row, "nodeNo", "") or ""),
                    classroom_no=str(self._map(row, "classroomNo", "") or ""),
                    user_type=int(self._map(row, "userType", 2) or 2),
                    raw=row,
                )
            )
        return out

    async def aclose(self) -> None:
        return None


def build_src2() -> GenericRestAdapter:
    return GenericRestAdapter(
        name="src2",
        base_url=settings.src2_base_url,
        enabled=settings.src2_enabled,
        access_key=settings.src2_access_key,
        access_key_secret=settings.src2_access_key_secret,
        stat_path=getattr(settings, "src2_stat_path", "") or "",
        detail_path=getattr(settings, "src2_detail_path", "") or "",
        auth_style=getattr(settings, "src2_auth_style", "none") or "none",
        field_map=getattr(settings, "src2_field_map", "{}") or "{}",
    )


def build_src3() -> GenericRestAdapter:
    return GenericRestAdapter(
        name="src3",
        base_url=settings.src3_base_url,
        enabled=settings.src3_enabled,
        access_key=settings.src3_access_key,
        access_key_secret=settings.src3_access_key_secret,
        stat_path=getattr(settings, "src3_stat_path", "") or "",
        detail_path=getattr(settings, "src3_detail_path", "") or "",
        auth_style=getattr(settings, "src3_auth_style", "none") or "none",
        field_map=getattr(settings, "src3_field_map", "{}") or "{}",
    )
