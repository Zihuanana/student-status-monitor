# -*- coding: utf-8 -*-
"""总览性能与「双路径一致性」测试。

背景
----
`dashboard_rows()` 原先按「学生 × 每节课」装载：1200 人 / 60 天就是 15.8 万行
对象，实测 4.6 秒；按保留期 400 天推算约 30 秒，页面基本不可用。

优化后新增了**按天聚合**的判定入口 `evaluate_from_days()`，与逐节课的
`evaluate()` 并存（后者仍是周课表等明细场景的路径）。两条路径规则必须一致 ——
否则会出现"总览显示异常、点开课表却正常"这类互相矛盾的结论。

因此本模块做两件事：
  1. **一致性**：同一份数据分别走两条路径，判定结果必须完全相同；
  2. **性能护栏**：真实规模下总览必须在一秒级完成。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import date, datetime, time as dtime, timedelta

from tests import dbguard  # noqa: E402

# 必须在导入 app.* 之前隔离数据库 —— app.core.db 的引擎在首个导入者处
# 就已绑定，晚设置会连到工作区真实库（曾把演示数据清空）。
_DB_PATH = dbguard.isolate("performance")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")
os.environ.setdefault("DATA_MODE", "mock")

from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceDaily,
    AttendanceEvent,
    Course,
    Student,
)
from app.services.attendance import (  # noqa: E402
    DayEvaluation,
    SessionFact,
    evaluate,
    evaluate_from_days,
    summarize_student,
)
from app.services.ingest import _load_day_evaluations  # noqa: E402

NOW = datetime(2026, 3, 11, 15, 0, 0)
TODAY = NOW.date()


def _session(
    day: date,
    guid: str,
    *,
    present: bool = False,
    leave: bool = False,
    absent: bool = False,
    hour: int = 8,
    duration: int = 2,
) -> SessionFact:
    start = datetime.combine(day, dtime(hour, 0))
    return SessionFact(
        schedule_guid=f"{guid}-{day}-{hour}",
        class_date=day,
        start_time=start,
        end_time=start + timedelta(hours=duration),
        present=present,
        leave=leave,
        absent_recorded=absent,
        sign_in_time=start + timedelta(minutes=3) if present else None,
    )


def _facts_to_days(facts: list[SessionFact], now: datetime) -> list[DayEvaluation]:
    """用与 `_load_day_evaluations` 相同的聚合口径，把逐节课事实折成按天。"""
    summaries = summarize_student(facts, now)
    out: list[DayEvaluation] = []
    for d, s in summaries.items():
        out.append(
            DayEvaluation(
                class_date=d,
                session_count=s.session_count,
                present_count=s.present_count,
                missed_count=s.missed_count,
                pending_count=s.pending_count,
                leave_count=s.leave_count,
                day_complete=s.day_complete,
                all_missed=s.all_missed,
                last_sign_in_time=s.last_sign_in_time,
            )
        )
    return out


class TestTwoPathsAgree(unittest.TestCase):
    """逐节课路径与按天聚合路径必须给出完全相同的判定。"""

    def _assert_same(self, facts: list[SessionFact], label: str) -> None:
        a = evaluate(facts, now=NOW)
        b = evaluate_from_days(_facts_to_days(facts, NOW), now=NOW)
        self.assertEqual(a.state, b.state, f"{label}：状态不一致 {a.state} vs {b.state}")
        self.assertEqual(
            a.consecutive_missed_days,
            b.consecutive_missed_days,
            f"{label}：连续天数不一致",
        )
        self.assertEqual(a.last_class_date, b.last_class_date, f"{label}：最近有课日不一致")

    def test_no_class(self) -> None:
        self._assert_same([], "无课")

    def test_active_today(self) -> None:
        self._assert_same([_session(TODAY, "a", present=True)], "今日已签到")

    def test_abnormal_yesterday(self) -> None:
        self._assert_same(
            [_session(TODAY - timedelta(days=1), "a", absent=True)], "昨日缺勤"
        )

    def test_severe_three_days(self) -> None:
        facts = [
            _session(TODAY - timedelta(days=k), "a", absent=True) for k in (1, 2, 3)
        ]
        self._assert_same(facts, "连续三天缺勤")

    def test_pending_today(self) -> None:
        # 14:00 开始、NOW=15:00 → 进行中
        self._assert_same([_session(TODAY, "a", hour=14)], "今日进行中")

    def test_upcoming_today(self) -> None:
        self._assert_same([_session(TODAY, "a", hour=16)], "今日未开始")

    def test_leave_breaks_streak(self) -> None:
        facts = [_session(TODAY - timedelta(days=1), "a", leave=True)]
        facts += [
            _session(TODAY - timedelta(days=k), "b", absent=True) for k in (2, 3, 4)
        ]
        self._assert_same(facts, "请假打断连续")

    def test_multi_session_day(self) -> None:
        facts = [
            _session(TODAY - timedelta(days=1), "a", hour=8, absent=True),
            _session(TODAY - timedelta(days=1), "b", hour=10, absent=True),
            _session(TODAY - timedelta(days=2), "c", absent=True),
            _session(TODAY - timedelta(days=3), "d", absent=True),
        ]
        self._assert_same(facts, "一天多节课")

    def test_mixed_attendance_today(self) -> None:
        facts = [
            _session(TODAY, "a", hour=8, present=True),
            _session(TODAY, "b", hour=10, absent=True),
        ]
        self._assert_same(facts, "今日一节签到一节缺勤")


class TestLoaderMatchesSessions(unittest.TestCase):
    """SQL 按天聚合的字段口径必须与 Python 逐节课汇总一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def _add_course(self, day: date, guid: str, hour: int, duration: int = 2) -> None:
        start = datetime.combine(day, dtime(hour, 0))
        self.db.add(
            Course(
                schedule_guid=guid,
                course_name="测试课",
                class_date=day,
                start_time=start,
                end_time=start + timedelta(hours=duration),
            )
        )

    def _add_event(
        self, day: date, guid: str, no: str, status: str, signed: datetime | None = None
    ) -> None:
        self.db.add(
            AttendanceEvent(
                source="hanboer",
                schedule_guid=guid,
                student_no=no,
                user_type=2,
                class_date=day,
                attend_status=status,
                sign_in_time=signed,
            )
        )

    def test_counts_match_session_level(self) -> None:
        no = "2024010001"
        self.db.add(Student(student_no=no, name="测试", grade="2024", major_code="01"))
        # 三天：前天缺勤、昨天签到、今天进行中（尚未结束）
        for offset, hour in ((2, 8), (1, 8)):
            day = TODAY - timedelta(days=offset)
            guid = f"g-{day}"
            self._add_course(day, guid, hour)
            status = "缺勤" if offset == 2 else "出勤"
            signed = None if offset == 2 else datetime.combine(day, dtime(hour, 3))
            self._add_event(day, guid, no, status, signed)
        # 今天 14:00 开始、NOW=15:00 → 进行中，且没有签到记录
        guid_today = f"g-{TODAY}-14"
        self._add_course(TODAY, guid_today, 14)
        self._add_event(TODAY, guid_today, no, "缺勤")
        self.db.commit()

        days = _load_day_evaluations(self.db, {no}, TODAY - timedelta(days=10), TODAY, NOW)
        by_date = {d.class_date: d for d in days[no]}

        two = by_date[TODAY - timedelta(days=2)]
        self.assertEqual(two.session_count, 1)
        self.assertEqual(two.missed_count, 1)
        self.assertEqual(two.present_count, 0)
        self.assertTrue(two.day_complete)
        self.assertTrue(two.all_missed)

        one = by_date[TODAY - timedelta(days=1)]
        self.assertEqual(one.present_count, 1)
        self.assertEqual(one.missed_count, 0)
        self.assertTrue(one.day_complete)
        self.assertFalse(one.all_missed)
        self.assertIsNotNone(one.last_sign_in_time)

        # 今天：课程未结束 → pending，绝不能算 all_missed
        today = by_date[TODAY]
        self.assertEqual(today.pending_count, 1)
        self.assertFalse(today.day_complete)
        self.assertFalse(today.all_missed, "进行中的课不能被当成整日无签到")

    def test_teacher_events_excluded(self) -> None:
        """老师（userType=1）的签到记录不能混进学生统计。"""
        no = "2024010002"
        self.db.add(Student(student_no=no, name="测试2", grade="2024", major_code="01"))
        day = TODAY - timedelta(days=1)
        guid = "g-teacher"
        self._add_course(day, guid, 8)
        self._add_event(day, guid, no, "出勤", datetime.combine(day, dtime(8, 3)))
        self.db.add(
            AttendanceEvent(
                source="hanboer", schedule_guid=guid, student_no="0107t1",
                user_type=1, class_date=day, attend_status="出勤",
            )
        )
        self.db.commit()

        days = _load_day_evaluations(self.db, {no, "0107t1"}, TODAY - timedelta(days=10), TODAY, NOW)
        self.assertIn(no, days)
        self.assertNotIn("0107t1", days, "教师记录不应出现在学生统计中")


