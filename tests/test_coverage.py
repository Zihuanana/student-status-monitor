# -*- coding: utf-8 -*-
"""考勤源覆盖（``Course.covered``）回归测试。

为什么单独一个模块
-----------------
教务选课名单录进来之后，"课表里有课 + 没有签到 = 缺勤"这条规则会立刻变成
**大规模误报**：名单 118 门课里只有 19 门能被翰博尔看到（它是录播系统，
只覆盖装了设备的教室）。剩下 99 门课的学生会被判成「整日无签到」，
连续三天就是「严重异常」——上千条假告警会把真问题淹掉。

所以本模块锁死三件事：

1. 没有考勤源覆盖的课**照常算"有课"**（页面要显示），但**不算缺勤**；
2. 一天里如果**只有**看不到的课，绝不能判成「整日无签到」；
3. 缺勤反推（infer）要整门跳过没有覆盖的课 —— 否则"应到 − 已观测"
   恒等于"应到"，推出来就是全员缺勤。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

from tests import dbguard  # noqa: E402

os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceDaily,
    AttendanceEvent,
    Course,
    Student,
    StudentCourse,
)
from app.services.attendance import SessionFact, summarize_day  # noqa: E402

NOW = datetime(2026, 10, 10, 20, 0)
TODAY = NOW.date()


def fact(
    name: str,
    *,
    covered: bool = True,
    start: datetime | None = None,
    signed: bool = False,
    leave: bool = False,
) -> SessionFact:
    start = start or datetime.combine(TODAY, datetime.min.time().replace(hour=8))
    return SessionFact(
        schedule_guid=name,
        class_date=TODAY,
        start_time=start,
        end_time=start + timedelta(hours=2),
        present=signed,
        leave=leave,
        course_name=name,
        covered=covered,
    )


class TestSummarizeDayWithNoSource(unittest.TestCase):
    """状态机那一侧：看不到的课不倒扣。"""

    def test_day_with_only_uncovered_classes_is_not_all_missed(self) -> None:
        """一整天只有看不到的课 → 我们没有任何考勤数据，不能指控缺勤。"""
        summary = summarize_day(TODAY, [fact("体育", covered=False)], NOW)
        self.assertEqual(summary.session_count, 1)
        self.assertEqual(summary.no_source_count, 1)
        self.assertFalse(summary.all_missed)

    def test_two_uncovered_classes_also_safe(self) -> None:
        summary = summarize_day(
            TODAY, [fact("思政", covered=False), fact("体育", covered=False)], NOW
        )
        self.assertEqual(summary.no_source_count, 2)
        self.assertEqual(summary.missed_count, 0)
        self.assertFalse(summary.all_missed)

    def test_day_with_only_uncovered_and_one_covered_class(self) -> None:
        """混合日：只有那节看得到的课参与判定。"""
        summary = summarize_day(
            TODAY, [fact("专业课"), fact("思政", covered=False)], NOW
        )
        self.assertEqual(summary.session_count, 2)
        self.assertEqual(summary.no_source_count, 1)
        self.assertTrue(summary.all_missed, "看得到的那节课没人签到，仍应判缺勤")

    def test_uncovered_class_does_not_mask_a_real_absence(self) -> None:
        """回归锁定：加了保险之后，真正的缺勤不能被放过。"""
        summary = summarize_day(TODAY, [fact("专业课", signed=False)], NOW)
        self.assertTrue(summary.all_missed)
        self.assertEqual(summary.no_source_count, 0)

    def test_uncovered_class_with_a_sign_still_counts_as_present(self) -> None:
        """看得到的课有签到时，正常算活跃（保险不能把签到吃掉）。"""
        summary = summarize_day(
            TODAY,
            [fact("专业课", signed=True), fact("思政", covered=False)],
            NOW,
        )
        self.assertTrue(summary.has_signed)
        self.assertFalse(summary.all_missed)

    def test_uncovered_class_without_end_time_is_not_pending(self) -> None:
        """看不到的课没有结束时间时，不能让这一天永远"没结束"。"""
        s = SessionFact(
            schedule_guid="x", class_date=TODAY, start_time=None, end_time=None,
            course_name="思政", covered=False,
        )
        summary = summarize_day(TODAY, [s], NOW)
        self.assertEqual(summary.pending_count, 0)
        self.assertEqual(summary.no_source_count, 1)
        self.assertFalse(summary.all_missed)


class TestDayEvaluationPath(unittest.TestCase):
    """SQL 聚合那条路（总览页与告警判定）也要一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceDaily, AttendanceEvent, StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _seed(self) -> None:
        """两门课：一门有考勤源，一门没有；学生各有一条事件。"""
        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.add_all(
            [
                Course(schedule_guid="seen", course_name="专业课", covered=True,
                       class_date=TODAY, start_time=datetime.combine(TODAY, datetime.min.time().replace(hour=8)),
                       end_time=datetime.combine(TODAY, datetime.min.time().replace(hour=10))),
                Course(schedule_guid="unseen", course_name="思政", covered=False,
                       class_date=TODAY, start_time=None, end_time=None),
            ]
        )
        self.db.add_all(
            [
                AttendanceEvent(source="hanboer", schedule_guid="seen",
                                student_no="202400000001N", class_date=TODAY,
                                attend_status="缺勤"),
                AttendanceEvent(source="hanboer", schedule_guid="unseen",
                                student_no="202400000001N", class_date=TODAY,
                                attend_status="缺勤"),
            ]
        )
        self.db.commit()

    def test_uncovered_event_is_excluded_from_judgement(self) -> None:
        from app.services.ingest import _load_day_evaluations

        self._seed()
        got = _load_day_evaluations(
            self.db, {"202400000001N"}, TODAY, TODAY, NOW, include_unobserved=False
        )
        days = got["202400000001N"]
        self.assertEqual(len(days), 1)
        d = days[0]
        self.assertEqual(d.session_count, 2)
        self.assertEqual(d.no_source_count, 1)
        self.assertEqual(d.missed_count, 1, "只有看得到的那节课该算缺勤")

    def test_only_uncovered_events_never_trigger_all_missed(self) -> None:
        from app.services.ingest import _load_day_evaluations

        self.db.add(Student(student_no="202400000001N", name="乙", grade="2024"))
        self.db.add(
            AttendanceEvent(source="hanboer", schedule_guid="unseen",
                            student_no="202400000001N", class_date=TODAY,
                            attend_status="缺勤")
        )
        self.db.commit()
        got = _load_day_evaluations(
            self.db, {"202400000001N"}, TODAY, TODAY, NOW, include_unobserved=False
        )
        d = got["202400000001N"][0]
        self.assertEqual(d.session_count, 1)
        self.assertEqual(d.no_source_count, 1)
        self.assertFalse(d.all_missed, "全是看不到的课，不能判整日无签到")
        self.assertEqual(d.missed_count, 0)

    def test_unobserved_uncovered_course_is_not_an_absence(self) -> None:
        """名单建立的课（有应到关系、一条事件都没有）不能被算成缺勤。

        这是录名单之后最容易踩的那条路：``include_unobserved=True``
        会把"应到但无事件"补成缺勤，而名单里的课恰恰都是这种。
        """
        from app.services.ingest import _load_day_evaluations

        self._seed()
        self.db.add(StudentCourse(student_no="202400000001N", schedule_guid="unseen",
                                  class_date=TODAY, source="roster"))
        self.db.commit()

        got = _load_day_evaluations(
            self.db, {"202400000001N"}, TODAY, TODAY, NOW, include_unobserved=True
        )
        d = got["202400000001N"][0]
        self.assertEqual(d.no_source_count, 1, "名单里那门课要记进 no_source_count")
        self.assertEqual(d.missed_count, 1, "仍然只有专业那节课算缺勤")


