# -*- coding: utf-8 -*-
"""采集体检与"系统自己坏了"告警的测试。

最危险的故障不是采到异常学生，而是**采集本身挂了却没人知道** ——
页面会一直显示几小时前的旧状态（看起来"全员正常"）。
本模块验证体检判定与告警邮件。
"""

from __future__ import annotations

import os
import tempfile

from tests import dbguard  # noqa: E402

# 必须在导入 app.* 之前隔离数据库 —— app.core.db 的引擎在首个导入者处
# 就已绑定，晚设置会连到工作区真实库（曾把演示数据清空）。
_DB_PATH = dbguard.isolate("health")
os.environ["RUN_BACKGROUND_JOBS"] = "false"

import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Alert,
    AppSetting,
    AttendanceDaily,
    AttendanceEvent,
    CollectRun,
    Course,
    Student,
    StudentCourse,
)
from app.services.health import (  # noqa: E402
    alert_if_unhealthy,
    consecutive_failures,
    health_report,
    last_success_age_minutes,
    should_check,
)
from tests.fakes import FakeSMTPServer  # noqa: E402

NOW = datetime(2026, 3, 11, 15, 0, 0)


class TestCollectHealth(unittest.TestCase):
    _MUTATED = (
        "alert_email_enabled", "alert_cooldown_hours", "smtp_host", "smtp_port",
        "smtp_use_ssl", "smtp_user", "smtp_password", "smtp_from",
        "alert_recipients", "poll_interval_seconds", "data_mode",
        "collect_enabled", "run_background_jobs",
    )

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = {n: getattr(settings, n) for n in cls._MUTATED}

    def setUp(self) -> None:
        for n, v in self._saved.items():
            setattr(settings, n, v)
        settings.poll_interval_seconds = 300
        settings.alert_email_enabled = False
        settings.collect_enabled = True
        settings.run_background_jobs = True
        self.db = SessionLocal()
        self.db.query(CollectRun).delete()
        self.db.query(AppSetting).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        for n, v in self._saved.items():
            setattr(settings, n, v)

    def _add_run(self, ok: bool, minutes_ago: float, message: str = "") -> None:
        self.db.add(
            CollectRun(
                source="all",
                job="poll",
                started_at=NOW - timedelta(minutes=minutes_ago),
                finished_at=NOW - timedelta(minutes=minutes_ago) + timedelta(seconds=5),
                ok=ok,
                message=message,
            )
        )
        self.db.commit()

    # ── 判定 ────────────────────────────────────────────
    def test_healthy_when_recent_success(self) -> None:
        self._add_run(True, minutes_ago=4)
        report = health_report(self.db, now=NOW)
        self.assertFalse(report["unhealthy"])
        self.assertEqual(report["consecutive_failures"], 0)
        self.assertLess(report["last_success_age_minutes"], 15)

    def test_consecutive_failures_counted_from_latest(self) -> None:
        for i in range(3):
            self._add_run(False, minutes_ago=i + 1)
        self._add_run(True, minutes_ago=10)  # 更早的一次成功
        self.assertEqual(consecutive_failures(self.db), 3)

    def test_success_resets_failure_streak(self) -> None:
        self._add_run(False, minutes_ago=1)
        self._add_run(True, minutes_ago=2)
        self._add_run(False, minutes_ago=3)
        self.assertEqual(consecutive_failures(self.db), 1)

    def test_three_failures_is_unhealthy(self) -> None:
        for i in range(3):
            self._add_run(False, minutes_ago=i + 1)
        self.assertTrue(health_report(self.db, now=NOW)["unhealthy"])

    def test_stale_data_is_unhealthy_even_without_failures(self) -> None:
        """没有失败记录，但很久没成功过（例如服务刚重启后一直连不上）也要报。"""
        self._add_run(True, minutes_ago=120)
        report = health_report(self.db, now=NOW)
        self.assertTrue(report["stale"])
        self.assertTrue(report["unhealthy"])

    def test_never_succeeded_is_unhealthy(self) -> None:
        report = health_report(self.db, now=NOW)
        self.assertIsNone(report["last_success_age_minutes"])
        self.assertTrue(report["stale"])
        self.assertTrue(report["unhealthy"])

    def test_last_success_age(self) -> None:
        self._add_run(True, minutes_ago=30)
        self.assertAlmostEqual(last_success_age_minutes(self.db, now=NOW), 30, places=1)

    def test_should_check_throttles(self) -> None:
        self.assertTrue(should_check(NOW, None))
        self.assertFalse(should_check(NOW, NOW - timedelta(minutes=5)))
        self.assertTrue(should_check(NOW, NOW - timedelta(minutes=31)))

    # ── 告警 ────────────────────────────────────────────
    def test_email_sent_when_unhealthy(self) -> None:
        for i in range(3):
            self._add_run(False, minutes_ago=i + 1, message="连接被拒绝")

        with FakeSMTPServer() as smtp:
            settings.alert_email_enabled = True
            settings.smtp_host = smtp.host
            settings.smtp_port = smtp.port
            settings.smtp_use_ssl = False
            settings.smtp_user = "monitor@example.com"
            settings.smtp_password = "code"
            settings.smtp_from = "monitor@example.com"
            settings.alert_recipients = "teacher@example.com"

            sent = alert_if_unhealthy(self.db, now=NOW)

        self.assertTrue(sent, "采集不健康时应发出告警邮件")
        self.assertEqual(len(smtp.messages), 1)
        # 正文是 base64 编码的 UTF-8，需解码后再断言
        from email import message_from_string

        parsed = message_from_string(smtp.messages[0].data)
        body = parsed.get_payload(decode=True).decode("utf-8")
        self.assertIn("连接被拒绝", body)
        self.assertIn("连续失败轮数：3", body)

    def test_no_email_when_healthy(self) -> None:
        self._add_run(True, minutes_ago=2)
        with FakeSMTPServer() as smtp:
            settings.alert_email_enabled = True
            settings.smtp_host = smtp.host
            settings.smtp_port = smtp.port
            settings.smtp_use_ssl = False
            settings.smtp_user = "monitor@example.com"
            settings.smtp_password = "code"
            settings.smtp_from = "monitor@example.com"
            settings.alert_recipients = "teacher@example.com"

            sent = alert_if_unhealthy(self.db, now=NOW)

        self.assertFalse(sent)
        self.assertEqual(len(smtp.messages), 0)

    def test_cooldown_prevents_repeat_emails(self) -> None:
        for i in range(3):
            self._add_run(False, minutes_ago=i + 1)

        with FakeSMTPServer() as smtp:
            settings.alert_email_enabled = True
            settings.alert_cooldown_hours = 24
            settings.smtp_host = smtp.host
            settings.smtp_port = smtp.port
            settings.smtp_use_ssl = False
            settings.smtp_user = "monitor@example.com"
            settings.smtp_password = "code"
            settings.smtp_from = "monitor@example.com"
            settings.alert_recipients = "teacher@example.com"

            self.assertTrue(alert_if_unhealthy(self.db, now=NOW))
            # 同一天内再体检：不应重复发信
            self.assertFalse(alert_if_unhealthy(self.db, now=NOW + timedelta(hours=1)))
            self.assertEqual(len(smtp.messages), 1, "冷却期内不应重复发送")

            # 超过冷却期后可以再发
            self.assertTrue(alert_if_unhealthy(self.db, now=NOW + timedelta(hours=25)))
            self.assertEqual(len(smtp.messages), 2)

    def test_no_crash_when_email_disabled(self) -> None:
        for i in range(3):
            self._add_run(False, minutes_ago=i + 1)
        settings.alert_email_enabled = False
        self.assertFalse(alert_if_unhealthy(self.db, now=NOW))


