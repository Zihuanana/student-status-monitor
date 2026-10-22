# -*- coding: utf-8 -*-
"""采集"全挂了"必须报失败 —— 不能被记成一次成功的空采集。

**为什么这条值得单独测**
采集是这套系统的命脉：它瞎了，页面上却只会显示"全员无异常"，
老师看到一切正常。这是最危险的一类失效 —— 静默失效。

实测踩过两次：
  1. 新服务器被考勤平台限流 403，而 `app.cli collect` 打印
     "✅ 课程 0 门，事件 0 条"，完全看不出异常。
  2. 更早的判据是 `if source_errors and not courses and not events` ——
     `courses` 常常是**从库里读出来的**（今天的课表已入库），
     所以接口全挂时 `courses` 也非空，条件永不成立。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta

from tests import dbguard  # noqa: E402

dbguard.isolate("collect_errors")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import AttendanceEvent, CollectRun, Course, Student, StudentCourse  # noqa: E402
from app.services.collect import Collector  # noqa: E402

NOW = datetime(2026, 10, 15, 19, 0)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for t in (AttendanceEvent, StudentCourse, Course, Student, CollectRun):
            self.db.query(t).delete()
        self.db.commit()
        self._saved = {
            k: getattr(settings, k)
            for k in ("data_mode", "hb_enabled", "hb_base_url")
        }
        settings.data_mode = "live"
        settings.hb_enabled = True
        settings.hb_base_url = "https://example.invalid"
        # 造一门今天有考勤源的课 —— 让 _fetch_details 的 todo 非空，
        # 这样才会真的去调接口、真的失败。
        day = NOW.date()
        start = datetime.combine(day, datetime.min.time()).replace(hour=8)
        self.db.add(
            Course(
                schedule_guid="c-today", course_name="高等数学", class_date=day,
                start_time=start, end_time=start + timedelta(minutes=95),
                covered=True, node_name="海南第一二节",
            )
        )
        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024",
                            class_name="2024数字智能1班", active=True))
        self.db.add(StudentCourse(student_no="202400000001N", schedule_guid="c-today",
                                  class_date=day, source="roster"))
        self.db.commit()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)
        self.db.close()

    def _patch_all_sources_failing(self) -> None:
        """让统计与明细接口都抛错（模拟 403 限流/接口不可用）。"""
        from app.services import collect as mod

        async def boom_stats(self, now):
            return [], ["翰博尔·考勤汇总 HanboerError: 被限流 HTTP 403"]

        async def boom_details(self, db, now):
            self._detail_errors = ["翰博尔·考勤详情 HanboerError: 被限流 HTTP 403"]
            return []

        self._orig = (
            mod.Collector._fetch_statistics,
            mod.Collector._fetch_details,
        )
        mod.Collector._fetch_statistics = boom_stats          # type: ignore[assignment]
        mod.Collector._fetch_details = boom_details           # type: ignore[assignment]

    def _restore(self) -> None:
        """还原被替换的方法。

        ⚠ **不要**用 importlib.reload()：reload 会造出一个**新的 Collector 类**，
        而测试模块顶部 `from app.services.collect import Collector` 拿到的是旧类，
        于是后面对旧类的打补丁对 reload 后的新类完全无效 ——
        表现为"补丁没生效、断言莫名其妙地失败"。实测踩过。
        直接存取方法属性最可靠。
        """
        from app.services import collect as mod

        stats, details = self._orig
        mod.Collector._fetch_statistics = stats      # type: ignore[assignment]
        mod.Collector._fetch_details = details       # type: ignore[assignment]


class TestAllSourcesFailingIsReported(_Base):
    def test_collect_reports_failure_when_every_source_fails(self) -> None:
        """**核心**：所有数据源都失败时，采集结果必须 ok=False。"""
        self._patch_all_sources_failing()
        try:
            result = asyncio.run(Collector().run_once(self.db, job="test", now=NOW))
        finally:
            self._restore()

        self.assertFalse(
            result["ok"],
            "接口全挂却报成功 —— 页面会显示『全员无异常』，而系统其实已经瞎了。"
            f"实际 message：{result['message']}",
        )
        self.assertIn("403", result["message"], "失败原因要写进 message，便于排障")

    def test_gucourses_from_db_does_not_mask_failure(self) -> None:
        """**关键回归**：库里已有课程时，仍然必须报失败。

        原来判据带 `not courses`，而 courses 常来自库（课表已入库），
        于是永不成立、静默成功。这条就是钉那个 bug 的。
        """
        # 先确认库里确实有今天的课（会被 run_once 读到）
        n = (
            self.db.query(Course)
            .filter(Course.class_date == NOW.date(), Course.covered.is_(True))
            .count()
        )
        self.assertGreater(n, 0, "夹具应已有一门今天的课")

        self._patch_all_sources_failing()
        try:
            result = asyncio.run(Collector().run_once(self.db, job="test", now=NOW))
        finally:
            self._restore()

        self.assertFalse(
            result["ok"],
            "库里有课程就把接口全挂掩盖过去了 —— 这正是踩过的那个 bug",
        )

    def test_detail_errors_are_surfaced(self) -> None:
        """明细接口的错误必须外传到 source_errors（原来只写日志）。"""
        from app.services import collect as mod

        async def ok_stats(self, now):
            return [], []

        async def boom_details(self, db, now):
            self._detail_errors = ["翰博尔·考勤详情 HanboerError: 403"]
            return []

        # 记下原方法以便还原（与 _patch_all_sources_failing 同样的手法）
        self._orig = (
            mod.Collector._fetch_statistics,
            mod.Collector._fetch_details,
        )
        mod.Collector._fetch_statistics = ok_stats      # type: ignore[assignment]
        mod.Collector._fetch_details = boom_details     # type: ignore[assignment]
        try:
            result = asyncio.run(Collector().run_once(self.db, job="test", now=NOW))
        finally:
            self._restore()

        self.assertFalse(result["ok"], "明细全失败也必须报失败")
        self.assertIn("403", result["message"])


class TestCollectEnabledGate(unittest.TestCase):
    """``COLLECT_ENABLED=false`` 时：

    1. **不许**把"没采"说成"采了 0 条"（静默失效）；
    2. 人工触发（``force=True``）应当**无视**这个开关 ——
       它防的是自动轮询，不该把人的显式操作也挡掉。

    实测踩过：部署时按计划把 COLLECT_ENABLED 设成 false 先观察，
    然后手工 `app.cli collect` 打印 "✅ 课程 0 门，事件 0 条" ——
    看起来像是"今天没课"，实际一次接口都没调。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for t in (AttendanceEvent, StudentCourse, Course, Student, CollectRun):
            self.db.query(t).delete()
        self.db.commit()
        self._saved = {
            k: getattr(settings, k)
            for k in ("data_mode", "collect_enabled", "hb_enabled", "hb_base_url")
        }
        settings.data_mode = "live"
        settings.hb_enabled = True
        settings.hb_base_url = "https://example.invalid"
        settings.collect_enabled = False

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)
        self.db.close()

    def test_message_says_disabled_not_zero_collected(self) -> None:
        """关着的时候，消息必须说"采集已关闭"，不能是"课程 0 门"。"""
        result = asyncio.run(Collector().run_once(self.db, job="test", now=NOW))
        self.assertTrue(result["ok"])
        self.assertIn(
            "采集已关闭", result["message"],
            f"关着却报成'采了 0 条'，会让人以为今天没课。实际：{result['message']}",
        )
        self.assertNotIn("课程 0 门", result["message"])

    def test_force_bypasses_the_gate(self) -> None:
        """force=True（人工触发）必须真的去采，而不是空转。"""
        called = {"stats": 0, "details": 0}
        from app.services import collect as mod

        # 造一门今天的课，让 _fetch_details 的 todo 非空 ——
        # 否则它压根不会去调明细接口，测不出"有没有真的采"。
        day = NOW.date()
        start = datetime.combine(day, datetime.min.time()).replace(hour=8)
        self.db.add(
            Course(
                schedule_guid="c-gate", course_name="高等数学", class_date=day,
                start_time=start, end_time=start + timedelta(minutes=95),
                covered=True, node_name="海南第一二节",
            )
        )
        self.db.commit()

        async def fake_stats(self, now):
            called["stats"] += 1
            return [], []

        async def fake_details(self, db, now):
            called["details"] += 1
            self._detail_errors = []
            return []

        orig_stats = mod.Collector._fetch_statistics
        orig_details = mod.Collector._fetch_details
        mod.Collector._fetch_statistics = fake_stats        # type: ignore[assignment]
        mod.Collector._fetch_details = fake_details         # type: ignore[assignment]
        try:
            result = asyncio.run(
                Collector().run_once(self.db, job="test", now=NOW, force=True)
            )
        finally:
            mod.Collector._fetch_statistics = orig_stats    # type: ignore[assignment]
            mod.Collector._fetch_details = orig_details     # type: ignore[assignment]

        self.assertGreater(called["stats"], 0, "force=True 却没去调统计接口")
        self.assertGreater(called["details"], 0, "force=True 却没去调明细接口")
        self.assertNotIn("采集已关闭", result["message"])


if __name__ == "__main__":
    unittest.main()
