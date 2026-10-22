# -*- coding: utf-8 -*-
"""端到端集成测试：真实 HTTP 采集 → 判定 → 邮件投递。

这是本仓库最重要的一组测试，因为它覆盖了两条**此前从未被真正运行过**的链路：

  1. 从 HTTP 考勤接口带签名拉取数据（此前只有内存 mock 数据源）；
  2. 通过 SMTP 登录并投递告警邮件（此前只验证过服务器能连通 465 端口）。

刻意使用假的翰博尔服务器（严格复刻文档中的鉴权与分页行为）+ 假 SMTP 服务器，
以便在**拿到真实凭据之前**就把整条链路跑通，避免接入当天才发现签名错误、
分页漏数据、邮件发不出去等问题。

注意：本模块在导入 app 之前就把配置指向临时目录，避免测试写入工作区的 data/。
"""

from __future__ import annotations

import os
import tempfile

# ── 必须在导入 app.* 之前设置环境 ─────────────────────────────
from tests import dbguard  # noqa: E402

# 必须在导入 app.* 之前隔离数据库 —— app.core.db 的引擎在首个导入者处
# 就已绑定，晚设置会连到工作区真实库（曾把演示数据清空）。
_DB_PATH = dbguard.isolate("integration")
os.environ["DATA_MODE"] = "live"  # 走真实采集分支
os.environ["COLLECT_ENABLED"] = "true"  # 允许手动采集
os.environ["RUN_BACKGROUND_JOBS"] = "false"  # 但不启动后台轮询循环
os.environ["SESSION_SECRET"] = "integration-test-secret"

import asyncio  # noqa: E402
import base64  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from email import message_from_string  # noqa: E402

from app.adapters.hanboer import HanboerClient  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.enums import StudentState  # noqa: E402
from app.models import (  # noqa: E402
    Alert,
    AttendanceEvent,
    CollectRun,
    Course,
    Student,
    StudentCourse,
)
from app.services.ingest import dashboard_rows, upsert_students  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeCourse,
    FakeHanboerServer,
    FakeRecord,
    FakeSMTPServer,
)

CLASS_NAME = "智能科学与技术241班"
STUDENTS = ["2024010001", "2024010002", "2024010003"]
ALWAYS_ABSENT = "2024010001"  # 真实系统里，整节课没来的学生不会出现在返回列表中

# ── 显式切到 live 采集分支 ────────────────────────────────────
# 只靠上面的环境变量是不够的：若**别的测试模块先**导入了 app.core.config，
# 单例就已经按工作区 .env（DATA_MODE=mock）建好了，环境变量不再生效，
# 集成用例会静默跑在 mock 分支上、根本不发任何 HTTP 请求。
# 这里在导入之后明确覆盖，保证无论测试顺序如何都走真实采集路径。
settings.data_mode = "live"
settings.collect_enabled = True