class TestInferSkipsUncovered(unittest.TestCase):
    """缺勤反推必须整门跳过没有覆盖的课。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceDaily, AttendanceEvent, StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _seed(self, covered: bool) -> Course:
        start = datetime.combine(TODAY, datetime.min.time().replace(hour=8))
        course = Course(
            schedule_guid="c1",
            course_name="思政",
            class_name="2024智能1班",
            class_date=TODAY,
            start_time=start,
            end_time=start + timedelta(hours=2),
            covered=covered,
        )
        self.db.add(course)
        self.db.add_all(
            [
                Student(student_no="202400000001N", name="甲", grade="2024",
                        class_name="2024智能1班"),
                Student(student_no="202400000001N", name="乙", grade="2024",
                        class_name="2024智能1班"),
            ]
        )
        self.db.commit()
        return course

    def test_uncovered_course_is_skipped_entirely(self) -> None:
        from app.services.infer import infer_absentees

        course = self._seed(covered=False)
        events = infer_absentees(self.db, [course], now=NOW)
        self.assertEqual(events, [], "看不到的课不能推出缺勤")

    def test_covered_course_still_infers(self) -> None:
        """回归锁定：保险不能把真正的反推能力也关掉。

        注意这里只让**一个人**缺勤：两个人都缺勤是 2/2 = 100%，
        会先撞上 ``INFER_MAX_RATIO``（默认 0.8）那道保险丝 —— 那是另一条规则，
        与本模块无关，混在一起会分不清是谁拦下来的。
        """
        from app.services.infer import infer_absentees

        course = self._seed(covered=True)
        self.db.add(
            AttendanceEvent(source="hanboer", schedule_guid="c1",
                            student_no="202400000001N", class_date=TODAY,
                            attend_status="出勤")
        )
        self.db.commit()

        events = infer_absentees(self.db, [course], now=NOW)
        self.assertEqual(len(events), 1, "没签到的那个人应被推出来")
        self.assertEqual(events[0].student_no, "202400000001N")

    def test_pending_courses_excludes_uncovered(self) -> None:
        from app.services.infer import pending_inference_courses

        self._seed(covered=False)
        self.assertEqual(pending_inference_courses(self.db, now=NOW, hours=24), [])

        self.db.query(Course).update({Course.covered: True})
        self.db.commit()
        self.assertEqual(len(pending_inference_courses(self.db, now=NOW, hours=24)), 1)


class TestRosterNameIsAuthoritative(unittest.TestCase):
    """名册给的姓名不能被采集覆盖。

    2026-10-12 老师报「有些学号的人名不对，以 24 智科最为严重」。
    查下来：翰博尔返回的 ``userName`` 是**学生自己设的账号名**
    （实测有人叫「爸爸」「cxy」「Jackie」），而采集每轮都调
    ``upsert_students`` 把姓名写回去 —— 于是名册里的真名被一遍遍覆盖。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceDaily, AttendanceEvent, StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_attendance_cannot_overwrite_roster_name(self) -> None:
        from app.services.ingest import upsert_students

        upsert_students(self.db, [{"student_no": "202400000001N", "name": "凌子岳"}],
                        source_kind="roster")
        self.db.commit()
        stu = self.db.get(Student, "202400000001N")
        self.assertEqual(stu.name, "凌子岳")
        self.assertEqual(stu.name_source, "roster")

        # 采集侧带着账号昵称来 → 不能覆盖
        upsert_students(self.db, [{"student_no": "202400000001N", "name": "cxy",
                                   "grade": "2024"}])
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(self.db.get(Student, "202400000001N").name, "凌子岳",
                         "名册姓名不能被考勤账号名覆盖")

        # 名册侧再写 → 仍然可以更新（比示示例辅导员改了名）
        upsert_students(self.db, [{"student_no": "202400000001N", "name": "凌子岳2"}],
                        source_kind="roster")
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(self.db.get(Student, "202400000001N").name, "凌子岳2")

    def test_attendance_can_fill_empty_name(self) -> None:
        """库里没名字时，采集带回来的名字要能用上（否则页面只有学号）。"""
        from app.services.ingest import upsert_students

        upsert_students(self.db, [{"student_no": "202400000001N", "name": ""}],
                        source_kind="roster")
        self.db.commit()
        upsert_students(self.db, [{"student_no": "202400000001N", "name": "张同学"}])
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(self.db.get(Student, "202400000001N").name, "张同学")

    def test_attendance_can_update_attendance_sourced_name(self) -> None:
        """名字本来来自考勤（学生自己改对/改名）时，采集可以更新它。"""
        from app.services.ingest import upsert_students

        upsert_students(self.db, [{"student_no": "202400000001N", "name": "旧账号名"}])
        self.db.commit()
        upsert_students(self.db, [{"student_no": "202400000001N", "name": "新账号名"}])
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(self.db.get(Student, "202400000001N").name, "新账号名")

    def test_repair_names_restores_and_marks(self) -> None:
        """``repair_names`` 按名单刷回真名，并标记为名册来源。"""
        import tempfile
        from pathlib import Path

        from app.services.ingest import upsert_students
        from app.services.roster import repair_names

        upsert_students(self.db, [{"student_no": "202400000001N", "name": "爸爸"}])
        self.db.commit()

        p = Path(tempfile.mkdtemp(prefix="sm-name-")) / "名单.csv"
        p.write_bytes("学号,姓名\n202400000001N,郝思博\n".encode("gb18030"))
        result = repair_names(self.db, p)
        self.assertEqual(result["fixed"], 1)
        self.db.expire_all()
        stu = self.db.get(Student, "202400000001N")
        self.assertEqual(stu.name, "郝思博")
        self.assertEqual(stu.name_source, "roster")

        # 修完之后采集再来也覆盖不了
        upsert_students(self.db, [{"student_no": "202400000001N", "name": "爸爸"}])
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(self.db.get(Student, "202400000001N").name, "郝思博")


