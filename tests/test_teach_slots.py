# -*- coding: utf-8 -*-
"""回归测试：一门课每周上多次时，每个时段都要出现。

真实数据（教务处课程表实测）：72 门课里 **64 门每周上 2 次以上**，
最多的一门有 18 个「星期+节次」时段。例如「计算机网络B」：

    星期四 3-4节（第 5-7、9-17 周）
    星期五 7-8节（第 5-7、9-16 周）
    星期六 3-4节（第 5 周）

早期实现「一门课 = 一行，只取最早那段排课」，结果这门课一周只显示一次 ——
**数据是够的，是实现偷懒**。本模块锁住修正后的行为。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("teach_slots")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.db import SessionLocal  # noqa: E402
from app.models import Course, Student, StudentCourse  # noqa: E402
from app.services.teach_schedule import import_teaching_schedule  # noqa: E402

WEEK1 = date(2026, 9, 7)  # 第 1 周周一

# 计算机网络B 的真实排课（教务处导出原文）
NET_CSV = (
    "课程名,课序号,上课时间地点,课程号,上课教师,开课单位\n"
    "计算机网络B,01,\"5-7周,9-17周 星期四 3-4节 C402,"
    "5-7周,9-16周 星期五 7-8节 C405,5周 星期六 3-4节 C405\","
    "2291000001,倪业鹏,海南国际学院\n"
)


def write_csv(text: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix="sm-slots-")) / "课表.csv"
    p.write_bytes(text.encode("gb18030"))
    return p


class TestWeeklySlots(unittest.TestCase):
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

    def _import(self, text: str) -> dict:
        return import_teaching_schedule(self.db, write_csv(text), week1_monday=WEEK1)

    def test_one_row_per_weekday_slot(self) -> None:
        """三个时段 → 三行，分别落在周四/周五/周六。"""
        self._import(NET_CSV)
        rows = (
            self.db.query(Course)
            .filter_by(course_name="计算机网络B")
            .order_by(Course.class_date)
            .all()
        )
        self.assertEqual(len(rows), 3, "三个时段应当有三行")

        got = [(r.class_date, r.node_name, r.classroom_name) for r in rows]
        self.assertEqual(
            got,
            [
                (date(2026, 10, 8), "3-4", "C402"),   # 第 5 周星期四
                (date(2026, 10, 9), "7-8", "C405"),   # 第 5 周星期五
                (date(2026, 10, 10), "3-4", "C405"),  # 第 5 周星期六
            ],
        )
        # 时间按节次算：3-4 节 10:10-12:00，7-8 节 15:50-17:30
        self.assertEqual(rows[0].start_time.time(), time(10, 10))
        self.assertEqual(rows[0].end_time.time(), time(12, 0))
        self.assertEqual(rows[1].start_time.time(), time(15, 50))
        self.assertEqual(rows[1].end_time.time(), time(17, 30))
        # 全部都是"没有考勤源"
        self.assertTrue(all(not r.covered for r in rows))

    def test_week_view_shows_every_slot(self) -> None:
        """周课表里这一周要能看到全部三个时段（同一门课出现三次）。"""
        from app.services.ingest import student_week

        self._import(NET_CSV)
        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.commit()
        for row in self.db.query(Course).filter_by(course_name="计算机网络B").all():
            self.db.add(
                StudentCourse(student_no="202400000001N",
                              schedule_guid=row.schedule_guid,
                              class_date=None, source="roster")
            )
        self.db.commit()

        week = student_week(
            self.db, "202400000001N", anchor=date(2026, 10, 8),
            now=datetime(2026, 10, 10, 22, 0),
        )
        seen = {
            (d["date"], s["start"], s["course_name"])
            for d in week["days"]
            for s in d["sessions"]
        }
        self.assertIn(("2026-10-08", "10:10", "计算机网络B"), seen)
        self.assertIn(("2026-10-09", "15:50", "计算机网络B"), seen)
        self.assertIn(("2026-10-10", "10:10", "计算机网络B"), seen)

    def test_half_day_slots_do_not_merge(self) -> None:
        """1-2 节与 3-4 节是两段，不能合并成 1-4 节。"""
        self._import(
            "课程名,课序号,上课时间地点,课程号\n"
            "专业英语1,01,\"1-16周 星期二 1-2节 A402,1-16周 星期二 3-4节 A402\","
            "2291000001\n"
        )
        rows = (
            self.db.query(Course)
            .filter_by(course_name="专业英语1")
            .order_by(Course.start_time)
            .all()
        )
        self.assertEqual([r.node_name for r in rows], ["1-2", "3-4"])
        self.assertEqual(rows[0].end_time.time(), time(9, 50))
        self.assertEqual(rows[1].start_time.time(), time(10, 10))

    def test_note_meetings_are_deduped(self) -> None:
        """多个教学班在同一时段上课 → 只留一行（合并周次）。"""
        self._import(
            "课程名,课序号,上课时间地点,课程号\n"
            "国际背景下的研究,01,\"10-11周 星期一 5-6节 C204\",2291000001\n"
            "国际背景下的研究,01S01,\"10-11周 星期一 5-6节 C204\",2291000001\n"
        )
        rows = self.db.query(Course).filter_by(course_name="国际背景下的研究").all()
        self.assertEqual(len(rows), 1)

    def test_reimport_is_idempotent(self) -> None:
        """重复导入不产生重复行（线上要能反复跑）。"""
        self._import(NET_CSV)
        first = self.db.query(Course).filter_by(course_name="计算机网络B").count()
        self._import(NET_CSV)
        second = self.db.query(Course).filter_by(course_name="计算机网络B").count()
        self.assertEqual(first, second, "重复导入不应增加课程行")
        self.assertEqual(second, 3)

    def test_old_row_is_adopted_not_duplicated(self) -> None:
        """早期版本留下的「一门课一行」要被**认领**，不能变出第四行。

        早期实现一门课只建一行（`roster-*`），课表导入必须认领它作为第一段
        —— 既不删除（删课程行会级联带走学生的选课关系），也不另建一行
        （否则同一门课会出现四行、其中一行是没人引用的孤儿）。
        """
        old_guid = "roster-2291000001-cafebabe"
        self.db.add(Course(schedule_guid=old_guid, course_name="计算机网络B",
                           title="计算机网络B", covered=False))
        self.db.commit()

        self._import(NET_CSV)
        rows = self.db.query(Course).filter_by(course_name="计算机网络B").all()
        self.assertEqual(len(rows), 3, "认领旧行后仍应只有三个时段行")
        self.assertIn(old_guid, [r.schedule_guid for r in rows],
                      "旧行应被认领为其中一个时段，而不是被删掉或留下孤儿")
        # 认领来的那一行也要拿到时间与节次
        adopted = next(r for r in rows if r.schedule_guid == old_guid)
        self.assertIsNotNone(adopted.class_date)
        self.assertTrue(adopted.node_name)

    def test_old_row_with_links_keeps_them(self) -> None:
        """认领旧行后，学生在它上面的选课关系必须保住，并补齐到其余时段。"""
        old_guid = "roster-2291000001-cafebabe"
        self.db.add(Course(schedule_guid=old_guid, course_name="计算机网络B",
                           title="计算机网络B", covered=False))
        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.add(StudentCourse(student_no="202400000001N", schedule_guid=old_guid,
                                  class_date=None, source="roster"))
        self.db.commit()

        self._import(NET_CSV)
        rows = self.db.query(Course).filter_by(course_name="计算机网络B").all()
        self.assertEqual(len(rows), 3)
        links = (
            self.db.query(StudentCourse)
            .filter_by(student_no="202400000001N")
            .all()
        )
        self.assertEqual(len(links), 3, "学生要挂到全部三个时段上")
        self.assertEqual(
            {l.schedule_guid for l in links}, {r.schedule_guid for r in rows}
        )


if __name__ == "__main__":
    unittest.main()