class TestHealthApi(unittest.TestCase):
    """前端数据健康度展示所依赖的接口契约。

    页面顶部徽标与告警条直接读这些字段，字段语义变了页面会静默显示错误结论。
    """

    _MUTATED = ("poll_interval_seconds", "collect_enabled", "run_background_jobs")

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = {n: getattr(settings, n) for n in cls._MUTATED}
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app)

    def setUp(self) -> None:
        for n, v in self._saved.items():
            setattr(settings, n, v)
        settings.collect_enabled = True
        settings.run_background_jobs = True
        self.db = SessionLocal()
        self.db.query(CollectRun).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _login(self) -> None:
        from app.core.security import issue_token

        self.client.cookies.set(settings.cookie_name, issue_token())

    def test_requires_session(self) -> None:
        self.client.cookies.clear()
        r = self.client.get("/api/collect/health")
        self.assertEqual(r.status_code, 401, "体检接口不应在未登录时泄露")

    def test_healthy_shape(self) -> None:
        self._login()
        self.db.add(
            CollectRun(source="all", job="poll", started_at=datetime.now(), ok=True, message="ok")
        )
        self.db.commit()

        body = self.client.get("/api/collect/health").json()
        for field in (
            "checked_at",
            "consecutive_failures",
            "last_success_age_minutes",
            "stale_after_minutes",
            "stale",
            "unhealthy",
            "last_message",
            "polling",
            "data_mode",
        ):
            self.assertIn(field, body, f"缺少字段 {field}，前端徽标会读不到")
        self.assertFalse(body["unhealthy"])
        self.assertFalse(body["stale"])
        self.assertEqual(body["consecutive_failures"], 0)

    def test_unhealthy_when_all_runs_fail(self) -> None:
        self._login()
        now = datetime.now()
        for i in range(3):
            self.db.add(
                CollectRun(
                    source="all",
                    job="poll",
                    started_at=now - timedelta(minutes=i + 1),
                    ok=False,
                    message="连接被拒绝",
                )
            )
        self.db.commit()

        body = self.client.get("/api/collect/health").json()
        self.assertTrue(body["unhealthy"], "连续失败应报告不健康")
        self.assertEqual(body["consecutive_failures"], 3)
        self.assertIn("连接被拒绝", body["last_message"])

    def test_runs_endpoint(self) -> None:
        self._login()
        self.db.add(
            CollectRun(
                source="all", job="manual", started_at=datetime.now(),
                finished_at=datetime.now(), ok=True, courses_seen=7,
                events_upserted=42, message="课程 7 门",
            )
        )
        self.db.commit()

        rows = self.client.get("/api/collect/runs", params={"limit": 5}).json()
        self.assertTrue(rows)
        first = rows[0]
        for field in ("id", "job", "started_at", "ok", "courses_seen", "events_upserted", "message"):
            self.assertIn(field, first)
        self.assertEqual(first["job"], "manual")
        self.assertEqual(first["events_upserted"], 42)