class TestCollectorSkipsUncovered(unittest.TestCase):
    """采集器**不能**去抓没有考勤源的课。

    2026-10-12 的真实事故：教务课表的 426 个时段行也是 ``Course``，
    而采集器按"库里今天的课"逐个调翰博尔的 ``getAttendanceDetails``。
    这些课在翰博尔里根本不存在 → 每门课 3 次重试、每次几十秒超时，
    一整轮几百次失败请求把事件循环占满，**网页 504 挂了两天**。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceDaily, AttendanceEvent, StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _seed(self) -> None:
        start = datetime.combine(TODAY, datetime.min.time().replace(hour=8))
        self.db.add_all(
            [
                # 有考勤源：翰博尔的课
                Course(schedule_guid="hb-1", course_name="专业课", covered=True,
                       class_date=TODAY, start_time=start,
                       end_time=start + timedelta(hours=2)),
                # 无考勤源：教务课表建的时段行（翰博尔里不存在）
                Course(schedule_guid="tch-1-334", course_name="计算机网络B",
                       covered=False, class_date=TODAY, start_time=start,
                       end_time=start + timedelta(hours=2)),
                # 已结束、未同步过的（回补候选）
                Course(schedule_guid="tch-2-334", course_name="思政",
                       covered=False, class_date=TODAY - timedelta(days=1),
                       start_time=start - timedelta(days=1),
                       end_time=start - timedelta(days=1) + timedelta(hours=2)),
            ]
        )
        self.db.commit()

    def test_unresolved_courses_excludes_uncovered(self) -> None:
        """回补候选不能包含没有考勤源的课，否则每轮都重试一遍。"""
        from app.services.collect import unresolved_courses

        self._seed()
        got = unresolved_courses(
            self.db, now=datetime.combine(TODAY, datetime.min.time().replace(hour=23))
        )
        self.assertEqual(got, [], "教务课表的行不该出现在回补候选里")

    def test_detail_query_filters_by_covered(self) -> None:
        """``_fetch_details`` 的筛选条件必须含 ``covered``。

        这里直接按同样的条件查一遍，锁住这条 where —— 早期版本没有它，
        426 个时段行每轮都会被拿去调翰博尔。
        """
        from sqlalchemy import select

        self._seed()
        stmt = (
            select(Course)
            .where(
                Course.start_time.is_not(None),
                Course.class_date == TODAY,
                Course.covered.is_(True),
            )
            .order_by(Course.start_time)
        )
        picked = list(self.db.execute(stmt).scalars())
        self.assertEqual([c.schedule_guid for c in picked], ["hb-1"])

        # 反证：不加 covered 就会捞到两门（其中一门在翰博尔不存在）
        loose = list(
            self.db.execute(
                select(Course).where(
                    Course.start_time.is_not(None),
                    Course.class_date == TODAY,
                )
            ).scalars()
        )
        self.assertEqual(len(loose), 2, "确认库里确实有两门，上面那条过滤才有效")


class TestSessionFactsCarryCoverage(unittest.TestCase):
    """逐节课那条路（周课表）也要把标记带上，否则前端没得置灰。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (AttendanceDaily, AttendanceEvent, StudentCourse, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_facts_from_student_course_are_marked_uncovered(self) -> None:
        from app.services.ingest import _load_session_facts, student_week

        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.add(Course(schedule_guid="unseen", course_name="思政", covered=False,
                           class_date=TODAY))
        self.db.add(StudentCourse(student_no="202400000001N", schedule_guid="unseen",
                                  class_date=TODAY, source="roster"))
        self.db.commit()

        facts = _load_session_facts(self.db, {"202400000001N"}, TODAY - timedelta(days=7),
                                    TODAY + timedelta(days=7))
        self.assertEqual(len(facts["202400000001N"]), 1)
        self.assertFalse(facts["202400000001N"][0].covered)

        week = student_week(self.db, "202400000001N", anchor=TODAY, now=NOW)
        sessions = [s for d in week["days"] for s in d["sessions"]]
        self.assertEqual(len(sessions), 1)
        self.assertIs(sessions[0]["covered"], False)

    def test_facts_from_events_default_to_covered(self) -> None:
        """有真实签到事件 → 一定有覆盖，不能被误标成看不到。"""
        from app.services.ingest import _load_session_facts

        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.add(Course(schedule_guid="seen", course_name="专业课", covered=True,
                           class_date=TODAY))
        self.db.add(AttendanceEvent(source="hanboer", schedule_guid="seen",
                                    student_no="202400000001N", class_date=TODAY,
                                    attend_status="出勤"))
        self.db.commit()

        facts = _load_session_facts(self.db, {"202400000001N"}, TODAY - timedelta(days=7),
                                    TODAY + timedelta(days=7))
        self.assertTrue(facts["202400000001N"][0].covered)

    def test_uncovered_course_with_class_date_shows_in_week(self) -> None:
        """回归锁定：教务课表给了课次日期的课，必须出现在周课表里。

        这里踩过两次坑，都是**静默**的：
        1. 无考勤源的课如果只按"应到关系"补，会被判成"从没观测到"而不显示；
        2. 课次日期在 ``Course.class_date`` 上，而 ``StudentCourse.class_date``
           是空的 —— 按后者过滤时，这类课一节都出不来，页面看着像没排课。
        """
        from app.services.ingest import _load_session_facts, student_week

        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        start = datetime.combine(TODAY, datetime.min.time().replace(hour=10, minute=10))
        self.db.add(
            Course(schedule_guid="plain", course_name="计算机网络B", covered=False,
                   class_date=TODAY, start_time=start,
                   end_time=start + timedelta(minutes=110), node_name="3-4",
                   classroom_name="C402")
        )
        # 关键：选课关系上的 class_date 留空（选课名单导入就是这样）
        self.db.add(StudentCourse(student_no="202400000001N", schedule_guid="plain",
                                  class_date=None, source="roster"))
        self.db.commit()

        facts = _load_session_facts(self.db, {"202400000001N"},
                                    TODAY - timedelta(days=7), TODAY + timedelta(days=7))
        got = facts["202400000001N"]
        self.assertEqual(len(got), 1, "有课次日期的无考勤源课必须出现")
        self.assertFalse(got[0].covered)
        self.assertEqual(got[0].class_date, TODAY)

        week = student_week(self.db, "202400000001N", anchor=TODAY, now=NOW)
        sessions = [s for d in week["days"] for s in d["sessions"]]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["course_name"], "计算机网络B")
        self.assertIs(sessions[0]["covered"], False)
        # 但它**不能**把这一天变成"整日无签到"
        day = next(d for d in week["days"] if d["date"] == TODAY.isoformat())
        self.assertEqual(day["no_source_count"], 1)

    def test_uncovered_course_without_date_is_not_shown(self) -> None:
        """没有课次日期的课不进周课表 —— 否则一周里会凭空冒出一堆课。"""
        from app.services.ingest import _load_session_facts

        self.db.add(Student(student_no="202400000001N", name="甲", grade="2024"))
        self.db.add(Course(schedule_guid="nodate", course_name="思政", covered=False,
                           class_date=None))
        self.db.add(StudentCourse(student_no="202400000001N", schedule_guid="nodate",
                                  class_date=None, source="roster"))
        self.db.commit()

        facts = _load_session_facts(self.db, {"202400000001N"},
                                    TODAY - timedelta(days=7), TODAY + timedelta(days=7))
        self.assertEqual(facts.get("202400000001N", []), [])


if __name__ == "__main__":
    unittest.main()
