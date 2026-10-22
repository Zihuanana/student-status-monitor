"""翰博尔「智慧课堂考勤汇总接口 v1.0」客户端。

依据《智慧课堂考勤汇总接口》说明书实现：

鉴权（每个接口都必须携带）
    签名参数：sign(有效期15分钟) / timeStamp(13位) / appSource / accessKey
    签名算法：除 sign 外，把参数按参数名（小写）正序排序，
              拼接「参数名+参数值」成字符串，再把 AccessKeySecret
              拼接到该字符串的**头部和尾部**，MD5(32位)，转大写。

接口
    1) GET /attendance/api/v1/openApi/getAttendanceStatistics  考勤汇总（课程维度）
    2) GET /attendance/api/v1/openApi/getAttendanceDetails     考勤详情（学生维度）
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from app.adapters.base import NormalizedCourse, NormalizedEvent
from app.core.config import settings
from app.core.enums import Source
from app.core.logging_conf import get_logger

log = get_logger("adapter.hanboer")

STAT_PATH = "/attendance/api/v1/openApi/getAttendanceStatistics"
DETAIL_PATH = "/attendance/api/v1/openApi/getAttendanceDetails"

#: 说明书规定 pageSize 最大值
MAX_PAGE_SIZE = 50
#: sign 有效期（分钟）
SIGN_TTL_MINUTES = 15

_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_dt(raw: Any) -> datetime | None:
    """解析接口返回的时间字符串（示例形如 2024-07-1106:24:00）。"""
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, (int, float)):
        try:
            ts = float(raw)
            if ts > 1e11:  # 毫秒
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 兜底：去掉多余空格再试
    compact = text.replace(" ", "")
    for fmt in ("%Y-%m-%d%H:%M:%S", "%Y-%m-%d%H:%M"):
        try:
            return datetime.strptime(compact, fmt)
        except ValueError:
            continue
    log.debug("无法解析时间: %r", raw)
    return None


def parse_date(raw: Any) -> date | None:
    if raw in (None, "", "null"):
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    dt = parse_dt(raw)
    return dt.date() if dt else None


class HanboerError(RuntimeError):
    """接口返回 code != 0 或网络异常。"""


class HanboerClient:
    """带签名与重试的 HTTP 客户端。"""

    #: 进程内共享的最小请求间隔（秒）。回补时按"每门课一次明细"发请求，
    #: 实测会被服务端 403 限流；全局节流比在每处调用点 sleep 更可靠。
    #: 0.4s ≈ 2.5 请求/秒；700 门课的回补约 5 分钟，实测不再被限流。
    _MIN_INTERVAL = 0.4
    _rate_lock: "asyncio.Lock | None" = None
    _last_request_at: float = 0.0

    def __init__(
        self,
        base_url: str | None = None,
        access_key: str | None = None,
        access_key_secret: str | None = None,
        app_source: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.hb_base_url).rstrip("/")
        self.access_key = access_key if access_key is not None else settings.hb_access_key
        self.access_key_secret = (
            access_key_secret if access_key_secret is not None else settings.hb_access_key_secret
        )
        self.app_source = app_source if app_source is not None else settings.hb_app_source
        self.timeout = timeout or settings.http_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.http_max_retries
        self._client: httpx.AsyncClient | None = None

    # ── 签名 ──────────────────────────────────────────────
    def build_sign(self, params: dict[str, Any], time_stamp: str | None = None) -> dict[str, str]:
        """按说明书生成签名参数。

        注意：参与签名的「参数名均为使用小写计算」，business 参数原样参与值拼接。
        """
        ts = time_stamp or str(int(time.time() * 1000))
        sign_fields = {
            "timestamp": ts,
            "appsource": self.app_source,
            "accesskey": self.access_key,
        }
        # 业务参数也参与签名（除 sign 外）
        for k, v in params.items():
            if k.lower() == "sign" or v is None or v == "":
                continue
            sign_fields[k.lower()] = "" if v is None else str(v)

        joined = "".join(f"{k}{sign_fields[k]}" for k in sorted(sign_fields.keys()))
        raw = f"{self.access_key_secret}{joined}{self.access_key_secret}"
        sign = hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

        out = {
            "sign": sign,
            "timeStamp": ts,
            "appSource": self.app_source,
            "accessKey": self.access_key,
        }
        return out

    @staticmethod
    def verify_sign(params: dict[str, str]) -> bool:
        """自检用：给定的 sign 是否在有效期内（不校验正确性，只查时间戳）。"""
        ts = params.get("timeStamp", "")
        if not ts.isdigit():
            return False
        delta = abs(time.time() * 1000 - int(ts))
        return delta <= SIGN_TTL_MINUTES * 60 * 1000

    # ── HTTP ─────────────────────────────────────────────
    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise HanboerError("未配置 HB_BASE_URL，无法调用翰博尔接口")
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        signed = {**clean, **self.build_sign(clean)}
        url = f"{self.base_url}{path}"

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._throttle()
                client = await self._ensure_client()
                resp = await client.get(url, params=signed)
                if resp.status_code in (403, 429):
                    # 服务端限流：实测回补时按"每门课一次明细"的节奏会被拒。
                    # 当作可重试错误处理，退避更久。
                    last_err = HanboerError(f"被限流 HTTP {resp.status_code}")
                    log.warning(
                        "被限流(%s/%s) %s，退避后重试", attempt, self.max_retries, path
                    )
                    if attempt < self.max_retries:
                        import asyncio

                        await asyncio.sleep(min(3 * attempt, 15))
                        continue
                    raise last_err
                resp.raise_for_status()
                try:
                    payload = resp.json()
                except json.JSONDecodeError as exc:
                    raise HanboerError(f"返回非 JSON：{resp.text[:200]}") from exc
                code = payload.get("code")
                if code not in (0, "0", None):
                    raise HanboerError(f"接口返回错误 code={code} msg={payload.get('msg')}")
                return payload
            except HanboerError:
                raise
            except Exception as exc:  # 网络/超时
                last_err = exc
                log.warning("请求失败(%s/%s) %s: %s", attempt, self.max_retries, path, exc)
                if attempt < self.max_retries:
                    import asyncio

                    await asyncio.sleep(min(2**attempt, 8))
        raise HanboerError(f"请求 {path} 失败：{last_err}") from last_err

    async def _throttle(self) -> None:
        """全局最小请求间隔，避免触发服务端限流。"""
        import asyncio
        import time

        cls = type(self)
        if cls._rate_lock is None:
            cls._rate_lock = asyncio.Lock()
        async with cls._rate_lock:
            now = time.monotonic()
            wait = cls._MIN_INTERVAL - (now - cls._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_request_at = time.monotonic()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # verify 由 HB_VERIFY_TLS 控制：内网出口若有 HTTPS 解密设备，
            # 证书会被替换成自签的，默认校验会让采集一条都拿不到。
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                verify=settings.hb_verify_tls,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "HanboerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ── 业务接口 ──────────────────────────────────────────
    async def iter_statistics(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        course_name: str | None = None,
        sort: str | None = None,
        order: int | None = None,
        max_pages: int = 200,
    ) -> list[NormalizedCourse]:
        """获取考勤汇总（课程/录播计划维度）。pageSize 上限 50，自动翻页。"""
        out: list[NormalizedCourse] = []
        page = 1
        while page <= max_pages:
            params = {
                "pageNumber": page,
                "pageSize": MAX_PAGE_SIZE,
                "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S") if start_time else None,
                "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else None,
                "courseName": course_name,
                "sort": sort,
                "order": order,
            }
            payload = await self._get(STAT_PATH, params)
            rows = payload.get("data") or []
            for row in rows:
                out.append(self._to_course(row))
            total = int(payload.get("total") or 0)
            if len(rows) < MAX_PAGE_SIZE or page * MAX_PAGE_SIZE >= total:
                break
            page += 1
        return out

    async def iter_details(
        self,
        schedule_guid: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        student_only: bool = True,
        max_pages: int = 200,
    ) -> list[NormalizedEvent]:
        """获取考勤详情（学生维度）。不传 scheduleGuid 时按时间区间查。"""
        out: list[NormalizedEvent] = []
        page = 1
        while page <= max_pages:
            params = {
                "pageNumber": page,
                "pageSize": MAX_PAGE_SIZE,
                "scheduleGuid": schedule_guid,
                "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S") if start_time else None,
                "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else None,
            }
            payload = await self._get(DETAIL_PATH, params)
            rows = payload.get("data") or []
            for row in rows:
                ev = self._to_event(row, schedule_guid)
                if ev is None:
                    continue
                if student_only and ev.user_type != 2:
                    continue
                out.append(ev)
            total = int(payload.get("total") or 0)
            if len(rows) < MAX_PAGE_SIZE or page * MAX_PAGE_SIZE >= total:
                break
            page += 1
        return out

    # ── 字段映射 ──────────────────────────────────────────
    @staticmethod
    def _to_course(row: dict[str, Any]) -> NormalizedCourse:
        start = parse_dt(row.get("startTime"))
        end = parse_dt(row.get("endTime"))
        return NormalizedCourse(
            schedule_guid=str(row.get("scheduleGuid") or ""),
            source=Source.HANBOER.value,
            title=str(row.get("title") or ""),
            course_name=str(row.get("courseName") or ""),
            teacher_name=str(row.get("teacherName") or ""),
            classroom_name=str(row.get("classroomName") or ""),
            classroom_no=str(row.get("classroomNo") or ""),
            building_name=str(row.get("buildingName") or ""),
            week_name=str(row.get("weekName") or ""),
            node_name=str(row.get("nodeName") or ""),
            class_name=str(row.get("className") or ""),
            org_name=str(row.get("orgName") or ""),
            start_time=start,
            end_time=end,
            should_student_count=int(row.get("shouldStudentCount") or 0),
            attend_student_count=int(row.get("attendStudentCount") or 0),
            absent_student_count=int(row.get("absentStudentCount") or 0),
            late_student_count=int(row.get("lateStudentCount") or 0),
            leave_student_count=int(row.get("leaveStudentCount") or 0),
            attendance_rate=float(row.get("attendanceRate") or 0.0),
            raw=row,
        )

    @staticmethod
    def _to_event(row: dict[str, Any], schedule_guid: str | None) -> NormalizedEvent | None:
        student_no = str(row.get("userNo") or "").strip()
        attend_date = parse_date(row.get("attendDate"))
        if not student_no or attend_date is None:
            return None
        guid = str(row.get("scheduleGuid") or schedule_guid or "").strip()
        return NormalizedEvent(
            source=Source.HANBOER.value,
            schedule_guid=guid,
            student_no=student_no,
            class_date=attend_date,
            attend_status=str(row.get("attendStatus") or ""),
            sign_in_time=parse_dt(row.get("signInTime")),
            node_no=str(row.get("nodeNo") or ""),
            classroom_no=str(row.get("classroomNo") or ""),
            user_type=int(row.get("userType") or 2),
            # 实测接口还会返回 userName（文档未写），带上即可自动填姓名
            student_name=str(row.get("userName") or "").strip(),
            raw=row,
        )


def build_signed_url(base_url: str, path: str, params: dict[str, Any]) -> str:
    """调试用：生成一条带签名的完整 URL（人类可读，便于浏览器直接打开核对）。"""
    client = HanboerClient(base_url=base_url)
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    signed = {**clean, **client.build_sign(clean)}
    return f"{base_url.rstrip('/')}{path}?{urlencode(signed)}"