class TestDailyTaskDue(unittest.TestCase):
    """「每天某整点跑一次」的判定。

    定时汇总是靠它触发的：判断错了会导致不发、或一天重复发好几封。
    """

    def test_disabled_when_negative(self) -> None:
        from app.services.health import daily_task_due

        self.assertFalse(daily_task_due(datetime(2026, 3, 11, 23, 0), -1, None))

    def test_not_due_before_hour(self) -> None:
        from app.services.health import daily_task_due

        self.assertFalse(daily_task_due(datetime(2026, 3, 11, 17, 59), 18, None))

    def test_due_at_and_after_hour(self) -> None:
        from app.services.health import daily_task_due

        self.assertTrue(daily_task_due(datetime(2026, 3, 11, 18, 0), 18, None))
        self.assertTrue(daily_task_due(datetime(2026, 3, 11, 23, 30), 18, None))

    def test_only_once_per_day(self) -> None:
        from app.services.health import daily_task_due

        ran = datetime(2026, 3, 11, 18, 5)
        self.assertFalse(daily_task_due(datetime(2026, 3, 11, 18, 30), 18, ran))
        self.assertFalse(daily_task_due(datetime(2026, 3, 11, 23, 0), 18, ran))

    def test_due_again_next_day(self) -> None:
        from app.services.health import daily_task_due

        ran = datetime(2026, 3, 11, 18, 5)
        self.assertTrue(daily_task_due(datetime(2026, 3, 12, 18, 2), 18, ran))

    def test_service_restart_after_hour_still_sends(self) -> None:
        """服务在汇总时刻之后才启动（例如 20:00 重启），当天仍应补发。"""
        from app.services.health import daily_task_due

        self.assertTrue(daily_task_due(datetime(2026, 3, 11, 20, 0), 18, None))


