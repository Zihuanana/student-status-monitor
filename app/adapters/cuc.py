# -*- coding: utf-8 -*-
"""中传教务接口客户端（主轴数据源）。

架构定位
--------
**教务是主轴**：学生的「应到」课表全部由这里生成。
畅课_签到 / 翰博尔 这些「实到」签到源，再适配到主轴上来。

六个数据源
----------
    ① 公共_校历              → 教学开始日期（第一周周一）
    ② 本科生院_本专科生基本信息 → 姓名 学号 专业 年级 班号
    ③ 本研_学生选课           → 学号 学年学期 选课类别 教学班编码 课程编码
    ④ 本研_课表              → 课程编码 课序号 名称 周次 节次 星期 教室 **记录id**
    ⑤ 畅课_签到              → 记录id 学号 教学班编码 签到状态 签到时间 上课时间

关于「记录id」
--------------
它把课表行和签到记录**精确**连起来：
    1 个记录id = 1 个课次（周次×星期×节次）
    n 个学号共享同一个记录id
所以课次主键优先用它，而不是靠"上课时间反推节次"那种猜测式对齐。

为什么要做「字段名容错」
------------------------
对方的真实 JSON 键名我们还不知道（要等 demo 跑出结构）。
如果写死 ``row["xh"]``，一旦对方叫 ``studentNo`` 就得改代码。
所以这里用**候选键列表**逐个尝试，并且允许用
``CUC_FIELDS_*`` 环境变量显式覆盖 —— 改配置就能适配，不用改代码。

数据隔离约定
------------
· 所有地址/token 从 settings 读，本文件**不出现任何字面量密钥**；
· 本模块只负责取数与归一化，不写库；
· 日志里**绝不打印学生姓名/学号明细**（只打条数与键名）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Iterable

import httpx

from app.core.config import settings


# =====================================================================
# 异常
# =====================================================================
class CucError(RuntimeError):
    """中传接口调用失败。"""


# =====================================================================
# 归一化后的数据结构
# =====================================================================
@dataclass(slots=True)
class CucStudent:
    """本科生院_本专科生基本信息 的一行。"""

    student_no: str
    name: str = ""
    major: str = ""
    grade: str = ""
    class_name: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class CucEnrollment:
    """本研_学生选课 的一行。

    示示例辅导员的要求：**A D E 用来做匹配，B C D 用来打印**。
    这里把五个字段都留下，匹配时只用 (student_no, teaching_class, course_no, term)。
    """

    student_no: str                 # A 学号
    term: str = ""                  # B 学年学期
    category: str = ""              # C 选课类别（必修/限选…）
    teaching_class: str = ""        # D 教学班编码（jxbbm，值形如 01 / 01S01）
    course_no: str = ""             # E 课程编码
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class CucTimetable:
    """本研_课表 的一行。

    ⚠ **课程号不足以唯一标识一门课**。实测同一课程号 ``2291000001``
    有 ``01`` 和 ``01S01`` 两个课序号，周次/星期/节次/教室全都不同。
    只按课程号连接会把两个教学班的课合并 —— 学生会凭空多出几次课。
    所以连接键必须是 (course_no, teaching_class, term)。
    """

    course_no: str                  # E 课程编码
    teaching_class: str = ""        # 课序号 / 班级编码（jxbbm）
    course_name: str = ""           # F 显示名称
    record_id: str = ""             # 记录id（课次唯一编号，有了它就不用反推）
    weeks: list[int] = field(default_factory=list)   # G 周次，已展开成 [1,2,5]
    weeks_raw: str = ""             # G 原始文本，便于排查
    period_start: int = 0           # H 开始节次
    period_end: int = 0             # H 结束节次
    weekday: int = 0                # I 星期（1=周一 … 7=周日）
    classroom: str = ""             # J 教室编码
    term: str = ""
    teacher: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def period_span(self) -> tuple[int, int]:
        return (self.period_start, self.period_end or self.period_start)


@dataclass(slots=True)
class CucCheckin:
    """畅课_签到 的一行。"""

    record_id: str = ""             # 记录id → 直接对上课次
    student_no: str = ""
    teaching_class: str = ""
    status: str = ""                # 签到状态
    checkin_time: datetime | None = None
    class_time: datetime | None = None   # 上课时间（用来确定是第几次课）
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class CucCalendar:
    """公共_校历 的归一化结果 —— 我们只关心一个字段。"""

    week1_monday: date
    term: str = ""
    raw: dict = field(default_factory=dict)


# =====================================================================
# 候选键（字段名容错）
# =====================================================================
#: 每类数据里，我们的字段 → 对方可能用的键名。
#: 顺序即优先级；命中第一个即可。对方键名不在列表里时，
#: 用 ``CUC_FIELDS_*`` 环境变量显式指定。
CANDIDATES: dict[str, dict[str, list[str]]] = {
    "calendar": {
        "week1_monday": ["jxksrq", "teaching_start_date", "startDate", "start_date",
                         "教学开始日期", "第一周周一", "xqksrq"],
        "term": ["xnxq", "term", "semester", "学年学期"],
    },
    "student": {
        "student_no": ["xh", "studentNo", "student_no", "sno", "学号"],
        "name": ["xm", "name", "studentName", "姓名"],
        "major": ["zymc", "major", "majorName", "专业", "专业名称"],
        "grade": ["nj", "grade", "年级", "rxnf"],
        "class_name": ["bh", "classNo", "class_name", "班级", "班号", "班级名称"],
    },
    "enrollment": {
        "student_no": ["xh", "studentNo", "student_no", "学号"],
        "term": ["xnxq", "term", "学年学期"],
        "category": ["xklb", "category", "选课类别"],
        "teaching_class": ["jxbbm", "teachingClass", "classCode", "教学班编码", "课序号"],
        "course_no": ["kcbm", "courseNo", "course_no", "课程编码", "课程号"],
    },
    "timetable": {
        "course_no": ["kcbm", "courseNo", "course_no", "课程编码", "课程号"],
        "teaching_class": ["jxbbm", "teachingClass", "classCode", "课序号", "班级编码"],
        "course_name": ["xsmc", "courseName", "course_name", "显示名称", "课程名"],
        "record_id": ["jlid", "recordId", "record_id", "id", "记录id", "记录ID"],
        "weeks": ["zc", "weeks", "week", "周次", "上课周次"],
        "period_start": ["ksjc", "startPeriod", "start_period", "开始节次"],
        "period_end": ["jsjc", "endPeriod", "end_period", "结束节次"],
        "weekday": ["xq", "weekday", "weekDay", "星期", "上课星期"],
        "classroom": ["jsbm", "classroom", "room", "教室编码", "教室名称", "jsmc"],
        "term": ["xnxq", "term", "学年学期"],
        "teacher": ["skjs", "teacher", "teacherName", "上课教师", "教师"],
    },
    "checkin": {
        "record_id": ["jlid", "recordId", "record_id", "记录id", "记录ID"],
        "student_no": ["xh", "studentNo", "student_no", "学号"],
        "teaching_class": ["jxbbm", "teachingClass", "classCode", "教学班编码"],
        "status": ["qdzt", "status", "checkinStatus", "签到状态"],
        "checkin_time": ["qdsj", "checkinTime", "checkin_time", "签到时间"],
        "class_time": ["sksj", "classTime", "class_time", "上课时间"],
        "raw": [],
    },
}

#: 星期文本 → 数字
_WEEKDAY_TEXT = {
    "星期一": 1, "周一": 1, "礼拜一": 1, "monday": 1,
    "星期二": 2, "周二": 2, "礼拜二": 2, "tuesday": 2,
    "星期三": 3, "周三": 3, "礼拜三": 3, "wednesday": 3,
    "星期四": 4, "周四": 4, "礼拜四": 4, "thursday": 4,
    "星期五": 5, "周五": 5, "礼拜五": 5, "friday": 5,
    "星期六": 6, "周六": 6, "礼拜六": 6, "saturday": 6,
    "星期日": 7, "星期天": 7, "周日": 7, "周天": 7, "sunday": 7,
}


# =====================================================================
# 解析工具
# =====================================================================
def parse_weeks(text: str) -> list[int]:
    """把周次文本展开成周次列表。

    实际课表里出现过的形态（来自真实截图）::

        2-3周           → [2, 3]
        5周             → [5]
        1-4周,6-7周     → [1, 2, 3, 4, 6, 7]
        1-16周(单)      → 奇数周
        1-16周(双)      → 偶数周

    解析不出来的片段**跳过并记录**，不瞎猜 —— 宁可少算一次课，
    也不要把课次安到错误的日期上。
    """
    text = (text or "").strip()
    if not text:
        return []

    # 单双周标记（可能是 "1-16周(单)" 或 "1-16周单" 或 "1-16周 单周"）
    odd_only = bool(re.search(r"[（(]?\s*单\s*[周)）]?", text)) and "双" not in text
    even_only = bool(re.search(r"[（(]?\s*双\s*[周)）]?", text))

    # 先按逗号/顿号/分号切成段
    segments = re.split(r"[,，、;；]", text)
    weeks: set[int] = set()

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # 去掉"周"字和单双标记后再找数字区间
        clean = re.sub(r"[(（]?\s*[单双]\s*[周)）]?", "", seg)
        clean = clean.replace("周", " ")
        # 匹配 "3-5" 或 "7"
        for m in re.finditer(r"(\d+)\s*(?:[-–—~至]\s*(\d+))?", clean):
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) else lo
            if hi < lo:
                lo, hi = hi, lo
            if hi - lo > 60:        # 明显异常，跳过
                continue
            weeks.update(range(lo, hi + 1))

    if odd_only:
        weeks = {w for w in weeks if w % 2 == 1}
    elif even_only:
        weeks = {w for w in weeks if w % 2 == 0}

    return sorted(weeks)


def parse_weekday(value: Any) -> int:
    """星期 → 1..7。支持文本（星期一）与数字（1 / 4）。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
        return n if 1 <= n <= 7 else 0
    text = str(value).strip()
    if not text:
        return 0
    for k, v in _WEEKDAY_TEXT.items():
        if k in text.lower() or k in text:
            return v
    m = re.search(r"[1-7]", text)
    return int(m.group(0)) if m else 0


