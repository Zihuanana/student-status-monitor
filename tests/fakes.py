# -*- coding: utf-8 -*-
"""测试辅助：极简的假 SMTP 服务器 + 假考勤接口服务器。

为什么需要
----------
系统里有两条链路从未被真正运行过：
  1. **真实采集链路**：从 HTTP 考勤接口带签名拉数据（此前只有 mock 内存数据源）；
  2. **邮件投递链路**：SMTP 登录并发信（此前只验证过服务器能连通 465 端口）。

这两条恰恰是最容易在拿到真实凭据后才炸掉的地方。这里用本地假服务器把
它们完整跑一遍，避免"接入当天才发现签名不对/邮件发不出"。

Python 3.12 起标准库移除了 ``smtpd``，所以这里用 ``socketserver`` 手写一个
只实现发信所需命令（EHLO/STARTTLS/AUTH/MAIL/RCPT/DATA/QUIT）的最小服务器。
"""

from __future__ import annotations

import base64
import json
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# ─────────────────────────────────────────────────────────────
# 假 SMTP 服务器
# ─────────────────────────────────────────────────────────────


@dataclass
class MailMessage:
    mail_from: str = ""
    rcpt_to: list[str] = field(default_factory=list)
    data: str = ""
    authenticated: bool = False
    auth_user: str = ""
    auth_password: str = ""


class _SMTPHandler(socketserver.StreamRequestHandler):
    """只实现发信所需的最小 SMTP 会话。"""

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("utf-8"))
        self.wfile.flush()

    def handle(self) -> None:  # noqa: C901 - 简单状态机
        msg = MailMessage()
        self._send("220 fake-smtp ready")
        in_data = False
        buffer: list[str] = []

        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")

            if in_data:
                if line == ".":
                    in_data = False
                    msg.data = "\n".join(buffer)
                    buffer = []
                    self.server.messages.append(msg)  # type: ignore[attr-defined]
                    msg = MailMessage()
                    self._send("250 OK queued")
                else:
                    # SMTP 点转义：行首的 ".." 还原为 "."
                    buffer.append(line[1:] if line.startswith("..") else line)
                continue

            upper = line.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                self._send("250-fake-smtp")
                self._send("250-AUTH LOGIN PLAIN")
                self._send("250 8BITMIME")
            elif upper.startswith("AUTH LOGIN"):
                self._send("334 " + base64.b64encode(b"Username:").decode())
                user = base64.b64decode(self.rfile.readline().strip()).decode("utf-8", "replace")
                self._send("334 " + base64.b64encode(b"Password:").decode())
                pwd = base64.b64decode(self.rfile.readline().strip()).decode("utf-8", "replace")
                msg.authenticated = True
                msg.auth_user = user
                msg.auth_password = pwd
                self._send("235 Authentication successful")
            elif upper.startswith("AUTH PLAIN"):
                parts = line.split()
                if len(parts) > 2:
                    decoded = base64.b64decode(parts[2]).decode("utf-8", "replace")
                    bits = decoded.split("\x00")
                    if len(bits) >= 3:
                        msg.authenticated = True
                        msg.auth_user = bits[1]
                        msg.auth_password = bits[2]
                self._send("235 Authentication successful")
            elif upper.startswith("MAIL FROM"):
                msg.mail_from = line.split(":", 1)[1].strip().strip("<>")
                self._send("250 OK")
            elif upper.startswith("RCPT TO"):
                msg.rcpt_to.append(line.split(":", 1)[1].strip().strip("<>"))
                self._send("250 OK")
            elif upper.startswith("DATA"):
                self._send("354 End data with <CR><LF>.<CR><LF>")
                in_data = True
            elif upper.startswith("QUIT"):
                self._send("221 Bye")
                break
            elif upper.startswith("RSET"):
                msg = MailMessage()
                self._send("250 OK")
            else:
                self._send("250 OK")


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeSMTPServer:
    """上下文管理器：``with FakeSMTPServer() as smtp: ... smtp.messages``"""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._server = _ThreadedTCPServer((host, 0), _SMTPHandler)
        self._server.messages = []  # type: ignore[attr-defined]
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def messages(self) -> list[MailMessage]:
        return self._server.messages  # type: ignore[attr-defined]

    def __enter__(self) -> "FakeSMTPServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