class TestHanboerHttpClient(unittest.TestCase):
    """客户端与假接口服务器的契约测试：签名、分页、字段映射。"""

    def setUp(self) -> None:
        now = datetime.now()
        self.today = now.date()
        start = datetime.combine(self.today, datetime.min.time()).replace(hour=8)
        self.courses = [
            FakeCourse(
                schedule_guid=f"c{i}",
                course_name=f"课程{i}",
                class_name=CLASS_NAME,
                start=start + timedelta(hours=i),
                end=start + timedelta(hours=i + 1),
            )
            for i in range(3)
        ]
        self.records = {
            c.schedule_guid: [
                FakeRecord(n, "出勤", c.start + timedelta(minutes=3)) for n in STUDENTS
            ]
            for c in self.courses
        }
        self.server = FakeHanboerServer(self.courses, self.records)

    def _client(self, server: FakeHanboerServer) -> HanboerClient:
        return HanboerClient(
            base_url=server.base_url,
            access_key="AK",
            access_key_secret="SK",
            app_source="app",
        )

    def test_statistics_and_details_roundtrip(self) -> None:
        with self.server:
            client = self._client(self.server)

            async def run() -> tuple[list, list]:
                stats = await client.iter_statistics()
                details = await client.iter_details(schedule_guid="c0")
                await client.aclose()
                return stats, details

            stats, details = asyncio.run(run())

        self.assertEqual(len(stats), 3)
        self.assertEqual(stats[0].course_name, "课程0")
        self.assertEqual(stats[0].source, "hanboer")
        self.assertEqual(stats[0].class_name, CLASS_NAME)
        self.assertEqual(str(stats[0].class_date), self.today.isoformat())

        self.assertEqual(len(details), len(STUDENTS))
        self.assertEqual({d.student_no for d in details}, set(STUDENTS))
        self.assertEqual(details[0].attend_status, "出勤")
        self.assertIsNotNone(details[0].sign_in_time)
        self.assertEqual(details[0].user_type, 2)

    def test_every_request_carries_signature_params(self) -> None:
        with self.server:
            client = self._client(self.server)

            async def run() -> None:
                await client.iter_statistics()
                await client.aclose()

            asyncio.run(run())

        self.assertTrue(self.server.requests)
        for req in self.server.requests:
            for key in ("sign", "timeStamp", "appSource", "accessKey"):
                self.assertIn(key, req["params"], f"{req['path']} 缺少 {key}")
            sign = req["params"]["sign"]
            self.assertEqual(len(sign), 32)
            self.assertEqual(sign, sign.upper())
            int(sign, 16)
            self.assertEqual(req["params"]["appSource"], "app")

    def test_pagination_covers_paginated_results(self) -> None:
        """记录数超过 pageSize(50) 上限时必须翻页取全，不能只拿第一页。"""
        many = [f"202401{i:04d}" for i in range(120)]
        guid = "big"
        self.records = {guid: [FakeRecord(n, "出勤", datetime.now()) for n in many]}
        server = FakeHanboerServer(self.courses, self.records)
        with server:
            client = self._client(server)

            async def run() -> list:
                out = await client.iter_details(schedule_guid=guid)
                await client.aclose()
                return out

            details = asyncio.run(run())

        self.assertEqual(len(details), 120, "分页未取全（应自动翻页）")
        self.assertGreaterEqual(len(server.requests), 3)

    def test_signature_rejected_when_missing(self) -> None:
        """缺少签名参数时应报错，而不是静默拿到空数据。"""
        with self.server:
            client = HanboerClient(
                base_url=self.server.base_url,
                access_key="",
                access_key_secret="",
                app_source="",
            )

            async def run() -> None:
                await client.iter_statistics()
                await client.aclose()

            with self.assertRaises(Exception):
                asyncio.run(run())


