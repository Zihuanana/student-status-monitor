# -*- coding: utf-8 -*-
"""「应到未签到」推断的保险丝测试。

背景
----
真实考勤接口只返回**有考勤动作**的人，整节课没来的学生查无此人。
系统用「班级应到名册 − 已观测到的学生」反推缺勤，这是发现这类学生的唯一手段。

但如果名册本身不准（例如把外班学生算进来），差集就会爆炸，导致大规模误报。
因此有一条保险丝：缺勤比例超过阈值时整节课跳过推断并告警。

阈值过紧的代价同样严重：小班遇到一次集体缺勤就整批漏判 ——
**最该被发现的学生反而被放过**。本模块锁定这条边界。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from tests import dbguard  # noqa: E402

# 必须在导入 app.* 之前隔离数据库 —— app.core.db 的引擎在首个导入者处
# 就已绑定，晚设置会连到工作区真实库（曾把演示数据清空）。
_DB_PATH = dbguard.isolate("infer")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import AttendanceEvent, Course, Student, StudentCourse  # noqa: E402
from app.services.infer import infer_absentees, pending_inference_courses  # noqa: E402

CLASS_NAME = "智能241班"


class TestInferRatioGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = settings.infer_max_ratio

    def setUp(self) -> None:
        settings.infer_max_ratio = self._saved
        self.db = SessionLocal()
        for table in (StudentCourse, AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        settings.infer_max_ratio = self._saved

    def _setup(self, roster: int, observed: int, minutes_ago: int = 60) -> Course:
        """建一个班 roster 人，其中 observed 人在考勤记录里，其余完全缺席。"""
        db = self.db
        now = datetime.now()
        start = now - timedelta(minutes=minutes_ago)
        for i in range(roster):
            db.add(
                Student(
                    student_no=f"2024{i:06d}", name=f"学生{i}", grade="2024",
                    major_code="01", major_name="智能科学与技术", class_name=CLASS_NAME,
                )
            )
        course = Course(
            schedule_guid="c-guard", course_name="高等数学", class_name=CLASS_NAME,
            class_date=start.date(), start_time=start,
            end_time=start + timedelta(minutes=40),
        )
        db.add(course)
        db.commit()
        for i in range(observed):
            db.add(
                AttendanceEvent(
                    source="hanboer", schedule_guid="c-guard",
                    student_no=f"2024{i:06d}", user_type=2,
                    class_date=start.date(), attend_status="出勤",
                    sign_in_time=start + timedelta(minutes=2),
                )
            )
        db.commit()
        return course

    def test_below_threshold_infers_everyone_missing(self) -> None:
        """缺勤比例在阈值内 → 剩余学生全部补齐（这是核心能力）。"""
        course = self._setup(roster=10, observed=6)  # 缺 4/10 = 40%
        self.db.add(
            StudentCourse(
                student_no="2024000000", schedule_guid=course.schedule_guid,
                class_date=course.class_date,
            )
        )
        self.db.commit()

        inferred = infer_absentees(self.db, [course], now=datetime.now())
        self.assertEqual(len(inferred), 4, "40% 缺勤应全部补齐")
        self.assertEqual({e.attend_status for e in inferred}, {"缺勤"})

    def test_above_threshold_skips_with_warning(self) -> None:
        """缺勤比例超阈值 → 整节课跳过，避免名单不准造成大面积误报。"""
        settings.infer_max_ratio = 0.5
        course = self._setup(roster=10, observed=3)  # 缺 7/10 = 70%
        inferred = infer_absentees(self.db, [course], now=datetime.now())
        self.assertEqual(inferred, [], "超过阈值时不应推断")

    def test_small_class_collective_absence_still_detected(self) -> None:
        """回归：小班集体缺勤不能因为阈值太紧而整批漏判。

        5 人小班缺 4 人 = 80%，默认阈值 0.8 恰好放行；
        早期硬编码 0.6 会把这类情况整批跳过 —— 最该被发现的学生反而被放过。
        """
        course = self._setup(roster=5, observed=1)  # 缺 4/5 = 80%
        inferred = infer_absentees(self.db, [course], now=datetime.now())
        self.assertEqual(len(inferred), 4, "小班集体缺勤必须能补齐")

    def test_in_progress_course_not_inferred(self) -> None:
        """课还没结束不能下结论 —— 否则开课几分钟就把全班标成缺勤。"""
        course = self._setup(roster=10, observed=2, minutes_ago=-10)  # 10 分钟后才结束
        inferred = infer_absentees(self.db, [course], now=datetime.now())
        self.assertEqual(inferred, [], "进行中的课不应推断缺勤")

    def test_no_roster_means_no_inference(self) -> None:
        """没有班级名单时不能瞎猜。"""
        course = self._setup(roster=10, observed=3, minutes_ago=60)
        # 把班级清空，模拟"名册没有班级信息"
        self.db.query(Student).update({Student.class_name: ""})
        self.db.commit()
        inferred = infer_absentees(self.db, [course], now=datetime.now())
        self.assertEqual(inferred, [])

    def test_ratio_clamped_to_sane_range(self) -> None:
        """配置异常的阈值要被夹到合理区间，不能变成"永不推断"或"总是推断"。"""
        from app.services.infer import _max_infer_ratio

        settings.infer_max_ratio = -5
        self.assertGreater(_max_infer_ratio(), 0)
        settings.infer_max_ratio = 99
        self.assertLessEqual(_max_infer_ratio(), 1.0)

    def test_pending_courses_respects_window(self) -> None:
        """只对最近结束的课做推断，避免把很久以前的课重新翻出来。"""
        self._setup(roster=5, observed=5, minutes_ago=60 * 24 * 5)  # 5 天前
        found = pending_inference_courses(self.db, now=datetime.now(), hours=24)
        self.assertEqual(found, [], "超出窗口的课不应参与推断")

        _ = date