# ─────────────────────────────────────────────────────────────
# 假翰博尔考勤接口服务器
# ─────────────────────────────────────────────────────────────


@dataclass
class FakeCourse:
    schedule_guid: str
    course_name: str
    class_name: str
    start: datetime
    end: datetime
    room: str = "Q3101"


@dataclass
class FakeRecord:
    user_no: str
    attend_status: str
    sign_in_time: datetime | None = None


class FakeHanboerServer:
    """模拟翰博尔两个开放接口。

    - ``GET /attendance/api/v1/openApi/getAttendanceStatistics``
    - ``GET /attendance/api/v1/openApi/getAttendanceDetails``

    严格复刻文档里的行为：必须携带 sign/timeStamp/appSource/accessKey；
    ``pageSize`` 上限 50；分页返回 ``total``；``data`` 为数组。
    """

    PAGE_SIZE_CAP = 50

    def __init__(self, courses: list[FakeCourse], records: dict[str, list[FakeRecord]]) -> None:
        self.courses = courses
        self.records = records  # schedule_guid -> 记录
        self.requests: list[dict[str, Any]] = []  # 便于断言签名与分页

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # 静音
                return

            def _json(self, payload: dict[str, Any], code: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                outer.requests.append({"path": parsed.path, "params": params})

                # —— 鉴权校验：签名参数必须齐全 ——
                for required in ("sign", "timeStamp", "appSource", "accessKey"):
                    if not params.get(required):
                        self._json({"code": 1, "msg": f"缺少签名参数 {required}"})
                        return
                if len(params["sign"]) != 32 or params["sign"] != params["sign"].upper():
                    self._json({"code": 1, "msg": "签名格式非法"})
                    return

                page = int(params.get("pageNumber") or 1)
                size = min(int(params.get("pageSize") or 50), outer.PAGE_SIZE_CAP)

                if parsed.path.endswith("getAttendanceStatistics"):
                    rows = [outer._course_row(c) for c in outer.courses]
                elif parsed.path.endswith("getAttendanceDetails"):
                    guid = params.get("scheduleGuid")
                    if guid:
                        rows = [outer._record_row(guid, r) for r in outer.records.get(guid, [])]
                    else:
                        rows = [
                            outer._record_row(g, r)
                            for g, rs in outer.records.items()
                            for r in rs
                        ]
                else:
                    self._json({"code": 1, "msg": "未知接口"})
                    return

                start = (page - 1) * size
                chunk = rows[start : start + size]
                self._json(
                    {
                        "code": 0,
                        "msg": None,
                        "data": chunk,
                        "stamp": int(datetime.now().timestamp() * 1000),
                        "total": len(rows),
                    }
                )

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.host, self.port = self._httpd.server_address
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _course_row(self, c: FakeCourse) -> dict[str, Any]:
        return {
            "scheduleGuid": c.schedule_guid,
            "classroomName": c.room,
            "classroomNo": c.room,
            "teacherName": "0107t1",
            "courseName": c.course_name,
            "buildingName": "逸夫楼",
            "weekName": f"第{c.start.isocalendar().week}周",
            "className": c.class_name,
            "orgName": "海南国际学院",
            "nodeName": "第一节",
            "createTime": c.start.strftime("%Y-%m-%d %H:%M:%S"),
            "startTime": c.start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": c.end.strftime("%Y-%m-%d %H:%M:%S"),
            "title": f"{c.room}{c.course_name}",
            "lateStudentCount": 0,
            "leaveStudentCount": 0,
            "shouldStudentCount": 10,
            "attendStudentCount": sum(
                1 for r in self.records.get(c.schedule_guid, []) if r.attend_status != "缺勤"
            ),
            "absentStudentCount": sum(
                1 for r in self.records.get(c.schedule_guid, []) if r.attend_status == "缺勤"
            ),
            "attendanceRate": 0.9,
        }

    def _record_row(self, guid: str, r: FakeRecord) -> dict[str, Any]:
        course = next((c for c in self.courses if c.schedule_guid == guid), None)
        return {
            "userNo": r.user_no,
            "attendDate": (course.start.date() if course else date.today()).isoformat(),
            "attendStatus": r.attend_status,
            "nodeNo": "第一节",
            "classroomNo": course.room if course else "Q3101",
            "signInTime": (
                r.sign_in_time.strftime("%Y-%m-%d %H:%M:%S") if r.sign_in_time else None
            ),
            "userType": 2,
        }

    def __enter__(self) -> "FakeHanboerServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def build_week_fixture(
    students: list[str],
    now: datetime,
    days_with_class: int = 5,
    absent_students: set[str] | None = None,
) -> tuple[list[FakeCourse], dict[str, list[FakeRecord]]]:
    """构造一个可复现的课表 + 签到数据。

    ``absent_students`` 里的学生**完全不出现**在考勤记录里（复刻真实系统
    「整节课没来的人不在返回列表中」的行为）。
    """
    absent_students = absent_students or set()
    courses: list[FakeCourse] = []
    records: dict[str, list[FakeRecord]] = {}

    day = now.date() - timedelta(days=days_with_class - 1)
    idx = 0
    while len(courses) < days_with_class:
        if day.weekday() < 5:  # 周末不排课
            start = datetime.combine(day, datetime.min.time()).replace(hour=8)
            end = start + timedelta(hours=1, minutes=35)
            guid = f"fake-{day:%Y%m%d}"
            courses.append(
                FakeCourse(
                    schedule_guid=guid,
                    course_name="高等数学",
                    class_name="智能科学与技术241班",
                    start=start,
                    end=end,
                )
            )
            records[guid] = [
                FakeRecord(
                    user_no=s,
                    attend_status="出勤",
                    sign_in_time=start + timedelta(minutes=3),
                )
                for s in students
                if s not in absent_students
            ]
        day += timedelta(days=1)
        idx += 1
        if idx > 60:
            break

    return courses, records


