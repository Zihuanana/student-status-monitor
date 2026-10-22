# -*- coding: utf-8 -*-
"""四级预警规则的回归测试（2026-10-13 学校口径）。

规则原文见 ``app/services/warnings.py`` 模块开头。本模块逐条锁定：

1. **学生无记录预警**：连续 3 个**有效统计日**无有效考勤记录。
   关键在"有效统计日"的定义 —— 课没有考勤数据的日子**不算**
   （那属于覆盖不足，不能拿来指控学生缺勤）。
2. **监测覆盖不足提醒**：7 天内有课，但这些课在各平台都没有可用考勤数据。
3. **长期缺少记录提醒**：14 天内记录持续偏少；但覆盖不足时**不判**缺少记录。
4. **数据异常提醒**：接口失败 / 课程对不上 / 课表缺失。

最容易出错、也最要紧的一条边界：**覆盖不足 ≠ 缺勤**。
把"我们看不到"当成"学生没来"，就是拿系统的盲区去冤枉学生。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("warnings")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.db import SessionLocal  # noqa: E402
from app.core.enums import AlertKind  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceEvent,
    CollectRun,
    Course,
    Student,
    StudentCourse,
)
from app.services.warnings import (  # noqa: E402
    consecutive_no_record_days,
    data_anomaly_warnings,
    evaluate_warnings,
    load_day_facts,
)

NOW = datetime(2026, 10, 13, 20, 0)   # 周二晚（课上完了）
TODAY = NOW.date()
STU = "202400000001N"
OTHER = "202400000001N"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceEvent, StudentCourse, Course, Student, CollectRun):
            self.db.query(table).delete()
        self.db.commit()
        self.db.add(Student(student_no=STU, name="甲", grade="2024"))
        self.db.add(Student(student_no=OTHER, name="乙", grade="2024"))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def add_day(
        self,
        day: date,
        *,
        covered: bool = True,
        own_status: str | None = None,
        student_no: str = STU,
        with_link: bool = True,
    ) -> str:
        """造一天的课：``covered`` 决定它算不算「有效统计日」。

        ``own_status=None`` 表示这天该生**没有任何考勤记录**
        （整节课没来的人不会出现在接口返回里，这就是真实场景）。
        """
        guid = f"c-{day:%Y%m%d}-{student_no[-3:]}"
        start = datetime.combine(day, datetime.min.time()).replace(hour=8)
        self.db.add(
            Course(
                schedule_guid=guid, course_name="高等数学", class_date=day,
                start_time=start, end_time=start + timedelta(minutes=95),
                covered=covered,
            )
        )
        if with_link:
            self.db.add(
                StudentCourse(student_no=student_no, schedule_guid=guid,
                              class_date=day, source="roster")
            )
        if own_status:
            self.db.add(
                AttendanceEvent(
                    source="hanboer", schedule_guid=guid, student_no=student_no,
                    user_type=2, class_date=day, attend_status=own_status,
                    sign_in_time=(start + timedelta(minutes=3)
                                  if own_status == "出勤" else None),
                )
            )
        self.db.commit()
        return guid


class TestRule1NoRecord(_Base):
    """① 学生无记录预警。"""

    def test_three_effective_days_without_record_fires(self) -> None:
        for offset in (1, 2, 3):
            self.add_day(TODAY - timedelta(days=offset))   # 有课、有考勤源、该生无记录
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        kinds = {w.kind for w in got}
        self.assertIn(AlertKind.NO_RECORD, kinds)
        w = next(w for w in got if w.kind is AlertKind.NO_RECORD)
        self.assertEqual(w.days, 3)
        self.assertEqual(w.last_date, TODAY - timedelta(days=1))

    def test_only_two_effective_days_does_not_fire(self) -> None:
        for offset in (1, 2):
            self.add_day(TODAY - timedelta(days=offset))
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(AlertKind.NO_RECORD, {w.kind for w in got})

    def test_days_without_coverage_are_not_counted(self) -> None:
        """**最要紧的边界**：课没有考勤数据的日子不算"无记录"。

        三天课全都没有考勤源（covered=False）→ 系统根本没数据，
        这三天的"没记录"是我们的盲区，不是学生没来 → 不能报无记录预警。
        """
        for offset in (1, 2, 3):
            self.add_day(TODAY - timedelta(days=offset), covered=False)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        kinds = {w.kind for w in got}
        self.assertNotIn(AlertKind.NO_RECORD, kinds,
                         "没有考勤数据的课不能算成学生缺勤")
        self.assertIn(AlertKind.LOW_COVERAGE, kinds)

    def test_present_breaks_the_streak(self) -> None:
        """中间签到过一次，连续就断了。

        构造：更早连续 3 天无记录、然后签到 1 天、最近 1 天又无记录。
        只数**最近那一段** → 连续 1 天，不够 3 天，不报预警。
        （若把整段历史都算进去就会误报 —— 那正是这条用例要挡的。）
        """
        for offset in (4, 5, 6):
            self.add_day(TODAY - timedelta(days=offset))
        self.add_day(TODAY - timedelta(days=3), own_status="出勤")
        self.add_day(TODAY - timedelta(days=1))
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(
            AlertKind.NO_RECORD, {w.kind for w in got},
            "最近这段只有 1 天无记录，签到那天应当把连续截断",
        )

    def test_leave_counts_as_a_record(self) -> None:
        """请假也算"有记录"——学生报备过，不该报"无记录"。

        构造：连续 4 天有课，最近 3 天都是请假/无记录交替。
        请假那天算记录 → 最近段只有 1 天无记录 → 不报预警。
        （若把请假当成"没记录"，这里就会变成连续 3 天而误报。）
        """
        self.add_day(TODAY - timedelta(days=1), own_status="请假")
        self.add_day(TODAY - timedelta(days=2))
        self.add_day(TODAY - timedelta(days=3), own_status="请假")
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(
            AlertKind.NO_RECORD, {w.kind for w in got},
            "最近一天是请假，请假算有记录，不能报无记录预警",
        )

    def test_consecutive_days_helper_counts_effective_only(self) -> None:
        days = {
            TODAY - timedelta(days=1): None,
        }
        for offset in (1, 2, 3):
            self.add_day(TODAY - timedelta(days=offset),
                         covered=(offset != 3))
        facts = load_day_facts(self.db, {STU}, TODAY - timedelta(days=7), TODAY)
        streak = consecutive_no_record_days(facts[STU], NOW)
        self.assertEqual(len(streak), 2, "第 3 天没有考勤源，不计入")
        _ = days


class TestRule2LowCoverage(_Base):
    """② 监测覆盖不足提醒。"""

    def test_course_without_coverage_in_window_fires(self) -> None:
        for offset in (1, 2, 3):
            self.add_day(TODAY - timedelta(days=offset), covered=False)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        w = next((x for x in got if x.kind is AlertKind.LOW_COVERAGE), None)
        self.assertIsNotNone(w)
        self.assertEqual(w.days, 3)

    def test_no_class_means_no_coverage_warning(self) -> None:
        """没课就不该报覆盖不足（否则全员长期挂着一条提醒）。"""
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertEqual(got, [])

    def test_class_outside_window_is_ignored(self) -> None:
        """窗口外（8 天前）的课不算 7 天统计周期内。"""
        self.add_day(TODAY - timedelta(days=8), covered=False)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertEqual([w.kind for w in got], [], )

    def test_one_covered_day_is_enough_to_avoid_coverage_warning(self) -> None:
        self.add_day(TODAY - timedelta(days=1), covered=False)
        self.add_day(TODAY - timedelta(days=2), covered=True)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(AlertKind.LOW_COVERAGE, {w.kind for w in got})


class TestRule3LongTermMissing(_Base):
    """③ 长期缺少记录提醒。"""

    def _seed_sparse(self, days: int = 5, recorded: int = 0) -> None:
        for offset in range(1, days + 1):
            self.add_day(
                TODAY - timedelta(days=offset),
                own_status="出勤" if offset <= recorded else None,
            )

    def test_sparse_records_fires(self) -> None:
        """记录"持续偏少"但**还没到**连续 3 天无记录 → 报长期缺少记录。

        构造 8 个有效统计日（``offset`` 越大越早），只在 **1 天前**和 **4 天前** 签到：

        - 最近一段（最近一天就有签到）连续无记录 = 0 天 → 规则 1 不成立；
        - 中间最长的一段无记录是 3 天前~1 天前？不，是「2、3 天前」共 2 天
          以及「5~8 天前」共 4 天，都不在最近 → 规则 1 仍不成立；
        - 但 8 天里只有 2 天有记录（25% ≤ 30%）→ 落到规则 3。

        （签到日必须包含**最近一天**：规则 1 只看最近那一段连续无记录，
        把签到放在最近一天，两条规则才真正区分得开。实测踩过坑 ——
        offset 方向搞反会把签到排到窗口最远端，最近端反而连出 5 天空档。）
        """
        recorded_offsets = {1, 4}
        for offset in range(1, 9):
            self.add_day(
                TODAY - timedelta(days=offset),
                own_status="出勤" if offset in recorded_offsets else None,
            )
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(
            AlertKind.NO_RECORD, {w.kind for w in got},
            "最近一天就有签到，连续无记录不足 3 天，规则 1 不该成立",
        )
        w = next((x for x in got if x.kind is AlertKind.LONG_TERM_MISSING), None)
        self.assertIsNotNone(w, "8 天有效统计日只签到 2 天 → 应报长期缺少记录")
        self.assertEqual(w.detail["effective_days"], 8)
        self.assertEqual(w.detail["recorded_days"], 2)
        self.assertEqual(w.detail["ratio"], 0.25)

    def test_good_attendance_does_not_fire(self) -> None:
        self._seed_sparse(days=5, recorded=5)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(AlertKind.LONG_TERM_MISSING, {w.kind for w in got})

    def test_single_day_is_not_long_term(self) -> None:
        """只上过一天课、恰好没记录 —— 那是偶发，不是"长期"。"""
        self._seed_sparse(days=1, recorded=0)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        self.assertNotIn(AlertKind.LONG_TERM_MISSING, {w.kind for w in got})

    def test_low_coverage_takes_precedence(self) -> None:
        """规则明文：覆盖不足时生成覆盖不足提醒，**不判**学生缺少考勤记录。"""
        for offset in range(1, 6):
            self.add_day(TODAY - timedelta(days=offset), covered=False)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        kinds = {w.kind for w in got}
        self.assertIn(AlertKind.LOW_COVERAGE, kinds)
        self.assertNotIn(AlertKind.LONG_TERM_MISSING, kinds,
                         "覆盖不足时不能判成学生缺少记录")

    def test_no_record_warning_replaces_long_term(self) -> None:
        """已经达到"连续 3 个有效统计日无记录"时，不再重复报长期缺少。

        （两者语义重叠：连续 3 天无记录本身就已经是"记录持续偏少"，
        再报一条长期缺少只会示示例辅导员的待办翻倍。）
        """
        self._seed_sparse(days=5, recorded=0)
        got = evaluate_warnings(self.db, {STU}, now=NOW).get(STU, [])
        kinds = {w.kind for w in got}
        self.assertIn(AlertKind.NO_RECORD, kinds)
        self.assertNotIn(AlertKind.LONG_TERM_MISSING, kinds)


class TestRule4DataAnomaly(_Base):
    """④ 数据异常提醒（面向管理员，与具体学生无关）。"""

    def test_failed_collect_run_fires(self) -> None:
        self.db.add(
            CollectRun(source="all", job="poll", started_at=NOW,
                       finished_at=NOW, ok=False, message="接口超时")
        )
        self.db.commit()
        got = data_anomaly_warnings(self.db, now=NOW)
        self.assertTrue(any("采集失败" in w.reason for w in got))

    def test_orphan_links_fire(self) -> None:
        """选课关系指向不存在的课程行。

        ``student_courses.schedule_guid`` 有外键，正常路径插不进这种数据，
        所以先建关系、再把课程行删掉（历史脏数据就是这么来的）。
        """
        self.add_day(TODAY - timedelta(days=1))
        # 外键约束不允许插孤儿关系（删课程行会被级联清掉），
        # 所以临时关掉外键插一条 —— 模拟人工改库留下的脏数据。
        from sqlalchemy import text

        self.db.execute(text("PRAGMA foreign_keys = OFF"))
        self.db.execute(
            text(
                "INSERT INTO student_courses (student_no, schedule_guid,"
                " class_date, source, created_at) VALUES"
                " (:no, 'ghost-course', :d, 'roster', :now)"
            ),
            {"no": STU, "d": TODAY.isoformat(), "now": NOW.isoformat()},
        )
        self.db.execute(text("PRAGMA foreign_keys = ON"))
        self.db.commit()
        got = data_anomaly_warnings(self.db, now=NOW)
        self.assertTrue(any("无法匹配" in w.reason for w in got), got)

    def test_course_without_date_and_coverage_fires(self) -> None:
        self.db.add(Course(schedule_guid="dangling", course_name="思政",
                           covered=False, class_date=None))
        self.db.add(StudentCourse(student_no=STU, schedule_guid="dangling",
                                  class_date=None, source="roster"))
        self.db.commit()
        got = data_anomaly_warnings(self.db, now=NOW)
        self.assertTrue(any("课表数据缺失" in w.reason for w in got))

    def test_covered_course_without_any_event_fires(self) -> None:
        self.add_day(TODAY - timedelta(days=2), covered=True)   # 有考勤源但一条事件都没有
        got = data_anomaly_warnings(self.db, now=NOW)
        self.assertTrue(any("没有产生任何考勤数据" in w.reason for w in got))

    def test_healthy_data_produces_no_anomaly(self) -> None:
        self.add_day(TODAY - timedelta(days=1), own_status="出勤")
        got = data_anomaly_warnings(self.db, now=NOW)
        self.assertEqual(got, [])


class TestCoverageIsPerStudent(_Base):
    """覆盖判断要落到**该生选的那些课**上，不能用全校口径。"""

    def test_another_students_covered_course_does_not_help(self) -> None:
        """乙的课有考勤源，不代表甲就有覆盖。

        甲选的课没有考勤源 → 甲应当收到覆盖不足提醒（他这节课我们看不到），
        不能因为"系统里别的课有数据"就当作甲的覆盖没问题。
        """
        self.add_day(TODAY - timedelta(days=1), covered=False, student_no=STU)
        self.add_day(TODAY - timedelta(days=1), covered=True, student_no=OTHER,
                     own_status="出勤")
        got = evaluate_warnings(self.db, {STU, OTHER}, now=NOW)
        self.assertIn(AlertKind.LOW_COVERAGE, {w.kind for w in got[STU]})
        self.assertNotIn(AlertKind.LOW_COVERAGE, {w.kind for w in got.get(OTHER, [])})


if __name__ == "__main__":
    unittest.main()