class TestDailyDigest(unittest.TestCase):
    """每日异常汇总邮件。

    普通「异常」不即时打示示例辅导员，靠这封定时汇总兜住 —— 它没被调度的话，
    除严重异常外的所有学生都不会被告警，等于漏掉一大半目标人群。
    """

    _MUTATED = (
        "alert_email_enabled", "alert_cooldown_hours", "smtp_host", "smtp_port",
        "smtp_use_ssl", "smtp_user", "smtp_password", "smtp_from",
        "alert_recipients", "digest_hour",
    )

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = {n: getattr(settings, n) for n in cls._MUTATED}

    def setUp(self) -> None:
        for n, v in self._saved.items():
            setattr(settings, n, v)
        self.db = SessionLocal()
        for table in (AppSetting, Alert, AttendanceDaily, AttendanceEvent,
                      Course, Student, StudentCourse, CollectRun):
            self.db.query(table).delete()
        self.db.commit()
        self._setup_data()

    def tearDown(self) -> None:
        self.db.close()
        for n, v in self._saved.items():
            setattr(settings, n, v)

    def _setup_data(self) -> None:
        """三名学生：一名连续 3 天缺勤、一名昨天缺勤、一名正常。"""
        from app.models import StudentCourse
        from app.services.ingest import rebuild_daily, upsert_students

        upsert_students(
            self.db,
            [
                {"student_no": "2024010001", "name": "严重同学", "grade": "2024",
                 "major_code": "01", "major_name": "智能科学与技术", "class_name": "智能241班"},
                {"student_no": "2024010002", "name": "轻度同学", "grade": "2024",
                 "major_code": "01", "major_name": "智能科学与技术", "class_name": "智能241班"},
                {"student_no": "2024010003", "name": "正常同学", "grade": "2024",
                 "major_code": "01", "major_name": "智能科学与技术", "class_name": "智能241班"},
            ],
        )
        now = datetime.now()
        for offset in (1, 2, 3):
            day = now.date() - timedelta(days=offset)
            guid = f"dg-{day:%Y%m%d}"
            start = datetime.combine(day, datetime.min.time()).replace(hour=8)
            self.db.add(
                Course(
                    schedule_guid=guid, course_name="高等数学", class_name="智能241班",
                    class_date=day, start_time=start,
                    end_time=start + timedelta(hours=1, minutes=35),
                    covered=True,   # 有考勤源 → 这些天算「有效统计日」
                )
            )
            # 四级预警的"无记录"要求：该生选了这门课（有应到关系），
            # 且这门课确实产生了考勤数据。缺了选课关系就判不出"该上课"。
            for no in ("2024010001", "2024010002", "2024010003"):
                self.db.add(
                    StudentCourse(student_no=no, schedule_guid=guid,
                                  class_date=day, source="roster")
                )
            # 严重同学：三天全缺勤
            self.db.add(
                AttendanceEvent(
                    source="hanboer", schedule_guid=guid, student_no="2024010001",
                    user_type=2, class_date=day, attend_status="缺勤",
                )
            )
            # 轻度同学：只有昨天缺勤，前天/大前天签到 → 连续 1 天 → 异常
            self.db.add(
                AttendanceEvent(
                    source="hanboer", schedule_guid=guid, student_no="2024010002",
                    user_type=2, class_date=day,
                    attend_status="缺勤" if offset == 1 else "出勤",
                    sign_in_time=None if offset == 1 else start + timedelta(minutes=3),
                )
            )
            # 正常同学：只有最近一天有课且签到（这样他是"活跃"）
            if offset == 1:
                self.db.add(
                    AttendanceEvent(
                        source="hanboer", schedule_guid=guid, student_no="2024010003",
                        user_type=2, class_date=day, attend_status="出勤",
                        sign_in_time=start + timedelta(minutes=3),
                    )
                )
        self.db.commit()
        rebuild_daily(
            self.db,
            student_nos={"2024010001", "2024010002", "2024010003"},
            start=now.date() - timedelta(days=10), end=now.date(), now=now,
        )

    def _enable(self, smtp) -> None:
        settings.alert_email_enabled = True
        settings.smtp_host = smtp.host
        settings.smtp_port = smtp.port
        settings.smtp_use_ssl = False
        settings.smtp_user = "monitor@example.com"
        settings.smtp_password = "code"
        settings.smtp_from = "monitor@example.com"
        settings.alert_recipients = "teacher@example.com"

    def test_current_anomalies_lists_only_abnormal(self) -> None:
        from app.services.alerts import current_anomalies

        rows = current_anomalies(self.db)
        nos = {r["student_no"] for r in rows}
        self.assertIn("2024010001", nos)
        self.assertIn("2024010002", nos)
        self.assertNotIn("2024010003", nos, "已签到的学生不应出现在待处理名单")
        # 越严重越靠前
        self.assertEqual(rows[0]["student_no"], "2024010001")

    def test_digest_sends_and_groups_by_severity(self) -> None:
        from app.services.alerts import daily_digest

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            ok, msg = daily_digest(self.db)

        self.assertTrue(ok, msg)
        self.assertEqual(len(smtp.messages), 1)

        from email import message_from_string

        parsed = message_from_string(smtp.messages[0].data)
        body = parsed.get_payload(decode=True).decode("utf-8")
        # 四级预警口径（2026-10-13）：
        #   · 严重同学连续 3 个有效统计日无有效考勤（那 3 天记的都是「缺勤」）
        #     → 学生无记录预警
        #   · 轻度同学只是昨天缺勤，另有 2 天签到 → 不构成预警
        #   · 正常同学只有 1 天有课且签到 → 不构成预警
        self.assertIn("严重同学", body)
        self.assertNotIn("轻度同学", body, "偶发一天缺勤不该进预警汇总")
        self.assertNotIn("正常同学", body, "正常学生不应出现在汇总里")
        self.assertIn("【学生无记录预警】", body)
        self.assertIn("有效统计日", body)

    def test_digest_skips_when_no_anomalies(self) -> None:
        """没有预警时不发信 —— 否则每天一封"一切正常"会成为噪音，老师很快就不看了。"""
        from app.services.alerts import daily_digest

        # 给两名学生的**每一个有效统计日**都补上签到记录（含今天），
        # 这样"连续无记录"与"长期缺少记录"都不成立。
        # 注意夹具里 1~3 天前已有事件（唯一键会冲突），所以用「有则改、无则加」。
        from app.models import AttendanceEvent as _Ev
        from sqlalchemy import select as _select

        now = datetime.now()
        for offset in (0, 1, 2, 3):
            day = now.date() - timedelta(days=offset)
            start = datetime.combine(day, datetime.min.time()).replace(hour=8)
            guid = f"dg-{day:%Y%m%d}"
            for no in ("2024010001", "2024010002"):
                exists = self.db.execute(
                    _select(_Ev).where(
                        _Ev.schedule_guid == guid, _Ev.student_no == no
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    exists.attend_status = "出勤"
                    exists.sign_in_time = start + timedelta(minutes=3)
                    continue
                self.db.add(
                    _Ev(
                        source="hanboer", schedule_guid=guid, student_no=no,
                        user_type=2, class_date=day, attend_status="出勤",
                        sign_in_time=start + timedelta(minutes=3),
                    )
                )
        self.db.commit()
        from app.services.ingest import rebuild_daily

        rebuild_daily(
            self.db, student_nos={"2024010001", "2024010002"},
            start=now.date() - timedelta(days=3), end=now.date(), now=now,
        )

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            ok, msg = daily_digest(self.db)

        self.assertTrue(ok)
        self.assertEqual(len(smtp.messages), 0, "无预警时不应发信")
        self.assertIn("无预警", msg)

    def test_digest_is_idempotent_per_day(self) -> None:
        from app.services.alerts import daily_digest

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            ok1, _ = daily_digest(self.db)
            ok2, msg2 = daily_digest(self.db)  # 同日第二次

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(len(smtp.messages), 1, "同一天不应重复发送汇总")
        self.assertIn("已发送过", msg2)

    def test_digest_force_resends(self) -> None:
        from app.services.alerts import daily_digest

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            daily_digest(self.db)
            daily_digest(self.db, force=True)

        self.assertEqual(len(smtp.messages), 2, "force=True 应忽略当日已发送标记")

    def test_digest_reports_when_email_disabled(self) -> None:
        from app.services.alerts import daily_digest

        settings.alert_email_enabled = False
        ok, msg = daily_digest(self.db)
        self.assertFalse(ok)
        self.assertIn("未启用", msg)

    def test_digest_reports_when_no_recipients(self) -> None:
        from app.services.alerts import daily_digest

        settings.alert_email_enabled = True
        settings.alert_recipients = ""
        ok, msg = daily_digest(self.db)
        self.assertFalse(ok)
        self.assertIn("收件人", msg)


class TestAlertEmailContent(unittest.TestCase):
    """告警邮件必须"可行动"：老师不打开页面也能判断该找谁、缺了什么课。

    只给一个学号和"连续 3 天无签到"，老师还得自己登录翻课表 ——
    邮件就只是提醒，不是可行动的信息。
    """

    _MUTATED = ("alert_email_enabled", "smtp_host", "smtp_port", "smtp_use_ssl",
                "smtp_user", "smtp_password", "smtp_from", "alert_recipients")

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = {n: getattr(settings, n) for n in cls._MUTATED}

    def setUp(self) -> None:
        for n, v in self._saved.items():
            setattr(settings, n, v)
        self.db = SessionLocal()
        for table in (AppSetting, Alert, AttendanceDaily, AttendanceEvent,
                      StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        for n, v in self._saved.items():
            setattr(settings, n, v)

    def _seed_absent_student(self, course_name: str = "高等数学") -> None:
        """一名学生连续 3 天有课、且这 3 天都没有有效考勤记录。

        注意「有效统计日」的定义：该生选了这门课（有应到关系），
        且这门课确实产生了考勤数据。夹具必须把这两件事都造出来，
        否则判不出"该上课却无记录"。
        """
        from app.models import StudentCourse
        from app.services.ingest import rebuild_daily, upsert_students

        upsert_students(
            self.db,
            [
                {"student_no": "2024010001", "name": "张同学", "grade": "2024",
                 "major_code": "01", "major_name": "智能科学与技术",
                 "class_name": "智能241班"},
            ],
        )
        now = datetime.now()
        for offset in (1, 2, 3):
            day = now.date() - timedelta(days=offset)
            guid = f"mail-{day:%Y%m%d}"
            start = datetime.combine(day, datetime.min.time()).replace(hour=8)
            self.db.add(
                Course(
                    schedule_guid=guid, course_name=course_name, class_name="智能241班",
                    class_date=day, start_time=start, node_name="第一节",
                    end_time=start + timedelta(hours=1, minutes=35),
                    covered=True,
                )
            )
            self.db.add(
                StudentCourse(student_no="2024010001", schedule_guid=guid,
                              class_date=day, source="roster")
            )
            self.db.add(
                AttendanceEvent(
                    source="hanboer", schedule_guid=guid, student_no="2024010001",
                    user_type=2, class_date=day, attend_status="缺勤",
                    node_no="第一节",
                )
            )
        self.db.commit()
        rebuild_daily(
            self.db, student_nos={"2024010001"},
            start=now.date() - timedelta(days=10), end=now.date(), now=now,
        )

    def _enable(self, smtp) -> None:
        settings.alert_email_enabled = True
        settings.alert_severe_immediate = True
        settings.alert_cooldown_hours = 0
        settings.smtp_host = smtp.host
        settings.smtp_port = smtp.port
        settings.smtp_use_ssl = False
        settings.smtp_user = "monitor@example.com"
        settings.smtp_password = "code"
        settings.smtp_from = "monitor@example.com"
        settings.alert_recipients = "teacher@example.com"

    def _send_and_read(self) -> str:
        from email import message_from_string

        from app.services.alerts import evaluate_and_alert

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            evaluate_and_alert(self.db, {"2024010001"}, commit=True)
            self.assertEqual(len(smtp.messages), 1, "应发出一封告警邮件")
            parsed = message_from_string(smtp.messages[0].data)
            return parsed.get_payload(decode=True).decode("utf-8")

    def test_email_includes_student_identity(self) -> None:
        self._seed_absent_student()
        body = self._send_and_read()
        self.assertIn("张同学", body)
        self.assertIn("2024010001", body)
        self.assertIn("智能科学与技术", body, "应写明年级专业/班级")

    def test_email_includes_verdict_and_streak(self) -> None:
        self._seed_absent_student()
        body = self._send_and_read()
        self.assertIn("学生无记录预警", body)
        self.assertIn("连续", body)
        self.assertIn("判定说明", body)

    def test_email_includes_course_names(self) -> None:
        """关键：邮件要说清缺的是哪门课，否示示例辅导员还得自己去翻课表。"""
        self._seed_absent_student(course_name="大学物理")
        body = self._send_and_read()
        self.assertIn(
            "大学物理", body,
            "邮件应包含课程名 —— 否示示例辅导员无法据此联系任课教师或判断情况",
        )

    def test_email_survives_missing_course_context(self) -> None:
        """取不到课程明细时也要能发出去，只是降级提示，不能整封失败。"""
        from app.services.alerts import _render_body
        from app.models import Alert
        from datetime import date as _date

        alert = Alert(
            student_no="2024010001", kind="学生无记录预警",
            state="学生无记录预警", consecutive_days=3,
            first_date=_date(2026, 3, 1), last_date=_date(2026, 3, 3),
            detail="测试", detail_json="",
        )
        body = _render_body(alert, None)
        self.assertIn("预警", body)   # 新口径按四类预警命名（2026-10-13）
        self.assertIn("未能取到课程明细", body)

    def test_email_survives_broken_json(self) -> None:
        """detail_json 损坏不能让告警发不出去。"""
        from datetime import date as _date

        from app.models import Alert
        from app.services.alerts import _render_body

        alert = Alert(
            student_no="2024010001", kind="学生无记录预警",
            state="学生无记录预警", consecutive_days=3,
            first_date=_date(2026, 3, 1), last_date=_date(2026, 3, 3),
            detail="测试", detail_json="{不是合法 JSON",
        )
        body = _render_body(alert, None)
        self.assertIn("预警", body)   # 新口径按四类预警命名（2026-10-13）


if __name__ == "__main__":
    unittest.main(verbosity=2)