# ─────────────────────────────────────────────────────────────
# 时间推进场景（多日演练用）
# ─────────────────────────────────────────────────────────────


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _in_window(
    moment: datetime, start: datetime | None, end: datetime | None
) -> bool:
    """真实接口按 startTime/endTime 过滤，假服务器必须同样遵守。"""
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True


@dataclass
class StudentScenario:
    """一名学生在演练期间的行为。

    ``attendance`` 按「第几天」给值（1 起）：
      True=出勤且正常签到；False=整日缺勤（**完全不出现在接口返回里**）；
      "leave"=请假；None=当天没课。
    """

    student_no: str
    name: str
    attendance: list[bool | str | None]

    def state_on(self, day_index: int) -> bool | str | None:
        if 1 <= day_index <= len(self.attendance):
            return self.attendance[day_index - 1]
        return None


class TimelineFakeServer:
    """随时间推进而"长大"的假考勤服务器。

    每次请求都返回**截至该时刻已结束的课程**，以及相应学生的签到记录 ——
    复刻真实系统"上课上到一半时查不到结果、课后才有记录"的行为。
    缺席学生完全不出现在记录里（这是最容易被漏掉的一类）。
    """

    def __init__(self, scenarios: list[StudentScenario], periods: int = 1) -> None:
        self.scenarios = scenarios
        self.periods = periods  # 每天几节课
        self.now: datetime = datetime.now()
        #: 由演练驱动：把"模拟当天"告诉假服务器，避免跨午夜导致 class_date 错位
        self.current_day: date | None = None
        self.requests: list[dict[str, Any]] = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # 静音
                return

            def _json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                outer.requests.append({"path": parsed.path, "params": params})
                for required in ("sign", "timeStamp", "appSource", "accessKey"):
                    if not params.get(required):
                        self._json({"code": 1, "msg": f"缺少签名参数 {required}"})
                        return

                # 必须遵守 startTime/endTime 过滤 —— 真实接口就是这样，
                # 否则每一轮都会把之前几天的课重新采一遍（时间推进演练会失真）。
                window_start = _parse_dt(params.get("startTime"))
                window_end = _parse_dt(params.get("endTime"))

                if parsed.path.endswith("getAttendanceStatistics"):
                    rows = [
                        outer._course_row(c)
                        for c in outer._courses()
                        if _in_window(c.start, window_start, window_end)
                    ]
                elif parsed.path.endswith("getAttendanceDetails"):
                    guid = params.get("scheduleGuid")
                    rows = [
                        outer._record_row(g, r)
                        for g in outer._detail_guids(guid)
                        if _in_window(
                            next(
                                (c.start for c in outer._courses() if c.schedule_guid == g),
                                outer.now,
                            ),
                            window_start,
                            window_end,
                        )
                        for r in outer._records(g)
                    ]
                else:
                    self._json({"code": 1, "msg": "未知接口"})
                    return

                page = int(params.get("pageNumber") or 1)
                size = min(int(params.get("pageSize") or 50), 50)
                start = (page - 1) * size
                self._json(
                    {
                        "code": 0,
                        "msg": None,
                        "data": rows[start : start + size],
                        "stamp": int(outer.now.timestamp() * 1000),
                        "total": len(rows),
                    }
                )

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.host, self.port = self._httpd.server_address
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # —— 时间推进 ——
    def advance_to(self, moment: datetime, day_index: int) -> None:
        self.now = moment
        self.current_day = moment.date()
        self._day_index = day_index

    def _day_index_for(self, day: date) -> int:
        # 演练第 1 天 = 起始日；这里用 current_day 反推
        return getattr(self, "_day_index", 1)

    # —— 课程与记录 ——
    def _courses(self) -> list[FakeCourse]:
        """返回截至 self.now 已开始的课程。"""
        out: list[FakeCourse] = []
        today = self.current_day or self.now.date()
        for p in range(self.periods):
            start = datetime.combine(today, datetime.min.time()).replace(hour=8 + p * 2)
            if start > self.now:
                continue
            out.append(
                FakeCourse(
                    schedule_guid=f"tl-{today:%Y%m%d}-{p}",
                    course_name=f"课程{p}",
                    class_name="智能241班",
                    start=start,
                    end=start + timedelta(hours=1, minutes=35),
                )
            )
        return out

    def _detail_guids(self, guid: str | None) -> list[str]:
        if guid:
            return [guid]
        return [c.schedule_guid for c in self._courses()]

    def _records(self, guid: str) -> list[FakeRecord]:
        """该节课的签到记录：已结束才产出；缺席学生完全不出现。"""
        course = next((c for c in self._courses() if c.schedule_guid == guid), None)
        if course is None or course.end > self.now:
            return []  # 课还没结束，接口没有结果
        idx = self._day_index_for(course.start.date())
        out: list[FakeRecord] = []
        for sc in self.scenarios:
            behavior = sc.state_on(idx)
            if behavior is None or behavior is False:
                continue  # 没课 / 缺勤 → 不在返回里
            status = "请假" if behavior == "leave" else "出勤"
            out.append(
                FakeRecord(
                    user_no=sc.student_no,
                    attend_status=status,
                    sign_in_time=None if status == "请假" else course.start + timedelta(minutes=3),
                )
            )
        return out

    def _course_row(self, c: FakeCourse) -> dict[str, Any]:
        return {
            "scheduleGuid": c.schedule_guid,
            "classroomName": c.room,
            "classroomNo": c.room,
            "teacherName": "0107t1",
            "courseName": c.course_name,
            "buildingName": "逸夫楼",
            "weekName": "第1周",
            "className": c.class_name,
            "orgName": "海南国际学院",
            "nodeName": "第一节",
            "createTime": c.start.strftime("%Y-%m-%d %H:%M:%S"),
            "startTime": c.start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": c.end.strftime("%Y-%m-%d %H:%M:%S"),
            "title": f"{c.room}{c.course_name}",
            "lateStudentCount": 0,
            "leaveStudentCount": 0,
            "shouldStudentCount": len(self.scenarios),
            "attendStudentCount": len(self._records(c.schedule_guid)),
            "absentStudentCount": len(self.scenarios) - len(self._records(c.schedule_guid)),
            "attendanceRate": 1.0,
        }

    def _record_row(self, guid: str, r: FakeRecord) -> dict[str, Any]:
        course = next((c for c in self._courses() if c.schedule_guid == guid), None)
        return {
            "userNo": r.user_no,
            "attendDate": (course.start.date() if course else self.now.date()).isoformat(),
            "attendStatus": r.attend_status,
            "nodeNo": "第一节",
            "classroomNo": course.room if course else "Q3101",
            "signInTime": r.sign_in_time.strftime("%Y-%m-%d %H:%M:%S") if r.sign_in_time else None,
            "userType": 2,
        }

    def __enter__(self) -> "TimelineFakeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


_ = date
