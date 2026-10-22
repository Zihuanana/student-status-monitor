# -*- coding: utf-8 -*-
"""教务课表解析与导入的回归测试。

锁的是两个实测踩点：

1. **一格里的多段排课用空格分隔，逗号是"周次"内部的分隔符**
   —— 按逗号切会把一段排课切碎，节次全部丢失（解析出 0 段而**不报错**）；
2. **同名课程要合并成一行** —— 本院有两个「国际背景下的研究」
   （课序号 01 / 01S01），但选课名单里没有课序号这一列，
   按课序号分行会让选课关系挂错。

外加一条最危险的：**导入课次时间之后，没有考勤源的课依然不能变成缺勤**。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, time

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("teach_schedule")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.db import SessionLocal  # noqa: E402
from app.models import Course, Student, StudentCourse  # noqa: E402
from app.services.teach_schedule import (  # noqa: E402
    expand_dates,
    meeting_dates,
    parse_meetings,
    period_time,
    slot_times,
    weeks_label,
)


class TestPeriodTiming(unittest.TestCase):
    """节次 → 时刻。这组数字是从翰博尔已有课表反推的，错了会显示错时间。"""

    def test_single_period(self) -> None:
        self.assertEqual(period_time(1), (time(8, 0), time(8, 50)))
        self.assertEqual(period_time(7), (time(15, 50), time(16, 40)))

    def test_span_covers_breaks(self) -> None:
        """1-4 节是 08:00-12:00（含中间两个 10 分钟课间），不是 4×50 分钟。"""
        self.assertEqual(slot_times("1-4"), (time(8, 0), time(12, 0)))
        self.assertEqual(slot_times("7-8"), (time(15, 50), time(17, 30)))
        self.assertEqual(slot_times("9-10"), (time(18, 30), time(20, 10)))

    def test_unknown_period_returns_none(self) -> None:
        """超出已知范围不要瞎猜。"""
        self.assertIsNone(period_time(13))
        self.assertIsNone(slot_times("13-14"))
        self.assertIsNone(slot_times(""))


class TestParseMeetings(unittest.TestCase):
    """「上课时间地点」的解析。"""

    def test_single_meeting(self) -> None:
        got = parse_meetings("5-7周,9-17周 星期四 7-8节 A402")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].weekday, 3)          # 星期四
        self.assertEqual(got[0].node, "7-8")
        self.assertEqual(got[0].room, "A402")
        self.assertEqual(got[0].weeks, [5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17])

    def test_multiple_meetings_are_space_separated(self) -> None:
        """回归锁定：逗号不能当段分隔符 —— 它在周次内部也是分隔符。

        真实数据：``1-2周,5-7周,9-16周 星期五 3-4节 A402,2周 星期日 3-4节 A402``
        按逗号切会变成 4 段，只有最后一段能解析出节次。
        """
        text = (
            "1-2周,5-7周,9-16周 星期五 3-4节 A402,"
            "2周 星期日 3-4节 A402"
        )
        got = parse_meetings(text)
        self.assertEqual(len(got), 2, "应当正好解析出 2 段排课")
        self.assertEqual(got[0].weekday, 4)   # 星期五
        self.assertEqual(got[0].node, "3-4")
        self.assertEqual(got[0].weeks, [1, 2, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16])
        self.assertEqual(got[1].weekday, 6)   # 星期日
        self.assertEqual(got[1].weeks, [2])

    def test_three_meetings(self) -> None:
        got = parse_meetings(
            "5-7周,9-17周 星期四 7-8节 A402,5-7周,9-14周 星期五 7-8节 A402,"
            "6-8周,10-17周 星期一 1-2节 A407"
        )
        self.assertEqual([m.weekday for m in got], [3, 4, 0])
        self.assertEqual([m.room for m in got], ["A402", "A402", "A407"])

    def test_empty_text(self) -> None:
        self.assertEqual(parse_meetings(""), [])
        self.assertEqual(parse_meetings("待定"), [])

    def test_weeks_are_deduped_and_sorted(self) -> None:
        got = parse_meetings("9-16周,5-7周,9周 星期五 3-4节 A402")
        self.assertEqual(got[0].weeks, sorted({5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16}))


class TestDateExpansion(unittest.TestCase):
    def test_meeting_dates(self) -> None:
        week1 = date(2026, 9, 7)  # 周一
        m = parse_meetings("5周 星期四 3-4节 C402")[0]
        # 第 5 周的周四是 09-07 + 4 周 + 3 天 = 10-08
        self.assertEqual(meeting_dates(m, week1), [date(2026, 10, 8)])

    def test_expand_dates_sorted_and_deduped(self) -> None:
        week1 = date(2026, 9, 7)
        ms = parse_meetings(
            "5周 星期四 3-4节 C402,6周 星期四 3-4节 C402,5周 星期四 9-9节 C402"
        )
        self.assertEqual(
            expand_dates(ms, week1),
            [date(2026, 10, 8), date(2026, 10, 15)],
        )

    def test_weeks_label_compresses_runs(self) -> None:
        self.assertEqual(weeks_label([5, 6, 7, 9, 10, 11, 12]), "5-7周,9-12周")
        self.assertEqual(weeks_label([5]), "5周")
        self.assertEqual(weeks_label([]), "")


class TestImportTeachingSchedule(unittest.TestCase):
    """导入本身：同名合并、不覆盖考勤源标记、不把看不到的课变成缺勤。"""

    CSV = (
        "课程名,课序号,上课时间地点,课程号,上课教师,开课单位\n"
        "计算机网络B,01,\"5-7周,9-17周 星期四 3-4节 C402\",2291000001,示示例辅导员,海南国际学院\n"
        "国际背景下的研究,01,\"10-11周 星期一 5-6节 C204\",2291000001,示示例辅导员,海南国际学院\n"
        "国际背景下的研究,01S01,\"10-11周 星期一 7-8节 A304\",2291000001,示示例辅导员,海南国际学院\n"
    )

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _write(self, text: str) -> "os.PathLike[str]":
        import tempfile
        from pathlib import Path

        p = Path(tempfile.mkdtemp(prefix="sm-teach-")) / "课表.csv"
        p.write_bytes(text.encode("gb18030"))
        return p

    def test_import_creates_course_with_class_date(self) -> None:
        from app.services.teach_schedule import import_teaching_schedule

        stat = import_teaching_schedule(
            self.db, self._write(self.CSV), week1_monday=date(2026, 9, 7)
        )
        self.assertEqual(stat["rows"], 3)
        self.assertEqual(stat["unparsed"], 0)

        net = self.db.query(Course).filter_by(course_name="计算机网络B").one()
        # 第 5 周的周四是 2026-10-08；3-4 节 = 10:10-12:00
        self.assertEqual(net.class_date, date(2026, 10, 8))
        self.assertEqual(net.start_time.time(), time(10, 10))
        self.assertEqual(net.end_time.time(), time(12, 0))
        self.assertEqual(net.classroom_name, "C402")
        self.assertEqual(net.course_no, "2291000001")
        self.assertEqual(net.week_name, "5-7周,9-17周")
        # 教务排课不提供出勤，新建的课必须是"没有考勤源"
        self.assertFalse(net.covered)

    def test_same_name_sections_merge_by_slot(self) -> None:
        """同名不同课序号 → 按**时段**合并（不是按课程压成一行）。

        选课名单里没有课序号列，按课序号分行会让选课关系只能挂到一行上。
        但也不能把一门课压成一行 —— 一门课每周上多次，压成一行后
        周课表一周就只显示一次（见 test_teach_slots.py）。

        这条用例里两个教学班的排课分别是「星期一 5-6 节」和「星期一 7-8 节」，
        所以应当得到 **2 行**（两个时段），而不是 1 行或 2 门课。
        """
        from app.services.teach_schedule import import_teaching_schedule

        import_teaching_schedule(
            self.db, self._write(self.CSV), week1_monday=date(2026, 9, 7)
        )
        rows = (
            self.db.query(Course)
            .filter_by(course_name="国际背景下的研究")
            .order_by(Course.start_time)
            .all()
        )
        self.assertEqual(len(rows), 2, "两个时段应当两行")
        self.assertEqual([r.node_name for r in rows], ["5-6", "7-8"])
        self.assertEqual({r.week_name for r in rows}, {"10-11周"})

    def test_existing_covered_flag_is_kept(self) -> None:
        """导入时间不能把"有考勤源"的课打成没考勤源（反之亦然）。"""
        from app.services.teach_schedule import import_teaching_schedule

        self.db.add(
            Course(
                schedule_guid="hanboer-1",
                course_name="计算机网络B",
                covered=True,
                course_no="2291000001",
            )
        )
        self.db.commit()

        import_teaching_schedule(
            self.db, self._write(self.CSV), week1_monday=date(2026, 9, 7)
        )
        row = (
            self.db.query(Course)
            .filter_by(course_name="计算机网络B", covered=True)
            .one()
        )
        self.assertTrue(row.covered, "已有考勤源的课不能因为导入课表而变成看不到")
        self.assertEqual(row.schedule_guid, "hanboer-1",
                         "应当认领已有课程行，主键不变")
        self.assertIsNotNone(row.class_date, "认领的同时也要补上课次时间")

    def test_dry_run_does_not_write(self) -> None:
        from app.services.teach_schedule import import_teaching_schedule

        import_teaching_schedule(
            self.db, self._write(self.CSV), week1_monday=date(2026, 9, 7), dry_run=True
        )
        self.assertEqual(self.db.query(Course).count(), 0)

    def test_imported_uncovered_course_still_not_an_absence(self) -> None:
        """最危险的一条：补上课次时间之后，看不到的课依然不能变成缺勤。

        课次时间让这些课进入了"某天有课"的计算范围 —— 一旦保险失守，
        名单那 93 门课上的学生会被集体误报。
        """
        from app.services.ingest import _load_day_evaluations
        from app.services.teach_schedule import import_teaching_schedule

        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.commit()
        import_teaching_schedule(
            self.db, self._write(self.CSV), week1_monday=date(2026, 9, 7)
        )
        net = self.db.query(Course).filter_by(course_name="计算机网络B").one()
        self.db.add(
            StudentCourse(student_no="202400000001N",
                          schedule_guid=net.schedule_guid,
                          class_date=net.class_date, source="roster")
        )
        self.db.commit()

        from datetime import datetime

        got = _load_day_evaluations(
            self.db, {"202400000001N"}, net.class_date, net.class_date,
            datetime(2026, 10, 8, 23, 0), include_unobserved=True,
        )
        days = got.get("202400000001N") or []
        self.assertTrue(days, "应当有天记录（这天有课）")
        d = days[0]
        self.assertEqual(d.no_source_count, 1)
        self.assertEqual(d.missed_count, 0, "看不到的课不能算缺勤")
        self.assertFalse(d.all_missed, "不能判成整日无签到")


if __name__ == "__main__":
    unittest.main()