class TestWriteScope(unittest.TestCase):
    """采集只应写回**真正变化的日期**，不能整段窗口重写。

    早期实现每轮把 60 天窗口全部 DELETE + INSERT：1200 人 × 60 天 = 5.28 万行，
    而实际变化的只有今天 1200 行 —— 44 倍写放大，且长事务长时间持有写锁。
    这个回归不会让任何功能测试失败（结果完全一样），只会让每轮采集从 8 秒变 20 秒。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceDaily, AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()
        self._seed_history()

    def tearDown(self) -> None:
        self.db.close()

    def _seed_history(self, students: int = 40, days: int = 30) -> None:
        now = datetime.now()
        rows = [
            Student(
                student_no=f"2024{i:06d}", name=f"学生{i}", grade="2024",
                major_code="01", major_name="智能科学与技术", class_name="智能241班",
            )
            for i in range(students)
        ]
        self.db.bulk_save_objects(rows)
        self.db.commit()

        courses, events = [], []
        # 从**昨天**往前铺历史；今天留给 _poll 制造变化，
        # 否则"今天的行已存在"，测不出本轮到底写了多少行。
        for d in range(1, days + 1):
            day = now.date() - timedelta(days=d)
            if day.weekday() >= 5:
                continue
            guid = f"hist-{day:%Y%m%d}"
            st = datetime.combine(day, dtime(8, 0))
            courses.append(
                Course(
                    schedule_guid=guid, course_name="历史课", class_name="智能241班",
                    class_date=day, start_time=st,
                    end_time=st + timedelta(hours=1, minutes=35),
                )
            )
            for i in range(students):
                events.append(
                    AttendanceEvent(
                        source="hanboer", schedule_guid=guid,
                        student_no=f"2024{i:06d}", user_type=2, class_date=day,
                        attend_status="出勤", sign_in_time=st + timedelta(minutes=3),
                    )
                )
        self.db.bulk_save_objects(courses)
        self.db.bulk_save_objects(events)
        self.db.commit()

        from app.services.ingest import rebuild_daily

        rebuild_daily(
            self.db,
            start=now.date() - timedelta(days=days),
            end=now.date() - timedelta(days=1),
            now=now,
        )
        self.now = now
        self.students = students

    def _poll(self) -> dict:
        """模拟一轮采集：今天新增一节课 + 每个学生一条签到。"""
        from app.adapters.base import NormalizedCourse, NormalizedEvent
        from app.services.ingest import rebuild_from_events, upsert_courses

        now = datetime.now()
        guid = f"today-{now:%Y%m%d%H%M%S%f}"
        st = now - timedelta(hours=1)
        upsert_courses(
            self.db,
            [
                NormalizedCourse(
                    schedule_guid=guid, source="hanboer", course_name="新课",
                    class_name="智能241班", start_time=st,
                    end_time=st + timedelta(minutes=40),
                )
            ],
        )
        self.db.commit()
        events = [
            NormalizedEvent(
                source="hanboer", schedule_guid=guid, student_no=f"2024{i:06d}",
                class_date=now.date(), attend_status="出勤",
                sign_in_time=st + timedelta(minutes=2),
            )
            for i in range(self.students)
        ]
        return rebuild_from_events(self.db, events, now=now)

    def _count_daily(self) -> int:
        """用新会话计数：rebuild 内部可能用的是另一个 Session，
        在当前会话里直接 `query().count()` 会读到过期的身份映射。"""
        from app.core.db import SessionLocal as _Session

        s = _Session()
        try:
            return s.query(AttendanceDaily).count()
        finally:
            s.close()

    def test_daily_rows_written_only_for_changed_date(self) -> None:
        before = self._count_daily()
        stats = self._poll()
        after = self._count_daily()

        self.assertEqual(
            stats["daily"],
            self.students,
            f"应只写回今天的 {self.students} 行，实际 {stats['daily']} 行（写放大）",
        )
        # 每个学生今天新增一行；若整段窗口重写，这里会远大于 students
        self.assertEqual(after - before, self.students)

    def test_history_rows_survive_poll(self) -> None:
        """收窄写回范围后，历史快照不能被误删。"""
        before = self._count_daily()
        self._poll()
        after = self._count_daily()
        self.assertGreaterEqual(
            before, self.students * 10, "前置数据不足，断言没有意义"
        )
        self.assertGreaterEqual(after, before, "历史天快照不应减少")

    def test_verdict_still_uses_full_history(self) -> None:
        """写回范围收窄了，但判定仍必须看完整历史 —— 否则连续异常会算错。"""
        # 让所有学生连续缺勤（历史 30 天全缺勤 + 今天缺勤）
        self.db.query(AttendanceEvent).filter(
            AttendanceEvent.user_type == 2
        ).delete()
        now = datetime.now()
        # 往回取 4 个**工作日**。不能写死 `range(1, 5)`：那样在周一/周二
        # 跨过周末后只凑得出 2 个工作日，"连续三天"的断言必然失败 ——
        # 这个测试曾经因此**按星期几翻车**，只是之前没在周一跑过。
        workdays: list[date] = []
        probe = now.date() - timedelta(days=1)
        while len(workdays) < 4:
            if probe.weekday() < 5:
                workdays.append(probe)
            probe -= timedelta(days=1)
        for day in workdays:
            guid = f"hist-{day:%Y%m%d}"
            for i in range(self.students):
                self.db.add(
                    AttendanceEvent(
                        source="hanboer", schedule_guid=guid,
                        student_no=f"2024{i:06d}", user_type=2,
                        class_date=day, attend_status="缺勤",
                    )
                )
        self.db.commit()

        from app.services.ingest import dashboard_rows

        rows = {r["student_no"]: r for r in dashboard_rows(self.db)}
        sample = rows["2024000000"]
        self.assertEqual(sample["state"], "严重异常", sample["reason"])
        self.assertGreaterEqual(sample["consecutive_missed_days"], 3)


class TestDashboardPerformance(unittest.TestCase):
    """性能护栏：真实规模下总览必须一秒级完成。

    这条断言是为了防止日后有人把按天聚合改回逐节课装载 ——
    那种改动不会让任何功能测试失败，只会让页面变慢十几倍。
    """

    STUDENTS = 1200
    DAYS = 60
    SESSIONS = 3
    BUDGET_SECONDS = 3.0

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def _seed(self) -> None:
        now = datetime.now()
        start_day = now.date() - timedelta(days=self.DAYS - 1)
        students = []
        for i in range(self.STUDENTS):
            grade = 2021 + (i % 4)
            students.append(
                Student(
                    student_no=f"{grade}{(i % 6) + 1:02d}{i:04d}",
                    name=f"学生{i:04d}",
                    grade=str(grade),
                    major_code=f"{(i % 6) + 1:02d}",
                    major_name="智能科学与技术",
                    class_name=f"智能{grade % 100}{(i % 3) + 1}班",
                )
            )
        self.db.bulk_save_objects(students)
        self.db.commit()
        nos = [s.student_no for s in students]

        courses, events = [], []
        for d in range(self.DAYS):
            day = start_day + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            for s in range(self.SESSIONS):
                guid = f"perf-{day:%Y%m%d}-{s}"
                st = datetime.combine(day, dtime(8 + s * 2, 0))
                courses.append(
                    Course(
                        schedule_guid=guid, course_name=f"课程{s}",
                        class_name="智能241班", class_date=day,
                        start_time=st, end_time=st + timedelta(hours=1, minutes=35),
                    )
                )
                for idx, no in enumerate(nos):
                    absent = idx % 17 == 0
                    events.append(
                        AttendanceEvent(
                            source="hanboer", schedule_guid=guid, student_no=no,
                            user_type=2, class_date=day,
                            attend_status="缺勤" if absent else "出勤",
                            sign_in_time=None if absent else st + timedelta(minutes=3),
                        )
                    )
        for i in range(0, len(courses), 5000):
            self.db.bulk_save_objects(courses[i : i + 5000])
            self.db.commit()
        for i in range(0, len(events), 20000):
            self.db.bulk_save_objects(events[i : i + 20000])
            self.db.commit()

    def test_dashboard_within_budget(self) -> None:
        from app.services.ingest import dashboard_rows

        self._seed()
        events = self.db.query(AttendanceEvent).count()
        self.assertGreater(events, 100_000, "数据量不足，性能断言没有意义")

        # 共用测试库：其它测试类可能留下学生，这里只要求"至少含本模块种下的这批"
        seeded = self.db.query(Student).count()
        self.assertGreaterEqual(seeded, self.STUDENTS)

        # 预热（首次查询含 SQLite 页缓存冷启动）
        dashboard_rows(self.db)

        t0 = time.perf_counter()
        rows = dashboard_rows(self.db)
        elapsed = time.perf_counter() - t0

        self.assertGreaterEqual(len(rows), self.STUDENTS)
        self.assertLess(
            elapsed,
            self.BUDGET_SECONDS,
            f"{events:,} 条事件下总览耗时 {elapsed:.2f}s，超过预算 {self.BUDGET_SECONDS}s",
        )
        print(f"\n    [性能] {events:,} 条事件 / {len(rows)} 人 → {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