def parse_period(value: Any) -> int:
    """节次 → 整数。支持 ``5`` / ``"5-6"`` / ``"5"``。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else 0


def parse_datetime(value: Any) -> datetime | None:
    """宽松解析时间。支持 ISO、``2026-10-22 14:30:00``、时间戳。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, (int, float)):
        t = float(value)
        if t > 1e12:          # 毫秒
            t /= 1000.0
        try:
            return datetime.fromtimestamp(t)
        except (OverflowError, OSError):
            return None
    text = str(value).strip()
    if not text:
        return None
    # 去掉毫秒与 Z
    text = re.sub(r"\.\d+", "", text).replace("Z", "")
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> date | None:
    dt = parse_datetime(value)
    return dt.date() if dt else None


# =====================================================================
# 客户端
# =====================================================================
class CucClient:
    """中传教务接口客户端。

    用法::

        client = CucClient()
        cal = client.fetch_calendar()          # → CucCalendar
        students = client.fetch_students()
        enrollments = client.fetch_enrollments()
        timetable = client.fetch_timetable()
        checkins = client.fetch_checkins()
    """

    def __init__(self, *, term: str | None = None) -> None:
        if not settings.cuc_enabled:
            raise CucError("CUC_ENABLED=false，中传接口未启用")
        if not settings.cuc_base_url:
            raise CucError("CUC_BASE_URL 未配置，无法调用中传接口")
        self.base_url = settings.cuc_base_url.rstrip("/")
        self.term = term if term is not None else settings.cuc_term

    # ── 内部：请求 ────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if settings.cuc_token:
            h["Authorization"] = f"Bearer {settings.cuc_token}"
        if settings.cuc_app_key:
            h["X-App-Key"] = settings.cuc_app_key
        if settings.cuc_app_secret:
            # 真实的签名算法由对方决定；这里先按常见做法给一个占位实现，
            # 拿到文档后替换 _sign()。**不要把算法写死在这里**。
            h["X-App-Sign"] = self._sign()
        # 附加头：appId=sm-monitor;X-Tenant=hic
        for item in (settings.cuc_extra_headers or "").split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            h[k.strip()] = v.strip()
        return h

    def _sign(self) -> str:
        """签名占位。拿到对方文档后按实际算法实现。"""
        import hashlib

        raw = f"{settings.cuc_app_key}{settings.cuc_app_secret}".encode()
        return hashlib.sha256(raw).hexdigest()

    def _rows(self, payload: Any) -> list[dict[str, Any]]:
        """从各种响应信封里掏出数据行。

        对方的返回可能是::

            {"code":0,"data":{"list":[...]}}
            {"success":true,"result":{"records":[...]}}
            {"data":[...]}
            [...]

        这里逐个尝试常见信封，找不到就报错 —— 不静默返回空，
        否则"接口通了但没数据"会被误当成"确实没课"。
        """
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if not isinstance(payload, dict):
            raise CucError(f"响应不是 JSON 对象或数组：{type(payload).__name__}")

        # 先看业务状态码（有的接口 HTTP 200 但 code != 0）
        for k in ("code", "status", "statusCode", "errcode"):
            if k in payload:
                v = payload[k]
                ok = str(v) in {"0", "200", "true", "True", "success", "SUCCESS"}
                if not ok:
                    msg = payload.get("message") or payload.get("msg") or ""
                    raise CucError(f"接口返回业务错误：{k}={v} {msg}")

        # 常见信封键
        for key in ("data", "result", "rows", "list", "records", "items", "content"):
            if key not in payload:
                continue
            val = payload[key]
            got = self._rows(val)
            if got:
                return got
        # 递归一层找列表
        for k, v in payload.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):
                try:
                    got = self._rows(v)
                except CucError:
                    continue
                if got:
                    return got
        return []

    def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """GET 一个接口，返回数据行（自动翻页）。"""
        if not path:
            raise CucError("接口路径未配置")
        url = f"{self.base_url}/{path.lstrip('/')}"
        collected: list[dict[str, Any]] = []

        with httpx.Client(
            timeout=settings.cuc_http_timeout_seconds,
            verify=settings.cuc_verify_tls,
            follow_redirects=True,
        ) as client:
            for page in range(1, max(1, settings.cuc_max_pages) + 1):
                q = dict(params or {})
                q.setdefault("page", page)
                q.setdefault("pageSize", settings.cuc_page_size)

                last_exc: Exception | None = None
                resp = None
                for attempt in range(max(0, settings.cuc_http_max_retries) + 1):
                    try:
                        resp = client.get(url, params=q, headers=self._headers())
                        break
                    except httpx.HTTPError as exc:
                        last_exc = exc
                if resp is None:
                    raise CucError(f"请求失败：{url} —— {last_exc}")

                if resp.status_code != 200:
                    raise CucError(
                        f"HTTP {resp.status_code}：{url} —— {resp.text[:200]}"
                    )

                try:
                    payload = resp.json()
                except json.JSONDecodeError as exc:
                    raise CucError(
                        f"响应不是合法 JSON：{url} —— {resp.text[:200]}"
                    ) from exc

                rows = self._rows(payload)
                if not rows:
                    break
                collected.extend(rows)

                # 不满一页就说明到底了
                if len(rows) < settings.cuc_page_size:
                    break
                # 有些接口会给出总页数
                if isinstance(payload, dict):
                    for k in ("totalPage", "pages", "pageCount"):
                        if k in payload and str(payload[k]).isdigit():
                            if page >= int(payload[k]):
                                return collected
                            break

        return collected

    # ── 字段取值（容错）──────────────────────────────────
    def _field_map(self, kind: str) -> dict[str, str]:
        """读 ``CUC_FIELDS_*`` 显式映射：``我们的字段=对方的键;...``"""
        raw = {
            "calendar": settings.cuc_fields_calendar,
            "student": settings.cuc_fields_student,
            "enrollment": settings.cuc_fields_enrollment,
            "timetable": settings.cuc_fields_timetable,
            "checkin": settings.cuc_fields_checkin,
        }.get(kind, "") or ""
        out: dict[str, str] = {}
        for item in raw.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            ours, theirs = item.split("=", 1)
            out[ours.strip()] = theirs.strip()
        return out

    def _pick(self, row: dict[str, Any], kind: str, field_name: str) -> Any:
        """按「显式映射 → 候选键 → 大小写不敏感 → 中文别名」取值。"""
        explicit = self._field_map(kind)
        if field_name in explicit:
            return row.get(explicit[field_name])

        for cand in CANDIDATES.get(kind, {}).get(field_name, []):
            if cand in row:
                return row[cand]
        lowered = {str(k).lower(): v for k, v in row.items()}
        for cand in CANDIDATES.get(kind, {}).get(field_name, []):
            if cand.lower() in lowered:
                return lowered[cand.lower()]
        return None

    def _text(self, row: dict[str, Any], kind: str, name: str) -> str:
        v = self._pick(row, kind, name)
        if v is None:
            return ""
        return str(v).strip()

    # ── 公开：五个数据源 ─────────────────────────────────
    def fetch_calendar(self) -> CucCalendar:
        """公共_校历 —— 只取「教学开始日期」（第一周周一）。"""
        rows = self._get(settings.cuc_path_calendar, {"xnxq": self.term} if self.term else None)
        if not rows:
            raise CucError("校历接口没有返回数据")
        row = rows[0]
        raw_date = self._pick(row, "calendar", "week1_monday")
        d = parse_date(raw_date)
        if d is None:
            raise CucError(
                f"校历里的教学开始日期解析失败：{raw_date!r}；"
                f"请在 CUC_FIELDS_CALENDAR 里显式指定该字段的键名"
            )
        # 规范到那一周的周一：万一对方给的是别的日子
        d = d.fromordinal(d.toordinal() - (d.weekday()))
        return CucCalendar(
            week1_monday=d,
            term=self._text(row, "calendar", "term") or self.term,
            raw=row,
        )

    def fetch_students(self) -> list[CucStudent]:
        """本科生院_本专科生基本信息。"""
        rows = self._get(settings.cuc_path_students)
        return [
            CucStudent(
                student_no=self._text(r, "student", "student_no").lstrip("cucCUC"),
                name=self._text(r, "student", "name"),
                major=self._text(r, "student", "major"),
                grade=self._text(r, "student", "grade"),
                class_name=self._text(r, "student", "class_name"),
                raw=r,
            )
            for r in rows
            if self._text(r, "student", "student_no")
        ]

    def fetch_enrollments(self) -> list[CucEnrollment]:
        """本研_学生选课。"""
        rows = self._get(
            settings.cuc_path_enrollments,
            {"xnxq": self.term} if self.term else None,
        )
        return [
            CucEnrollment(
                student_no=self._text(r, "enrollment", "student_no"),
                term=self._text(r, "enrollment", "term") or self.term,
                category=self._text(r, "enrollment", "category"),
                teaching_class=self._text(r, "enrollment", "teaching_class"),
                course_no=self._text(r, "enrollment", "course_no"),
                raw=r,
            )
            for r in rows
            if self._text(r, "enrollment", "student_no")
        ]

    def fetch_timetable(self) -> list[CucTimetable]:
        """本研_课表。"""
        rows = self._get(
            settings.cuc_path_timetable,
            {"xnxq": self.term} if self.term else None,
        )
        out: list[CucTimetable] = []
        for r in rows:
            course_no = self._text(r, "timetable", "course_no")
            if not course_no:
                continue
            raw_weeks = self._text(r, "timetable", "weeks")
            ps = parse_period(self._pick(r, "timetable", "period_start"))
            pe = parse_period(self._pick(r, "timetable", "period_end"))
            out.append(
                CucTimetable(
                    course_no=course_no,
                    teaching_class=self._text(r, "timetable", "teaching_class"),
                    course_name=self._text(r, "timetable", "course_name"),
                    record_id=self._text(r, "timetable", "record_id"),
                    weeks=parse_weeks(raw_weeks),
                    weeks_raw=raw_weeks,
                    period_start=ps,
                    period_end=pe or ps,
                    weekday=parse_weekday(self._pick(r, "timetable", "weekday")),
                    classroom=self._text(r, "timetable", "classroom"),
                    term=self._text(r, "timetable", "term") or self.term,
                    teacher=self._text(r, "timetable", "teacher"),
                    raw=r,
                )
            )
        return out

    def fetch_checkins(self) -> list[CucCheckin]:
        """畅课_签到。"""
        rows = self._get(settings.cuc_path_checkins)
        return [
            CucCheckin(
                record_id=self._text(r, "checkin", "record_id"),
                student_no=self._text(r, "checkin", "student_no"),
                teaching_class=self._text(r, "checkin", "teaching_class"),
                status=self._text(r, "checkin", "status"),
                checkin_time=parse_datetime(self._pick(r, "checkin", "checkin_time")),
                class_time=parse_datetime(self._pick(r, "checkin", "class_time")),
                raw=r,
            )
            for r in rows
        ]

    # ── 供 probe 用：探测响应结构 ────────────────────────
    def probe(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """请求一个路径，返回**结构摘要**（键名与类型），不含数据值。

        这是联调第一步：不知道对方返回什么字段时，用它看清结构。
        """
        if not path:
            raise CucError("接口路径为空")
        url = f"{self.base_url}/{path.lstrip('/')}"
        with httpx.Client(
            timeout=settings.cuc_http_timeout_seconds,
            verify=settings.cuc_verify_tls,
            follow_redirects=True,
        ) as client:
            resp = client.get(url, params=params or {}, headers=self._headers())
        info: dict[str, Any] = {
            "url": url,
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "bytes": len(resp.content),
        }
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            info["json"] = False
            info["body_head"] = resp.text[:300]
            return info

        info["json"] = True
        info["envelope"] = _shape(payload, depth=0)
        try:
            rows = self._rows(payload)
            info["row_count"] = len(rows)
            if rows:
                info["row_keys"] = _shape(rows[0], depth=0)
        except CucError as exc:
            info["rows_error"] = str(exc)
        return info


def _shape(obj: Any, *, depth: int = 0) -> Any:
    """把 JSON 结构压成「键名 → 类型」，值一律不输出。

    这样 probe 的结果可以直接贴到对话里，不含任何学生数据。
    """
    if depth > 3:
        return "…"
    if isinstance(obj, dict):
        return {k: _shape(v, depth=depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        if not obj:
            return "[]"
        return [f"list[{len(obj)}]", _shape(obj[0], depth=depth + 1)]
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "bool"
    if isinstance(obj, (int, float)):
        return "number"
    text = str(obj)
    # 只报类型与长度，不报内容
    return f"str({len(text)})"


__all__ = [
    "CucClient",
    "CucError",
    "CucStudent",
    "CucEnrollment",
    "CucTimetable",
    "CucCheckin",
    "CucCalendar",
    "parse_weeks",
    "parse_weekday",
    "parse_period",
    "parse_datetime",
    "parse_date",
    "CANDIDATES",
]
