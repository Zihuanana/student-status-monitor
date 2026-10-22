# -*- coding: utf-8 -*-
"""多日时间推进演练：把「连续三天异常」的真实节奏压缩到秒级。

为什么需要这组测试
------------------
「连续三天以上有课日整日无签到」本质上是**跨天的时间性质**。
单元测试和单时刻的集成测试都验证不了它 —— 它们只能固定在某一刻看结果。
这里用假考勤服务器 + 假 SMTP，把学生 5 个上学日的签到节奏压缩成快速推进：

    第 1 天课后采集 → 第 2 天课后采集 → … → 第 5 天

每一步都断言「页面看到的状态」与「是否发了邮件」，覆盖：

- 状态随天推进的迁移（活跃 → 今日未签到 → 异常 → 严重异常 → 恢复活跃）
- 告警邮件**恰好在第 3 天**发出，而不是更早或更晚
- 「昨天异常」与「连续三天」的先后顺序（多一层区分）
- 请假能打断连续
- 老师不会因为同一次异常被反复骚扰
"""

from __future__ import annotations

import os
import tempfile

from tests import dbguard  # noqa: E402

# 必须在导入 app.* 之前隔离数据库 —— app.core.db 的引擎在首个导入者处
# 就已绑定，晚设置会连到工作区真实库（曾把演示数据清空）。
_DB_PATH = dbguard.isolate("timeline")
os.environ["DATA_MODE"] = "live"
os.environ["COLLECT_ENABLED"] = "true"
os.environ["RUN_BACKGROUND_JOBS"] = "false"
os.environ["SESSION_SECRET"] = "timeline-test-secret"

import asyncio  # noqa: E402
import unittest  # noqa: E402
from datetime import date, datetime, time as dtime, timedelta  # noqa: E402

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
)
from app.services.ingest import dashboard_rows, upsert_students  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeSMTPServer,
    StudentScenario,
    TimelineFakeServer,
)

CLASS_NAME = "智能241班"
#: 每个学生一条 5 天行为轨迹（1 起）：
#:   True=正常出勤 / False=整日缺勤（不在接口返回里）/ "leave"=请假
#:
#: 刻意凑成 8 人：前 4 天每天 3 人缺席（37.5%），低于「推断保险丝」阈值，
#: 因此"整节课没来"的学生会被正常补齐。班级太小（如 5 人里缺 3-4 人）会
#: 触发保险丝，属于"名单可能不准"的保护，不是本演练要验证的路径。
SCENARIOS = [
    StudentScenario("2024010001", "全勤甲", [True] * 5),
    StudentScenario("2024010002", "全勤乙", [True] * 5),
    StudentScenario("2024010003", "断点同学", [False, False, True, True, True]),
    StudentScenario("2024010004", "严重同学", [False, False, False, False, False]),
    StudentScenario("2024010005", "单日同学", [True, False, True, True, True]),
    StudentScenario("2024010006", "请假同学", [True, "leave", True, True, True]),
    StudentScenario("2024010007", "全勤丙", [True] * 5),
    StudentScenario("2024010008", "全勤丁", [True] * 5),
]
ALWAYS_PRESENT = "2024010001"
BROKEN = "2024010003"
SEVERE = "2024010004"
SINGLE = "2024010005"
ON_LEAVE = "2024010006"


def next_monday(day: date) -> date:
    """返回 day 当天或之后的第一个周一，保证 5 个工作日都不跨周末。"""
    return day + timedelta(days=(7 - day.weekday()) % 7)