class TestCollectionPipeline(unittest.TestCase):
    """采集 → 补齐 → 判定 → 告警 → 邮件，全链路。"""

    #: 本类会临时改写这些全局配置，必须在每个用例前后恢复，
    #: 否则「接口不可用」用例留下的死地址会污染后面的用例。
    _MUTATED = (
        "hb_base_url", "hb_access_key", "hb_access_key_secret", "hb_app_source",
        "hb_enabled", "http_max_retries", "http_timeout_seconds",
        "alert_email_enabled", "alert_severe_immediate", "alert_cooldown_hours",
        "smtp_host", "smtp_port", "smtp_use_ssl", "smtp_user", "smtp_password",
        "smtp_from", "alert_recipients", "collect_enabled", "data_mode",
    )

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = {name: getattr(settings, name) for name in cls._MUTATED}

    def setUp(self) -> None:
        # 每个用例都从一份干净的配置出发；并强制 live 分支
        for name, value in self._saved.items():
            setattr(settings, name, value)
        settings.data_mode = "live"
        settings.collect_enabled = True

        db = SessionLocal()
        try:
            for table in (Alert, AttendanceEvent, StudentCourse, Course, Student,
                          CollectRun):
                db.query(table).delete()
            db.commit()
            upsert_students(
                db,
                [
                    {
                        "student_no": n,
                        "name": f"学生{n[-2:]}",
                        "grade": "2024",
                        "major_code": "01",
                        "major_name": "智能科学与技术",
                        "class_name": CLASS_NAME,
                    }
                    for n in STUDENTS
                ],
            )
            db.commit()
        finally:
            db.close()

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(settings, name, value)

    def _seed_prior_class_days(self, days: int = 2) -> None:
        """铺出「前 N 天有课且整日无签到」的完整历史，**包括缺席学生本人**。

        必须是完整历史：只写课程而不写该生的事件，等价于「该生那几天根本没被
        采集到」，连续天数自然不成立 —— 这是测试夹具的坑，不是判定逻辑的问题。
        真实系统里这几天的数据是此前若干轮采集累积下来的。
        """
        db = SessionLocal()
        try:
            now = datetime.now()
            for offset in range(1, days + 1):
                day = now.date() - timedelta(days=offset)
                guid = f"hist-{day:%Y%m%d}"
                start = datetime.combine(day, datetime.min.time()).replace(hour=8)
                db.add(
                    Course(
                        schedule_guid=guid,
                        course_name="高等数学",
                        class_name=CLASS_NAME,
                        class_date=day,
                        start_time=start,
                        end_time=start + timedelta(hours=1, minutes=35),
                        should_student_count=len(STUDENTS),
                        # 这些课在真实系统里来自翰博尔（有考勤源），
                        # 所以这些天算「有效统计日」——四级预警的第 1 条
                        # 正是按"课程确实产生了考勤数据"来定义的。
                        covered=True,
                    )
                )
                for n in STUDENTS:
                    db.add(
                        AttendanceEvent(
                            source="hanboer",
                            schedule_guid=guid,
                            student_no=n,
                            user_type=2,
                            class_date=day,
                            attend_status="缺勤",
                        )
                    )
                    # 应到关系（真实场景由选课名单导入建立）
                    db.add(
                        StudentCourse(
                            student_no=n,
                            schedule_guid=guid,
                            class_date=day,
                            source="roster",
                        )
                    )
            db.commit()
        finally:
            db.close()

    def _today_fake_course(self, omit: set[str]) -> tuple[FakeCourse, FakeHanboerServer]:
        """今天的课程（已结束），接口返回里**不含** omit 里的学生。"""
        now = datetime.now()
        start = now - timedelta(hours=3)  # 确保课程已结束
        course = FakeCourse(
            schedule_guid=f"today-{now.date():%Y%m%d}",
            course_name="大学物理",
            class_name=CLASS_NAME,
            start=start,
            end=start + timedelta(hours=1, minutes=35),
        )
        records = {
            course.schedule_guid: [
                FakeRecord(n, "出勤", start + timedelta(minutes=3))
                for n in STUDENTS
                if n not in omit
            ]
        }
        return course, FakeHanboerServer([course], records)

    def _point_settings_at(self, server: FakeHanboerServer) -> None:
        settings.hb_base_url = server.base_url
        settings.hb_access_key = "AK"
        settings.hb_access_key_secret = "SK"
        settings.hb_app_source = "app"
        settings.hb_enabled = True

    def test_absent_student_missing_from_api_is_still_flagged(self) -> None:
        """核心场景：连续三天有课，且该生**从未出现在接口返回里**。

        真实考勤系统通常只返回有考勤动作的人，整节课没来的学生查无此人。
        本测试验证「应到名册反推缺勤」能把这类学生正确判为严重异常。
        """
        self._seed_prior_class_days(2)
        _course, server = self._today_fake_course(omit={ALWAYS_ABSENT})

        from app.services.collect import Collector

        with server:
            self._point_settings_at(server)
            db = SessionLocal()
            try:
                result = asyncio.run(Collector().run_once(db, job="test"))
            finally:
                db.close()

        self.assertTrue(result["ok"], f"采集失败：{result['message']}")

        db = SessionLocal()
        try:
            today = datetime.now().date()
            inferred = (
                db.query(AttendanceEvent)
                .filter(
                    AttendanceEvent.student_no == ALWAYS_ABSENT,
                    AttendanceEvent.class_date == today,
                )
                .all()
            )
            self.assertTrue(
                inferred, "未补齐「应到未签到」记录 —— 该生会在系统里凭空消失"
            )
            self.assertEqual(inferred[0].attend_status, "缺勤")

            rows = {r["student_no"]: r for r in dashboard_rows(db)}
            absent_row = rows[ALWAYS_ABSENT]
            self.assertEqual(
                absent_row["state"],
                StudentState.SEVERE.value,
                f"应为严重异常，实际 {absent_row['state']}（{absent_row['reason']}）",
            )
            self.assertGreaterEqual(absent_row["consecutive_missed_days"], 3)

            # 正常签到的同学不应被误报
            self.assertEqual(rows[STUDENTS[1]]["state"], StudentState.ACTIVE.value)
            self.assertEqual(rows[STUDENTS[2]]["state"], StudentState.ACTIVE.value)

            alerts = db.query(Alert).filter(Alert.student_no == ALWAYS_ABSENT).all()
            self.assertTrue(alerts, "未生成告警记录")
            self.assertEqual(alerts[0].kind, "学生无记录预警")
            self.assertFalse(alerts[0].email_sent, "邮件未启用时不应标记为已发送")
        finally:
            db.close()

    def test_details_not_refetched_after_sync(self) -> None:
        """已结束且同步过的课程不应每轮重复抓取（否则每天约 7000 次无效请求）。"""
        _course, server = self._today_fake_course(omit=set())

        from app.services.collect import Collector

        with server:
            self._point_settings_at(server)

            db = SessionLocal()
            try:
                asyncio.run(Collector().run_once(db, job="t1"))
            finally:
                db.close()

            first_round = len(server.requests)
            self.assertGreater(first_round, 0)
            self.assertIn("scheduleGuid", server.requests[-1]["params"])

            # 第二轮：课程已结束且已同步 → 不应再抓明细
            db = SessionLocal()
            try:
                asyncio.run(Collector().run_once(db, job="t2"))
            finally:
                db.close()

            second_round = server.requests[first_round:]
            detail_calls = [r for r in second_round if "getAttendanceDetails" in r["path"]]
            self.assertEqual(
                len(detail_calls), 0, f"第二轮仍重复抓取明细 {len(detail_calls)} 次"
            )

    def test_email_delivery_through_real_smtp_client(self) -> None:
        """邮件链路：走真正的 smtplib，投递到假 SMTP 服务器。"""
        from app.services.notify import send_alert_email

        with FakeSMTPServer() as smtp:
            settings.alert_email_enabled = True
            settings.smtp_host = smtp.host
            settings.smtp_port = smtp.port
            settings.smtp_use_ssl = False  # 假服务器不提供 TLS
            settings.smtp_user = "monitor@example.com"
            settings.smtp_password = "auth-code-123"
            settings.smtp_from = "monitor@example.com"
            settings.alert_recipients = "teacher@example.com,teacher2@example.com"

            ok, err = send_alert_email(
                subject="【学生状态监测】严重异常：学生01（2024010001）连续 3 个有课日无签到",
                body="这是一封集成测试邮件。\n第二行。",
            )

        self.assertTrue(ok, f"邮件发送失败：{err}")
        self.assertEqual(len(smtp.messages), 1, "假 SMTP 未收到邮件")

        msg = smtp.messages[0]
        self.assertTrue(msg.authenticated, "未执行 SMTP 登录")
        self.assertEqual(msg.auth_user, "monitor@example.com")
        self.assertEqual(msg.auth_password, "auth-code-123")
        self.assertEqual(msg.mail_from, "monitor@example.com")
        self.assertEqual(
            sorted(msg.rcpt_to), ["teacher2@example.com", "teacher@example.com"]
        )

        parsed = message_from_string(msg.data)
        self.assertEqual(parsed["To"], "teacher@example.com, teacher2@example.com")
        self.assertIn("=?utf-8?", str(parsed["Subject"]).lower(), "中文主题需 RFC2047 编码")

        # 正文为 base64 编码的 UTF-8，解码后再断言内容
        decoded = parsed.get_payload(decode=True).decode("utf-8")
        self.assertIn("集成测试邮件", decoded)
        self.assertIn("第二行", decoded)
        self.assertEqual(decoded, base64.b64decode(parsed.get_payload()).decode("utf-8"))

    def test_alert_email_marks_sent_and_respects_cooldown(self) -> None:
        """生成告警时会真的发信并回写状态；冷却期内不重复打示示例辅导员。"""
        from app.services.alerts import evaluate_and_alert

        self._seed_prior_class_days(3)  # 连同今天共 4 天 → 严重异常

        with FakeSMTPServer() as smtp:
            settings.alert_email_enabled = True
            settings.alert_severe_immediate = True
            settings.alert_cooldown_hours = 24
            settings.smtp_host = smtp.host
            settings.smtp_port = smtp.port
            settings.smtp_use_ssl = False
            settings.smtp_user = "monitor@example.com"
            settings.smtp_password = "auth-code-123"
            settings.smtp_from = "monitor@example.com"
            settings.alert_recipients = "teacher@example.com"

            db = SessionLocal()
            try:
                created = evaluate_and_alert(db, {ALWAYS_ABSENT}, commit=True)
                self.assertGreaterEqual(created, 1, "应生成告警")

                alert = db.query(Alert).filter(Alert.student_no == ALWAYS_ABSENT).first()
                self.assertIsNotNone(alert)
                self.assertEqual(alert.kind, "学生无记录预警")
                self.assertTrue(alert.email_sent, f"邮件未标记为已发送：{alert.email_error}")
                self.assertIsNotNone(alert.email_sent_at)
                self.assertEqual(alert.email_error, "")

                first_batch = len(smtp.messages)
                self.assertEqual(first_batch, 1, "应恰好发出 1 封告警邮件")

                # 冷却期内再评估一次：不应重复发信
                evaluate_and_alert(db, {ALWAYS_ABSENT}, commit=True)
                self.assertEqual(
                    len(smtp.messages), first_batch, "冷却期内不应重复发送告警邮件"
                )
            finally:
                db.close()

    def test_collect_failure_is_recorded_not_raised(self) -> None:
        """接口不可用时，采集应记录失败而不是把服务打崩。

        这一点直接决定「系统已经瞎了」会不会被发现：若失败被吞掉，
        页面会显示"全员无异常"，而实际上一条数据都没采到。
        """
        settings.hb_base_url = "http://127.0.0.1:9"  # 必然连不上
        settings.hb_access_key = "AK"
        settings.hb_access_key_secret = "SK"
        settings.hb_app_source = "app"
        settings.hb_enabled = True
        settings.http_max_retries = 1
        settings.http_timeout_seconds = 2

        from app.services.collect import Collector

        db = SessionLocal()
        try:
            result = asyncio.run(Collector().run_once(db, job="test-fail"))
        finally:
            db.close()

        self.assertFalse(result["ok"], "接口不可用时应报告失败")
        self.assertTrue(result["message"], "应带有可诊断的错误信息")

        db = SessionLocal()
        try:
            run = db.query(CollectRun).order_by(CollectRun.id.desc()).first()
            self.assertIsNotNone(run)
            self.assertFalse(run.ok)
            self.assertTrue(run.message)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
