# -*- coding: utf-8 -*-
"""老师手动标记（解除预警）测试。

规则示示例辅导员定的原话，改过一版：

> 第一版："如果标记后三有效天内（不含标记日，有效天指有课的天数）仍无签到
>          则继续判定严重异常，异常同理"
> **现行版**："标记后，视作一次没有课程的签到。也就是说，如果标记完第二天
>          依然没有签到，那么就继续进行异常标记。"

也就是：标记 = 学生刚签到过一次（只不过那天没课），
它把标记日及以前的连续无签到**清零**，但**不赠送任何豁免**。

三条边界各自都有陷阱，所以逐条锁死：

1. **标记日及以前被清零** —— 老师已经处理过那段了，不该继续算连续天数；
2. **第二天起照常判定** —— 标记完第二天没签到就报异常，
   这正是"视作一次签到"而不是"豁免 N 天"的关键差别；
3. **标记信息要一直带着** —— 标记后又异常时，页面得同时显示
   「异常」和「已示示例辅导员标记过」，否示示例辅导员会以为自己没标上。
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("manual_mark")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.ids import normalize_student_no  # noqa: E402
from app.core.enums import StudentState  # noqa: E402
from app.services.attendance import (  # noqa: E402
    DayEvaluation,
    ManualMark,
    evaluate_from_days,
    split_at_mark,
)

#: 一周的有课日：周一 ~ 周五（2026-03-09 是周一）
MON, TUE, WED, THU, FRI = (date(2026, 3, 9) + timedelta(days=i) for i in range(5))


def day(d: date, *, present: bool = False, complete: bool = True) -> DayEvaluation:
    return DayEvaluation(
        class_date=d,
        session_count=1,
        present_count=1 if present else 0,
        missed_count=0 if present else 1,
        pending_count=0 if complete else 1,
        day_complete=complete,
        all_missed=(complete and not present),
    )


class TestSplitAtMark(unittest.TestCase):
    """「标记日及以前」的裁剪。"""

    def test_drops_marking_day_and_earlier(self) -> None:
        days = {MON: day(MON), TUE: day(TUE), WED: day(WED)}
        kept = split_at_mark(days, ManualMark(marked_on=TUE))
        self.assertEqual(sorted(kept), [WED])

    def test_no_mark_keeps_everything(self) -> None:
        days = {MON: day(MON), TUE: day(TUE)}
        self.assertEqual(sorted(split_at_mark(days, None)), [MON, TUE])

    def test_mark_on_non_class_day_still_drops_earlier(self) -> None:
        """标记日可以是没课的日子（比如周末），规则一样。"""
        sunday = date(2026, 3, 8)
        days = {MON: day(MON), TUE: day(TUE)}
        self.assertEqual(sorted(split_at_mark(days, ManualMark(marked_on=sunday))),
                         [MON, TUE])


class TestMarkVerdict(unittest.TestCase):
    """标记对最终判定的影响。"""

    def test_mark_clears_streak_and_shows_active(self) -> None:
        """标记当天：连续无签到被清零 → 活跃。"""
        days = [day(MON), day(TUE)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 10, 20, 0), mark=ManualMark(TUE))
        self.assertIs(v.state, StudentState.ACTIVE)
        self.assertTrue(v.manual_marked)
        self.assertEqual(v.manual_marked_on, TUE)

    def test_next_class_day_without_signin_is_abnormal_again(self) -> None:
        """**核心用例**：标记完第二天依然没签到 → 继续报异常。"""
        days = [day(MON), day(TUE), day(WED)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 11, 20, 0), mark=ManualMark(TUE))
        self.assertIs(v.state, StudentState.ABNORMAL)
        self.assertTrue(v.manual_marked, "标记信息仍要保留，页面要能同时显示两者")

    def test_third_day_without_signin_is_severe(self) -> None:
        """标记后再连续三个有课日无签到 → 严重异常。"""
        days = [day(WED), day(THU), day(FRI)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 13, 20, 0), mark=ManualMark(TUE))
        self.assertIs(v.state, StudentState.SEVERE)
        self.assertTrue(v.manual_marked)

    def test_signin_after_mark_is_active(self) -> None:
        days = [day(MON), day(TUE), day(WED, present=True)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 11, 20, 0), mark=ManualMark(TUE))
        self.assertIs(v.state, StudentState.ACTIVE)
        self.assertTrue(v.manual_marked)

    def test_mark_gives_no_grace(self) -> None:
        """和上一版的差别：标记后**没有** 3 天豁免，第二天就报。"""
        days = [day(MON), day(TUE), day(WED)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 11, 20, 0), mark=ManualMark(TUE))
        self.assertIsNot(v.state, StudentState.ACTIVE)
        self.assertEqual(v.consecutive_missed_days, 1)

    def test_mark_without_any_class_after_shows_active(self) -> None:
        """暂时没课的学生被标记后显示活跃（视作刚签到）。"""
        v = evaluate_from_days([], now=datetime(2026, 3, 11, 10, 0), mark=ManualMark(MON))
        self.assertIs(v.state, StudentState.ACTIVE)
        self.assertTrue(v.manual_marked)

    def test_mark_only_forgives_up_to_mark_day(self) -> None:
        """标记只清零到标记日；标记日之前的天数不该再被算进连续天数。"""
        days = [day(MON), day(TUE), day(WED), day(THU)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 12, 20, 0), mark=ManualMark(WED))
        self.assertIs(v.state, StudentState.ABNORMAL)
        self.assertEqual(v.consecutive_missed_days, 1, "只应算周四这一天")

    def test_no_mark_means_normal_rules(self) -> None:
        days = [day(TUE), day(WED), day(THU)]
        v = evaluate_from_days(days, now=datetime(2026, 3, 12, 20, 0))
        self.assertIs(v.state, StudentState.SEVERE)
        self.assertFalse(v.manual_marked)


class TestStudentNoNormalization(unittest.TestCase):
    """学号去掉平台账号前缀 cuc。"""

    def test_strips_prefix(self) -> None:
        self.assertEqual(normalize_student_no("202400000001N"), "202400000001N")
        self.assertEqual(normalize_student_no("202400000001N"), "202400000001N")

    def test_case_insensitive_and_trimmed(self) -> None:
        for raw in ("202400000001N", "  202400000001N ", "cuc 202400000001N"):
            self.assertEqual(normalize_student_no(raw), "202400000001N".replace(" ", ""))

    def test_only_strips_leading_prefix_once(self) -> None:
        """锚定在开头且只剥一次 —— 学号中间出现的 cuc 不能被吃掉。"""
        self.assertEqual(normalize_student_no("cuc2023cuc001N"), "2023cuc001N")

    def test_empty_is_empty(self) -> None:
        self.assertEqual(normalize_student_no(""), "")
        self.assertEqual(normalize_student_no(None), "")

    def test_needs_normalizing(self) -> None:
        from app.core.ids import needs_normalizing

        self.assertTrue(needs_normalizing("202400000001N"))
        self.assertFalse(needs_normalizing("202400000001N"))
        self.assertFalse(needs_normalizing(""))


class TestRekeyMigration(unittest.TestCase):
    """把库里已带前缀的学号改掉（含 4 张子表与手动标记）。"""

    def test_rekey_moves_everything_and_keeps_marks(self) -> None:
        dbguard.ensure_schema()
        from datetime import date as _date

        from app.models import (
            Alert,
            AttendanceDaily,
            AttendanceEvent,
            Course,
            Student,
            StudentCourse,
        )
        from app.services.rekey import rekey_students

        db = SessionLocal()
        try:
            for t in (Alert, AttendanceDaily, AttendanceEvent, StudentCourse,
                      Course, Student):
                db.query(t).delete()
            # student_courses 对 courses 也有外键，先把课程建好
            db.add(Course(schedule_guid="g1", course_name="软件工程",
                          class_date=_date(2026, 3, 10)))
            db.add(
                Student(
                    student_no="202400000001N",
                    name="陈泓恺",
                    grade="2023",
                    major_code="013",
                    manual_state="活跃",
                    manual_marked_at=datetime(2026, 3, 10, 12, 0),
                    manual_reason="已核实",
                    manual_by="示示例辅导员",
                )
            )
            # 必须先落库：student_courses 有真正的外键约束，
            # 依赖 SQLAlchemy 的自动排序会撞 FK（本项目踩过）
            db.flush()
            db.add(AttendanceEvent(source="hanboer", schedule_guid="g1",
                                   student_no="202400000001N",
                                   class_date=_date(2026, 3, 10), attend_status="缺勤"))
            db.add(AttendanceDaily(student_no="202400000001N",
                                   class_date=_date(2026, 3, 10)))
            db.add(StudentCourse(student_no="202400000001N", schedule_guid="g1",
                                 class_date=_date(2026, 3, 10), source="hanboer"))
            db.add(Alert(student_no="202400000001N", kind="异常", state="异常",
                         first_date=_date(2026, 3, 10), last_date=_date(2026, 3, 10)))
            db.commit()

            result = rekey_students(db)
            self.assertEqual(result["renamed"], 1)
            self.assertEqual(result["skipped_exists"], 0)

            self.assertEqual(
                [s.student_no for s in db.query(Student).all()], ["202400000001N"]
            )
            self.assertEqual(db.query(Student).one().manual_reason, "已核实")
            self.assertEqual(db.query(AttendanceEvent).one().student_no, "202400000001N")
            self.assertEqual(db.query(AttendanceDaily).one().student_no, "202400000001N")
            self.assertEqual(db.query(StudentCourse).one().student_no, "202400000001N")
            self.assertEqual(db.query(Alert).one().student_no, "202400000001N")

            # 幂等
            self.assertEqual(rekey_students(db)["renamed"], 0)
        finally:
            db.close()

    def test_rekey_skips_when_target_exists(self) -> None:
        """目标学号已存在时不能硬改（主键冲突），要跳过并报出来。"""
        dbguard.ensure_schema()
        from app.models import Student
        from app.services.rekey import rekey_students

        db = SessionLocal()
        try:
            db.query(Student).delete()
            db.add_all([
                Student(student_no="202400000001N", name="旧"),
                Student(student_no="202400000001N", name="新"),
            ])
            db.commit()

            result = rekey_students(db)
            self.assertEqual(result["skipped_exists"], 1)
            self.assertEqual(result["renamed"], 0)
            self.assertEqual(db.query(Student).count(), 2)
        finally:
            db.close()


class TestMarkApi(unittest.TestCase):
    """接口层。

    为什么非要走 TestClient：标记功能一开始在 `/api/marks` 里
    `from app.services.ingest import manual_grace_left` —— 那个符号其实在
    `app.services.attendance`。纯函数测不到，`python -c "import app.main"`
    也测不到（ImportError 在函数体里），只有真发一次请求才会 500。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app)

    def setUp(self) -> None:
        from app.models import Student

        self.db = SessionLocal()
        self.db.query(Student).delete()
        # 故意用**带前缀**的学号：入库必须自动规范化
        self.db.add(
            Student(
                student_no="202400000001N",
                name="陈泓恺",
                grade="2023",
                major_code="013",
            )
        )
        self.db.commit()
        from app.core.security import issue_token

        self.client.cookies.set(settings.cookie_name, issue_token())

    def tearDown(self) -> None:
        self.db.close()

    def _student(self) -> dict:
        r = self.client.get("/api/students/202400000001N")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_mark_requires_session(self) -> None:
        self.client.cookies.clear()
        r = self.client.post(
            "/api/students/202400000001N/mark",
            json={"reason": "测试"},
        )
        self.assertEqual(r.status_code, 401)

    def test_mark_requires_reason(self) -> None:
        """原因必填 —— 这个操作要能被追问，空原因必须被拒。"""
        r = self.client.post("/api/students/202400000001N/mark", json={"reason": ""})
        self.assertIn(r.status_code, (400, 422))

    def test_mark_then_unmark_roundtrip(self) -> None:
        before = self._student()
        self.assertFalse(before["manual_marked"])

        r = self.client.post(
            "/api/students/202400000001N/mark",
            json={"state": "活跃", "reason": "已请假，辅导员确认", "by": "示示例辅导员"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        marked = r.json()
        self.assertTrue(marked["manual_marked"])
        self.assertEqual(marked["manual_reason"], "已请假，辅导员确认")
        self.assertEqual(marked["manual_by"], "示示例辅导员")
        # 标记时刻要精确到分钟（卡片上直接显示「手动标记：2026-09-20 12:37」）
        self.assertRegex(marked["manual_marked_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

        r = self.client.delete("/api/students/202400000001N/mark")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["manual_marked"])

    def test_marks_list_works(self) -> None:
        """/api/marks 必须可用（它曾经因导入错模块直接 500）。"""
        self.client.post(
            "/api/students/202400000001N/mark",
            json={"state": "活跃", "reason": "联调", "by": "开发"},
        )
        r = self.client.get("/api/marks")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertEqual(item["student_no"], "202400000001N")
        self.assertIn("marked_at", item)
        self.assertIn("state_now", item)

    def test_mark_unknown_student_is_404(self) -> None:
        r = self.client.post("/api/students/nobody/mark", json={"reason": "测试"})
        self.assertEqual(r.status_code, 404)

    def test_week_includes_mark_fields(self) -> None:
        self.client.post(
            "/api/students/202400000001N/mark",
            json={"state": "活跃", "reason": "联调", "by": "开发"},
        )
        r = self.client.get("/api/students/202400000001N/week")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["manual_marked"])
        self.assertEqual(body["manual_reason"], "联调")


class TestRepeatedMarking(unittest.TestCase):
    """重复标记。

    老师反馈："标记完了为什么没法再标记了" —— 后端本来就允许（再 POST 一次
    就是新的一次标记），是界面只给了"撤销"没给"重新标记"。这里把后端的
    语义与流水锁死，界面才有东西可靠。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app)

    def setUp(self) -> None:
        from app.models import Student, StudentMark

        self.db = SessionLocal()
        self.db.query(StudentMark).delete()
        self.db.query(Student).delete()
        self.db.add(Student(student_no="202400000001N", name="陈泓恺", grade="2023",
                            major_code="013"))
        self.db.commit()
        from app.core.security import issue_token

        self.client.cookies.set(settings.cookie_name, issue_token())

    def tearDown(self) -> None:
        self.db.close()

    def _mark(self, reason: str, by: str = "老师"):
        return self.client.post(
            "/api/students/202400000001N/mark",
            json={"state": "活跃", "reason": reason, "by": by},
        )

    def test_mark_twice_is_allowed(self) -> None:
        r1 = self._mark("第一次：已请假")
        self.assertEqual(r1.status_code, 200, r1.text)
        first_at = r1.json()["manual_marked_at"]

        r2 = self._mark("第二次：家长确认下周返校")
        self.assertEqual(r2.status_code, 200, r2.text)
        second = r2.json()
        self.assertTrue(second["manual_marked"])
        self.assertEqual(second["manual_reason"], "第二次：家长确认下周返校")
        # 时间被刷新（同一分钟内可能相同，所以只断言不早于第一次）
        self.assertGreaterEqual(second["manual_marked_at"], first_at)

    def test_history_keeps_every_mark(self) -> None:
        """重复标记会覆盖"当前标记"，所以流水必须把每一次都留下。"""
        self._mark("第一次", by="示示例辅导员")
        self._mark("第二次", by="示示例辅导员")
        r = self.client.get("/api/students/202400000001N/mark-history")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["mark_times"], 2)
        self.assertEqual(len(body["items"]), 2)
        # 新的在前
        self.assertEqual(body["items"][0]["reason"], "第二次")
        self.assertEqual(body["items"][0]["by"], "示示例辅导员")
        self.assertEqual(body["items"][1]["reason"], "第一次")
        self.assertEqual(body["items"][1]["by"], "示示例辅导员")

    def test_unmark_is_recorded_too(self) -> None:
        self._mark("标记一下")
        self.client.delete("/api/students/202400000001N/mark")
        body = self.client.get("/api/students/202400000001N/mark-history").json()
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["items"][0]["action"], "unmark")
        self.assertEqual(body["items"][1]["action"], "mark")

    def test_unmark_then_mark_again(self) -> None:
        self._mark("先标")
        self.client.delete("/api/students/202400000001N/mark")
        self.assertFalse(
            self.client.get("/api/students/202400000001N").json()["manual_marked"]
        )
        r = self._mark("再标")
        self.assertTrue(r.json()["manual_marked"])
        self.assertEqual(self.client.get(
            "/api/students/202400000001N/mark-history"
        ).json()["mark_times"], 2)

    def test_repeated_mark_resets_baseline(self) -> None:
        """重复标记的语义：**又视作一次签到** —— 以最新那次为界清零。

        这条是"能不能再标一次"的实质：第二次标记必须真的把界往前推，
        否则再标一次只是改了个备注，学生该报还是照报。
        """
        early = date(2026, 3, 16)
        late = date(2026, 3, 18)
        days = [day(date(2026, 3, 16) + timedelta(days=i)) for i in range(5)]
        now = datetime(2026, 3, 20, 20, 0)  # 3/16~3/20 全都没签到

        v1 = evaluate_from_days(days, now=now, mark=ManualMark(early))
        self.assertEqual(v1.consecutive_missed_days, 4, "3/17、3/18、3/19、3/20")

        v2 = evaluate_from_days(days, now=now, mark=ManualMark(late))
        self.assertEqual(v2.consecutive_missed_days, 2, "只剩 3/19、3/20")

        self.assertIs(v1.state, StudentState.SEVERE)
        self.assertIs(v2.state, StudentState.ABNORMAL)


class TestMajorAliases(unittest.TestCase):
    """专业代码 → 专业名（013/023/033 → 智能科学与技术/视觉传达设计/数字媒体技术）。"""

    def setUp(self) -> None:
        self._saved = settings.major_aliases

    def tearDown(self) -> None:
        settings.major_aliases = self._saved

    def test_parses_configured_mapping(self) -> None:
        m = settings.major_alias_map
        self.assertEqual(m["013"], "智能科学与技术")
        self.assertEqual(m["023"], "视觉传达设计")
        self.assertEqual(m["033"], "数字媒体技术")

    def test_tolerates_chinese_punctuation_and_spaces(self) -> None:
        settings.major_aliases = "013 = 智能科学与技术，023=视觉传达设计、033=数字媒体技术"
        m = settings.major_alias_map
        self.assertEqual(len(m), 3)
        self.assertEqual(m["013"], "智能科学与技术")

    def test_empty_setting_is_empty_map(self) -> None:
        settings.major_aliases = ""
        self.assertEqual(settings.major_alias_map, {})

    def test_display_major_prefers_roster_then_alias(self) -> None:
        from app.services.ingest import display_major

        alias = {"013": "智能科学与技术"}
        # 名册里的专业名优先（信息化处给的名册可能更权威）
        self.assertEqual(display_major("人工智能", "013", alias), "人工智能")
        # 其次查别名表
        self.assertEqual(display_major("", "013", alias), "智能科学与技术")
        # 再退回代码
        self.assertEqual(display_major("", "999", alias), "专业999")
        # 最后兜底
        self.assertEqual(display_major("", "", alias), "未知专业")

    def test_sync_does_not_clobber_existing_names(self) -> None:
        """库里已有的名称优先 —— 名册导入的专业名不能被配置覆盖掉。"""
        dbguard.ensure_schema()
        from app.models import MajorAlias
        from app.services.ingest import sync_major_aliases

        db = SessionLocal()
        try:
            db.query(MajorAlias).delete()
            db.add(MajorAlias(code="013", name="名册里的名字"))
            db.commit()

            sync_major_aliases(db)
            rows = {r.code: r.name for r in db.query(MajorAlias).all()}
            self.assertEqual(rows["013"], "名册里的名字")
            self.assertEqual(rows["023"], "视觉传达设计")
            self.assertEqual(rows["033"], "数字媒体技术")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