class TimelineHarness:
    """把「某天课后」这一刻的采集跑一遍。

    注意：所有查询都必须带上**模拟时刻**。演练的时间轴与真实"今天"不同，
    若 `dashboard_rows` 用真实 now，它会去找"最近 30 天"，
    而模拟日期可能在其之外，结果全被判成「无课」。
    """

    def __init__(self, server: TimelineFakeServer, start: date) -> None:
        self.server = server
        self.start = start
        self.moment: datetime = datetime.combine(start, dtime(9, 40))

    def day_of(self, index: int) -> date:
        return self.start + timedelta(days=index - 1)

    def poll_after_class(self, index: int) -> datetime:
        """第 index 天、课程结束后的时刻（08:00-09:35 的课，09:40 采集）。"""
        moment = datetime.combine(self.day_of(index), dtime(9, 40))
        self.moment = moment
        self.server.advance_to(moment, index)

        from app.services.collect import Collector

        db = SessionLocal()
        try:
            result = asyncio.run(Collector().run_once(db, job="timeline", now=moment))
        finally:
            db.close()
        if not result["ok"]:
            raise AssertionError(f"第 {index} 天采集失败：{result['message']}")
        return moment

    def states(self) -> dict[str, str]:
        db = SessionLocal()
        try:
            return {
                r["student_no"]: r["state"]
                for r in dashboard_rows(db, now=self.moment)
            }
        finally:
            db.close()

    def detail(self, student_no: str) -> dict:
        db = SessionLocal()
        try:
            rows = {r["student_no"]: r for r in dashboard_rows(db, now=self.moment)}
            return rows[student_no]
        finally:
            db.close()

    def email_count(self, smtp: FakeSMTPServer, student_no: str) -> int:
        """统计提到该学号的邮件数（邮件正文为 base64，需解码）。"""
        from email import message_from_string

        n = 0
        for m in smtp.messages:
            parsed = message_from_string(m.data)
            body = parsed.get_payload(decode=True).decode("utf-8", "replace")
            if student_no in body:
                n += 1
        return n


class TestMultiDayTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls.start = next_monday(date.today() - timedelta(days=1))

    def setUp(self) -> None:
        settings.alert_cooldown_hours = 24
        settings.alert_severe_immediate = True
        settings.digest_hour = -1  # 演练只关心即时告警，关掉定时汇总

        db = SessionLocal()
        try:
            for table in (
                Alert, AppSetting, AttendanceDaily, AttendanceEvent, Course,
                Student, CollectRun,
            ):
                db.query(table).delete()
            db.commit()
            upsert_students(
                db,
                [
                    {
                        "student_no": sc.student_no,
                        "name": sc.name,
                        "grade": "2024",
                        "major_code": "01",
                        "major_name": "智能科学与技术",
                        "class_name": CLASS_NAME,
                    }
                    for sc in SCENARIOS
                ],
            )
            db.commit()
        finally:
            db.close()

        self.server = TimelineFakeServer(list(SCENARIOS), periods=1)
        self.harness = TimelineHarness(self.server, self.start)

    # ─────────────────────────────────────────────────────
    def test_state_progression_over_five_days(self) -> None:
        """逐日断言状态迁移，这是"连续三天"语义的直接验证。"""
        with self.server, FakeSMTPServer() as smtp:
            settings.alert_email_enabled = True
            settings.smtp_host = smtp.host
            settings.smtp_port = smtp.port
            settings.smtp_use_ssl = False
            settings.smtp_user = "monitor@example.com"
            settings.smtp_password = "code"
            settings.smtp_from = "monitor@example.com"
            settings.alert_recipients = "teacher@example.com"
            settings.hb_base_url = self.server.base_url
            settings.hb_access_key = "AK"
            settings.hb_access_key_secret = "SK"
            settings.hb_app_source = "app"

            # ── 第 1 天：刚刚开始缺勤 ──
            self.harness.poll_after_class(1)
            s = self.harness.states()
            self.assertEqual(s[ALWAYS_PRESENT], "活跃", "全勤同学第 1 天应活跃")
            self.assertEqual(s[SEVERE], "异常", "缺勤第 1 天是「异常」而非严重")
            self.assertEqual(
                self.harness.detail(SEVERE)["consecutive_missed_days"], 1
            )
            self.assertEqual(
                self.harness.email_count(smtp, SEVERE), 0,
                "第 1 天不应发即时告警（还没到连续三天）",
            )

            # ── 第 2 天：连续两天，仍是「异常」 ──
            self.harness.poll_after_class(2)
            s = self.harness.states()
            self.assertEqual(s[SEVERE], "异常", "连续两天仍是「异常」")
            self.assertEqual(
                self.harness.detail(SEVERE)["consecutive_missed_days"], 2
            )
            self.assertEqual(
                self.harness.email_count(smtp, SEVERE), 0,
                "连续两天不应发即时告警",
            )
            # 单日同学：第 2 天缺勤。这里是在**课后**采集，当天的课已结束，
            # 因此已是确定结论「异常」；若在课中采集才会是「今日未签到」。
            self.assertEqual(
                s[SINGLE], "异常",
                "课后采集时，当天缺勤已是确定结论",
            )
            self.assertEqual(
                self.harness.detail(SINGLE)["consecutive_missed_days"], 1
            )

            # ── 第 3 天：达到阈值 → 严重异常 + 立即发信 ──
            self.harness.poll_after_class(3)
            s = self.harness.states()
            self.assertEqual(
                s[SEVERE], "严重异常", "连续三天整日无签到应为「严重异常」"
            )
            detail = self.harness.detail(SEVERE)
            self.assertGreaterEqual(detail["consecutive_missed_days"], 3)
            self.assertEqual(
                self.harness.email_count(smtp, SEVERE), 1,
                "连续三天应在第 3 天恰好发出一封即时告警",
            )
            # 断点同学第 3 天来上课 → 回到活跃
            self.assertEqual(
                s[BROKEN], "活跃", "第 3 天签到后应从异常恢复为活跃"
            )
            # 请假同学第 3 天请假 → 不算异常
            self.assertNotIn(
                s[ON_LEAVE], ("异常", "严重异常"),
                "请假不应被判为异常",
            )

            # ── 第 4 天：严重同学继续缺勤，但不重复打扰 ──
            self.harness.poll_after_class(4)
            s = self.harness.states()
            self.assertEqual(s[SEVERE], "严重异常")
            self.assertEqual(
                self.harness.email_count(smtp, SEVERE), 1,
                "冷却期内不应重复发送同一学生的告警",
            )

            # ── 第 5 天：仍然保持 ──
            self.harness.poll_after_class(5)
            s = self.harness.states()
            self.assertEqual(s[SEVERE], "严重异常")
            self.assertEqual(s[ALWAYS_PRESENT], "活跃", "全勤同学始终活跃")

    def test_yesterday_abnormal_precedes_severe(self) -> None:
        """「异常」与「严重异常」的先后顺序：第 1 天必须是异常。

        这条断言防止有人把阈值写成 1（那样第一天就报严重异常），
        或者把「连续 N 天」误实现为「累计 N 天」。
        """
        with self.server:
            settings.alert_email_enabled = False
            settings.hb_base_url = self.server.base_url
            settings.hb_access_key = "AK"
            settings.hb_access_key_secret = "SK"
            settings.hb_app_source = "app"

            self.harness.poll_after_class(1)
            self.assertEqual(self.harness.states()[SEVERE], "异常")

            self.harness.poll_after_class(2)
            self.assertEqual(self.harness.states()[SEVERE], "异常")

            self.harness.poll_after_class(3)
            self.assertEqual(self.harness.states()[SEVERE], "严重异常")

    def test_persisted_snapshots_cover_each_day(self) -> None:
        """每天的天快照都应落库 —— 否则历史追溯与汇总会缺数据。"""
        with self.server:
            settings.alert_email_enabled = False
            settings.hb_base_url = self.server.base_url
            settings.hb_access_key = "AK"
            settings.hb_access_key_secret = "SK"
            settings.hb_app_source = "app"

            for i in range(1, 4):
                self.harness.poll_after_class(i)

            db = SessionLocal()
            try:
                days = {
                    r[0]
                    for r in db.query(AttendanceDaily.class_date)
                    .filter(AttendanceDaily.student_no == SEVERE)
                    .all()
                }
            finally:
                db.close()

            for i in range(1, 4):
                self.assertIn(
                    self.harness.day_of(i), days,
                    f"第 {i} 天的天快照缺失（写回范围收窄后可能漏写）",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
