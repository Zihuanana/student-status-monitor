# -*- coding: utf-8 -*-
"""每日预警汇总的**多人分发**端到端测试（2026-10-13 需求）。

``tests/test_routing.py`` 测的是"谁能收到哪些预警"（纯逻辑）；
本文件把邮件真的发一遍（假 SMTP 服务器），锁定：

1. 辅导员收到的邮件**只含自己班**的学生，且正文写明负责班级；
2. 管理员的邮件只含数据类预警；
3. 没示示例辅导员的班 → 预警落在管理员邮件里，并写明"请补配置"；
4. 配示示例辅导员之后，管理员**不再**收到那个班的学生预警（避免双重打扰）。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from email import message_from_string

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("digest_route")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceEvent,
    CollectRun,
    CounselorContact,
    Course,
    Student,
    StudentCourse,
)
from app.services.routing import normalize_class  # noqa: E402
from tests.fakes import FakeSMTPServer  # noqa: E402

NOW = datetime(2026, 10, 13, 20, 0)
TODAY = NOW.date()

CLASS_A = "2024数字媒体技术（中外合作办学）1班"
CLASS_B = "2024视觉传达设计（中外合作办学）1班"
STU_A = "202400000001N"
STU_B = "202400000001N"
#: 同班的「背景同学」。必须有他们，否则那几节课就是「有考勤源却一条记录都没有」，
#: 会被规则 4 判成数据异常、给管理员多发一封 —— 测试就会误以为路由错了。
#: 真实场景里也确实是这样的：同班总有人签到。
STU_OTHER = "202400000001N"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (
            AttendanceEvent, StudentCourse, Course, Student, CounselorContact,
            CollectRun,
        ):
            self.db.query(table).delete()
        self.db.commit()
        self.db.add_all(
            [
                Student(student_no=STU_A, name="甲同学", grade="2024", class_name=CLASS_A),
                Student(student_no=STU_B, name="乙同学", grade="2024", class_name=CLASS_B),
                Student(
                    student_no=STU_OTHER, name="背景同学", grade="2024",
                    class_name=CLASS_A,
                ),
            ]
        )
        self.db.commit()
        self._saved = {
            k: getattr(settings, k)
            for k in (
                "alert_email_enabled", "smtp_host", "smtp_port", "smtp_use_ssl",
                "smtp_user", "smtp_password", "smtp_from", "alert_recipients",
            )
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)
        # 清掉"当日已发送"标记，避免污染后续测试
        from app.models import AppSetting

        row = self.db.get(AppSetting, "digest_sent_on")
        if row is not None:
            self.db.delete(row)
            self.db.commit()
        self.db.close()

    # ── 夹具 ───────────────────────────────────────────────
    def _seed_no_record(self, student_no: str, days: int = 3) -> None:
        """给该生造 ``days`` 个「有课、有考勤源、但本人无记录」的日子。

        同时让**背景同学**在这些课上有签到 —— 否则这几节课会被规则 4 看成
        「有考勤源却没有任何考勤数据」的数据异常，给管理员多塞一封邮件。
        """
        for offset in range(1, days + 1):
            day = TODAY - timedelta(days=offset)
            # 用整段学号做 guid：只取尾部几位会撞（两个学号都以 001N 结尾），
            # 撞了就是 UNIQUE 冲突 —— 夹具的锅，不是被测代码的锅。
            guid = f"c-{student_no}-{day:%Y%m%d}"
            start = datetime.combine(day, datetime.min.time()).replace(hour=8)
            self.db.add(
                Course(
                    schedule_guid=guid, course_name="高等数学", class_date=day,
                    start_time=start, end_time=start + timedelta(minutes=95),
                    covered=True,
                )
            )
            for no in (student_no, STU_OTHER):
                self.db.add(
                    StudentCourse(
                        student_no=no, schedule_guid=guid, class_date=day,
                        source="roster",
                    )
                )
            # 背景同学签到（目标学生**故意**不签到）
            if student_no != STU_OTHER:
                self.db.add(
                    AttendanceEvent(
                        source="hanboer", schedule_guid=guid, student_no=STU_OTHER,
                        user_type=2, class_date=day, attend_status="出勤",
                        sign_in_time=start + timedelta(minutes=3),
                    )
                )
        self.db.commit()

    def _enable(self, smtp) -> None:
        settings.alert_email_enabled = True
        settings.smtp_host = smtp.host
        settings.smtp_port = smtp.port
        settings.smtp_use_ssl = False
        settings.smtp_user = "monitor@example.com"
        settings.smtp_password = "code"
        settings.smtp_from = "monitor@example.com"
        settings.alert_recipients = "admin@example.com"

    def _add_contact(self, class_name: str, *emails: str) -> None:
        slots = list(emails) + [""] * (3 - len(emails))
        self.db.add(
            CounselorContact(
                class_key=normalize_class(class_name), class_label=class_name,
                slot1=slots[0], slot2=slots[1], slot3=slots[2],
            )
        )
        self.db.commit()

    @staticmethod
    def _bodies(smtp) -> list[tuple[str, str]]:
        """``[(收件人, 正文)]``，正文已从 base64 解回明文。

        只取**第一个**收件地址：``To`` 头可能有多个，但本系统的汇总邮件
        都是单收件人一封（辅导员之间不该互相看到别人的班）。
        """
        out: list[tuple[str, str]] = []
        for m in smtp.messages:
            parsed = message_from_string(m.data)
            body = parsed.get_payload(decode=True).decode("utf-8")
            raw_to = str(parsed.get("To") or "")
            first = raw_to.split(",")[0].strip()
            out.append((first, body))
        return out


class TestDigestRouting(_Base):
    def test_counselor_and_admin_get_separate_mails(self) -> None:
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_A)          # A 班（已示示例辅导员）
        self._db_add_data_anomaly()
        self._add_contact(CLASS_A, "teacher-a@example.com")

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            ok, msg = daily_digest(self.db, force=True)

        self.assertTrue(ok, msg)
        mails = self._bodies(smtp)
        by_to = {to: body for to, body in mails}
        self.assertIn("teacher-a@example.com", by_to)
        self.assertIn("admin@example.com", by_to)
        self.assertEqual(len(mails), 2, "一示示例辅导员 + 一个管理员，各一封")

        teacher_body = by_to["teacher-a@example.com"]
        admin_body = by_to["admin@example.com"]
        self.assertIn("甲同学", teacher_body)
        self.assertIn("负责班级", teacher_body)
        self.assertIn("【学生无记录预警】", teacher_body)
        self.assertNotIn("最近一轮采集失败", teacher_body, "数据类不该发示示例辅导员")
        self.assertIn("最近一轮采集失败", admin_body, "数据异常必须发给管理员")

    def test_unassigned_class_is_flagged_to_admin(self) -> None:
        """**不许静默丢失**：B 班没示示例辅导员 → 预警落到管理员邮件并写明要补配置。"""
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_B)
        # 只配 A 班，B 班故意留空
        self._add_contact(CLASS_A, "teacher-a@example.com")

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            ok, msg = daily_digest(self.db, force=True)

        self.assertTrue(ok, msg)
        by_to = {to: body for to, body in self._bodies(smtp)}
        self.assertIn("admin@example.com", by_to)
        admin_body = by_to["admin@example.com"]
        self.assertIn("乙同学", admin_body, "没示示例辅导员的班，学生预警必须有人收到")
        self.assertIn("尚未配示示例辅导员", admin_body)
        self.assertIn(CLASS_B, admin_body)

    def test_configured_class_stops_bothering_admin(self) -> None:
        """配示示例辅导员之后，管理员不再收到该班学生预警（避免双重打扰）。"""
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_A)
        self._add_contact(CLASS_A, "teacher-a@example.com")

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            daily_digest(self.db, force=True)

        by_to = {to: body for to, body in self._bodies(smtp)}
        self.assertIn("teacher-a@example.com", by_to)
        self.assertNotIn("admin@example.com", by_to)

    def test_two_counselors_each_get_only_their_class(self) -> None:
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_A)
        self._seed_no_record(STU_B)
        self._add_contact(CLASS_A, "teacher-a@example.com")
        self._add_contact(CLASS_B, "teacher-b@example.com")

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            daily_digest(self.db, force=True)

        by_to = {to: body for to, body in self._bodies(smtp)}
        self.assertEqual(len(by_to), 2)
        self.assertIn("甲同学", by_to["teacher-a@example.com"])
        self.assertNotIn("乙同学", by_to["teacher-a@example.com"])
        self.assertIn("乙同学", by_to["teacher-b@example.com"])
        self.assertNotIn("甲同学", by_to["teacher-b@example.com"])

    def test_multi_slot_class_reaches_all_slots(self) -> None:
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_A)
        self._add_contact(CLASS_A, "a1@example.com", "a2@example.com", "a3@example.com")

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            daily_digest(self.db, force=True)

        tos = {to for to, _ in self._bodies(smtp)}
        for email in ("a1@example.com", "a2@example.com", "a3@example.com"):
            self.assertIn(email, tos, "三个收件位置都要收到")
        bodies = dict(self._bodies(smtp))
        self.assertIn("甲同学", bodies["a1@example.com"])
        self.assertIn("甲同学", bodies["a2@example.com"])
        self.assertIn("甲同学", bodies["a3@example.com"])

    def test_no_admin_configured_still_delivers_students(self) -> None:
        """没配管理员也不该让配示示例辅导员的班收不到信。"""
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_A)
        self._add_contact(CLASS_A, "teacher-a@example.com")

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            settings.alert_recipients = ""    # 管理员留空
            ok, msg = daily_digest(self.db, force=True)

        self.assertTrue(ok, msg)
        self.assertEqual(len(smtp.messages), 1)
        self.assertIn("teacher-a@example.com", self._bodies(smtp)[0][0])

    def test_reports_failure_when_one_recipient_bounces(self) -> None:
        """部分失败必须报出来 —— 不能让"发了"变成一句空话。"""
        from app.services import notify
        from app.services.alerts import daily_digest

        self._seed_no_record(STU_A)
        self._add_contact(CLASS_A, "teacher-a@example.com")
        real = notify.send_alert_email

        def flaky(subject: str, body: str, recipients=None):
            if recipients and recipients[0] == "teacher-a@example.com":
                return False, "550 mailbox unavailable"
            return real(subject, body, recipients)

        with FakeSMTPServer() as smtp:
            self._enable(smtp)
            notify.send_alert_email = flaky  # type: ignore[assignment]
            try:
                ok, msg = daily_digest(self.db, force=True)
            finally:
                notify.send_alert_email = real  # type: ignore[assignment]

        self.assertFalse(ok, "有收件人没收到就不算成功")
        self.assertIn("550", msg)
        _ = smtp

    # ── helpers ────────────────────────────────────────────
    def _db_add_data_anomaly(self) -> None:
        """造一条数据异常（最近一轮采集失败）。"""
        self.db.add(
            CollectRun(
                source="all", job="poll", started_at=NOW, finished_at=NOW, ok=False,
                message="接口超时",
            )
        )
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
