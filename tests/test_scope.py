# -*- coding: utf-8 -*-
"""校区作用域过滤测试。

背景
----
翰博尔平台是**多校区共用**的，接口本身没有校区过滤参数（实测 orgName /
className / buildingName / orgId / schoolName 传了全被忽略）。所以整所学校的课
会被一次性返回：

    实测 cuc.smartclass.cn 近 31 天 742 门课
      海南校区  152 门（楼栋 C座100 / A座33 / B座19）
      其它校区  590 门（楼栋 48号教学楼564 / 1号教学楼26）
    两拨学生学号零重叠

如果不在采集端切掉，库里就会混进 7000+ 不是本院的学生 —— 这不是"多一点脏数据"，
而是**页面直接不可用**：老师看到的是全平台 7000 人，真正的 749 人淹没在里面。

本模块锁定：切分靠的是节次名「海南第一二节」这个天然带校区的字段，
且楼栋与节次名两个信号必须一致（互为交叉验证）。
"""

from __future__ import annotations

import os
import unittest
from datetime import date

from tests import dbguard  # noqa: E402

_DB_PATH = dbguard.isolate("scope")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.adapters.base import NormalizedCourse  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Alert,
    AttendanceDaily,
    AttendanceEvent,
    Course,
    Student,
    StudentCourse,
    StudentMark,
)
from app.services import scope  # noqa: E402


def _day() -> date:
    """测试统一用一个固定日期，避免"今天/昨天"跨天导致断言漂移。"""
    return date(2026, 9, 15)


def course(
    guid: str,
    node_name: str = "第一二节",
    building_name: str = "48号教学楼",
    course_name: str = "高等数学",
    org_name: str = "",
    class_name: str = "智能241班",
) -> NormalizedCourse:
    return NormalizedCourse(
        schedule_guid=guid,
        source="hanboer",
        course_name=course_name,
        building_name=building_name,
        node_name=node_name,
        org_name=org_name,
        class_name=class_name,
    )


class TestKeywordMatching(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = settings.hb_campus_keywords

    def setUp(self) -> None:
        settings.hb_campus_keywords = self._saved

    def tearDown(self) -> None:
        settings.hb_campus_keywords = self._saved

    def test_default_keyword_is_hainan(self) -> None:
        settings.hb_campus_keywords = "海南"
        self.assertEqual(scope.scope_keywords(), ("海南",))

    def test_hainan_node_is_in_scope(self) -> None:
        settings.hb_campus_keywords = "海南"
        self.assertTrue(
            scope.course_in_scope(course("a", node_name="海南第五六节", building_name="C座"))
        )

    def test_main_campus_node_is_out_of_scope(self) -> None:
        settings.hb_campus_keywords = "海南"
        self.assertFalse(
            scope.course_in_scope(course("b", node_name="第五六节", building_name="48号教学楼"))
        )

    def test_empty_keywords_means_no_filtering(self) -> None:
        """留空 = 不过滤。排障时要能一键关掉，否则没法对比。"""
        settings.hb_campus_keywords = ""
        self.assertTrue(scope.course_in_scope(course("c", node_name="第五六节")))
        self.assertEqual(scope.scope_keywords(), ())

    def test_multiple_keywords_and_chinese_punctuation(self) -> None:
        """配置里混用中英文逗号、顿号都要能解析。"""
        settings.hb_campus_keywords = "海南，国际学院、Hainan"
        self.assertEqual(scope.scope_keywords(), ("海南", "国际学院", "Hainan"))
        self.assertTrue(scope.course_in_scope(course("d", node_name="第一节", org_name="国际学院")))
        self.assertTrue(scope.course_in_scope(course("e", node_name="第一节", course_name="hainan math")))

    def test_building_is_a_second_signal(self) -> None:
        """楼栋也能命中 —— 海南课全在 A/B/C 座，本部全在 48号/1号教学楼。"""
        settings.hb_campus_keywords = "C座"
        self.assertTrue(scope.course_in_scope(course("f", node_name="第五六节", building_name="C座")))
        self.assertFalse(
            scope.course_in_scope(course("g", node_name="第五六节", building_name="48号教学楼"))
        )

    def test_split_by_scope_keeps_order_and_partitions(self) -> None:
        settings.hb_campus_keywords = "海南"
        items = [
            course("1", node_name="海南第一二节", building_name="A座"),
            course("2", node_name="第一二节"),
            course("3", node_name="海南第三四节", building_name="B座"),
            course("4", node_name="第七八节"),
        ]
        kept, dropped = scope.split_by_scope(items)
        self.assertEqual([c.schedule_guid for c in kept], ["1", "3"])
        self.assertEqual([c.schedule_guid for c in dropped], ["2", "4"])

    def test_describe_scope_reads_naturally(self) -> None:
        settings.hb_campus_keywords = "海南"
        self.assertEqual(scope.describe_scope(), "海南")
        settings.hb_campus_keywords = ""
        self.assertIn("未设置", scope.describe_scope())


class TestPurgeOutOfScope(unittest.TestCase):
    """清库路径：作用域没配就采过一轮，需要能收拾现场。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved = settings.hb_campus_keywords

    def setUp(self) -> None:
        settings.hb_campus_keywords = "海南"
        self.db = SessionLocal()
        for table in (StudentCourse, AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()
        self._seed()

    def tearDown(self) -> None:
        self.db.close()
        settings.hb_campus_keywords = self._saved

    def _seed(self) -> None:
        from datetime import date

        self.db.add_all(
            [
                Course(schedule_guid="hn1", node_name="海南第一二节", building_name="A座",
                       course_name="专业英语（AI）"),
                Course(schedule_guid="bj1", node_name="第一二节", building_name="48号教学楼",
                       course_name="高等数学"),
                Course(schedule_guid="bj2", node_name="第五六节", building_name="1号教学楼",
                       course_name="新闻学概论"),
            ]
        )
        self.db.add_all(
            [
                Student(student_no="cuc2023hn001", name="海南同学"),
                Student(student_no="cuc2023bj001", name="本部同学"),
            ]
        )
        self.db.add_all(
            [
                StudentCourse(student_no="cuc2023hn001", schedule_guid="hn1",
                              class_date=date(2026, 9, 18), source="hanboer"),
                StudentCourse(student_no="cuc2023bj001", schedule_guid="bj1",
                              class_date=date(2026, 9, 18), source="hanboer"),
            ]
        )
        self.db.add_all(
            [
                AttendanceEvent(source="hanboer", schedule_guid="hn1",
                                student_no="cuc2023hn001", class_date=date(2026, 9, 18),
                                attend_status="出勤"),
                AttendanceEvent(source="hanboer", schedule_guid="bj1",
                                student_no="cuc2023bj001", class_date=date(2026, 9, 18),
                                attend_status="缺勤"),
                AttendanceEvent(source="hanboer", schedule_guid="bj2",
                                student_no="cuc2023bj001", class_date=date(2026, 9, 18),
                                attend_status="缺勤"),
            ]
        )
        self.db.commit()

    def test_out_of_scope_guids_only_flags_other_campus(self) -> None:
        guids, total = scope.out_of_scope_guids(self.db)
        self.assertEqual(total, 3)
        self.assertEqual(sorted(guids), ["bj1", "bj2"])

    def test_purge_removes_courses_events_and_links(self) -> None:
        result = scope.purge_out_of_scope(self.db, drop_students=True)
        self.assertEqual(result["courses_dropped"], 2)
        self.assertEqual(result["events_deleted"], 2)
        self.assertEqual(result["links_deleted"], 1)
        self.assertEqual(result["courses_deleted"], 2)
        self.assertEqual(result["students_deleted"], 1)

        remaining = [c.schedule_guid for c in self.db.query(Course).all()]
        self.assertEqual(remaining, ["hn1"])
        self.assertEqual(self.db.query(AttendanceEvent).count(), 1)
        self.assertEqual(
            [s.student_no for s in self.db.query(Student).all()], ["cuc2023hn001"]
        )

    def test_purge_keeps_students_by_default(self) -> None:
        """默认不删学生行 —— 误删会连同人工补齐的姓名/班级一起丢掉。"""
        result = scope.purge_out_of_scope(self.db)
        self.assertEqual(result["students_deleted"], 0)
        self.assertEqual(self.db.query(Student).count(), 2)

    def test_purge_is_noop_when_keywords_empty(self) -> None:
        """没配关键词时绝不能删任何东西 —— 这是最危险的操作。"""
        settings.hb_campus_keywords = ""
        result = scope.purge_out_of_scope(self.db, drop_students=True)
        self.assertEqual(result["courses_dropped"], 0)
        self.assertEqual(result["events_deleted"], 0)
        self.assertEqual(self.db.query(Course).count(), 3)
        self.assertEqual(self.db.query(Student).count(), 2)

    def test_purge_twice_is_safe(self) -> None:
        scope.purge_out_of_scope(self.db)
        again = scope.purge_out_of_scope(self.db)
        self.assertEqual(again["courses_dropped"], 0)
        self.assertEqual(again["events_deleted"], 0)


class TestStudentIdentifierInference(unittest.TestCase):
    """学号切片：742 门课的本部/海南之分成功后，紧接着踩的第二个坑。

    翰博尔 ``userNo`` 形如 ``202400000001N`` —— 前后都带字母。
    旧的 ``infer_major_code_from_student_no`` 要求整个串是纯数字，
    于是本院 749 人**全部**被判成"未知专业"，页面上挤成一坨。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = settings.student_id_major_slice

    def setUp(self) -> None:
        settings.student_id_major_slice = "6:9"

    def tearDown(self) -> None:
        settings.student_id_major_slice = self._saved

    def test_grade_found_behind_cuc_prefix(self) -> None:
        from app.services.ingest import infer_grade_from_student_no

        self.assertEqual(infer_grade_from_student_no("202400000001N"), "2023")
        self.assertEqual(infer_grade_from_student_no("202400000001"), "2024")

    def test_major_code_extracted_from_wrapped_id(self) -> None:
        from app.services.ingest import infer_major_code_from_student_no

        # 202400000001N → 学号本体 202400000001 → 专业段 013
        self.assertEqual(infer_major_code_from_student_no("202400000001N"), "013")
        self.assertEqual(infer_major_code_from_student_no("202400000001N"), "023")
        self.assertEqual(infer_major_code_from_student_no("202400000001N"), "033")
        # 纯数字学号走快路径，结果一致
        self.assertEqual(infer_major_code_from_student_no("202400000001"), "013")

    def test_no_digits_returns_empty(self) -> None:
        """没有任何数字时返回空，不要瞎猜。"""
        from app.services.ingest import infer_major_code_from_student_no

        self.assertEqual(infer_major_code_from_student_no("cucN"), "")
        self.assertEqual(infer_major_code_from_student_no(""), "")

    def test_college_slice_would_collapse_everyone(self) -> None:
        """回归锁定：4:6 取到的是学院码（本院全是 29），不是专业。"""
        from app.services.ingest import infer_major_code_from_student_no

        settings.student_id_major_slice = "4:6"
        self.assertEqual(infer_major_code_from_student_no("202400000001N"), "29")

    def test_recompute_updates_existing_rows(self) -> None:
        """改切片配置后，库里已有的学生行必须能被刷新。"""
        dbguard.ensure_schema()
        from app.services.ingest import recompute_student_identifiers

        db = SessionLocal()
        try:
            db.query(Student).delete()
            db.add(Student(student_no="202400000001N", name="陈泓恺",
                           grade="2023", major_code=""))
            db.commit()

            touched = recompute_student_identifiers(db)
            self.assertEqual(touched, 1)
            stu = db.query(Student).filter_by(student_no="202400000001N").one()
            self.assertEqual(stu.grade, "2023")
            self.assertEqual(stu.major_code, "013")
            self.assertIn("013", stu.group_key)

            # 再跑一次应当无事发生（幂等）
            self.assertEqual(recompute_student_identifiers(db), 0)
        finally:
            db.close()


class TestStudentIdScope(unittest.TestCase):
    """学号作用域：只收海南国际学院（学号 202x290x3xxx…）的学生。

    这是**第二道闸门**。第一道（``HB_CAMPUS_KEYWORDS``）切的是课程，靠的是
    上游给节次起名时带上了「海南」二字 —— 平台改个名就静默失效，而失效的
    后果是库里混进全校 5957 人（实测），页面直接不可读。

    学号形态是学校定的，不随平台改版变化，所以拿它做更硬的那道闸门。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved_enabled = settings.hb_student_id_scope_enabled
        cls._saved_pattern = settings.hb_student_id_pattern

    def setUp(self) -> None:
        settings.hb_student_id_scope_enabled = True
        settings.hb_student_id_pattern = r"(?i)^(cuc)?202\d290\d3\d{3}[0-9A-Z]?$"

    def tearDown(self) -> None:
        settings.hb_student_id_scope_enabled = self._saved_enabled
        settings.hb_student_id_pattern = self._saved_pattern

    def test_real_hainan_ids_pass(self) -> None:
        for raw in (
            "202400000001N",          # 库里存的形态（已剥前缀）
            "202400000001N",       # 接口原样返回的形态
            "202400000001N",
            "202400000001N",
            "202400000001N",       # 大小写不该影响判定
            # 尾字母可缺省：上线库实测有 96 名本院学生是这种号
            "202400000001",
            "202400000001",
            "2024000000013",          # 尾位也可以是数字
        ):
            self.assertTrue(scope.student_id_in_scope(raw), raw)

    def test_missing_tail_letter_is_still_our_college(self) -> None:
        """回归锁定：尾字母不能写成必需。

        上线库 5957 人里，学院码 29（本院）的共 1033 人，其中 96 人学号
        末尾没有字母。把正则写成 ``[0-9A-Z]$`` 会把这些人判成"别院"删掉 ——
        那不是过滤外院，而是删本院真人。
        """
        self.assertTrue(scope.student_id_in_scope("202400000001"))
        self.assertTrue(scope.student_id_in_scope("202400000001"))

    def test_college_code_is_what_separates_colleges(self) -> None:
        """真正区分学院的是第 5~6 位的学院码（本院 29），不是尾字母。"""
        for raw in (
            "202400000001084",   # 本部
            "2024000000011",        # 学院码 20
            "2024000000011",        # 学院码 11
            "2024000000011",        # 学院码 08
            "2024000000011",        # 学院码 02
        ):
            self.assertFalse(scope.student_id_in_scope(raw), raw)

    def test_other_campus_ids_are_rejected(self) -> None:
        """其它学院的学号必须挡住 —— 这才是本功能存在的理由。"""
        for raw in (
            "202400000001084",  # 本部形态（学院码不是 29）
            "202400000001084N",
            "2024010001",          # 测试占位学号
            "cuc2023hn001",        # 非数字学号
            "202400000001N",    # 学院码 19
            "202400000001084",  # 学院码 20
        ):
            self.assertFalse(scope.student_id_in_scope(raw), raw)

    def test_shape_is_anchored(self) -> None:
        """首尾必须锚定：多一个字符、少一位都不能过，否则等于没过滤。"""
        for raw in (
            "cuc2023290130",        # 少一位
            "20240000000178N",   # 多一位
            "x202400000001N",    # 前缀不对
            "202400000001N ",       # 尾随空格（strip 后应通过，见下）
            "2023290z3006N",        # 夹杂字母
            "",
            None,
        ):
            if raw == "202400000001N ":
                self.assertTrue(scope.student_id_in_scope(raw))
                continue
            self.assertFalse(scope.student_id_in_scope(raw), repr(raw))

    def test_switch_off_disables_filtering(self) -> None:
        """关掉开关就必须谁都不拦 —— 排障时要能一键关。"""
        settings.hb_student_id_scope_enabled = False
        self.assertIsNone(scope.student_id_like())
        self.assertTrue(scope.student_id_in_scope("随便什么学号"))
        self.assertIn("未启用", scope.describe_student_scope())

    def test_empty_pattern_disables_filtering(self) -> None:
        """正则留空 = 不过滤（与 HB_CAMPUS_KEYWORDS 留空的行为一致）。"""
        settings.hb_student_id_pattern = ""
        self.assertIsNone(scope.student_id_like())
        self.assertTrue(scope.student_id_in_scope("2024010001"))

    def test_filter_student_ids_partitions_events(self) -> None:
        """按 student_no 切分事件，顺序保持不变。"""
        from app.adapters.base import NormalizedEvent

        def ev(no: str) -> NormalizedEvent:
            return NormalizedEvent(
                source="hanboer", schedule_guid="g", student_no=no,
                class_date=_day(), attend_status="出勤",
            )

        kept, dropped = scope.filter_student_ids(
            [ev("202400000001N"), ev("202400000001084"), ev("202400000001N")]
        )
        self.assertEqual([e.student_no for e in kept], ["202400000001N", "202400000001N"])
        self.assertEqual([e.student_no for e in dropped], ["202400000001084"])

    def test_filter_student_ids_accepts_dicts(self) -> None:
        """名册导入那条路传的是 dict，同一个函数要能用。"""
        kept, dropped = scope.filter_student_ids(
            [
                {"student_no": "202400000001N", "name": "甲"},
                {"student_no": "2024010001", "name": "乙"},
            ]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "甲")
        self.assertEqual(dropped[0]["name"], "乙")

    def test_describe_mentions_pattern(self) -> None:
        self.assertIn("202", scope.describe_student_scope())


class TestPurgeOutOfScopeStudents(unittest.TestCase):
    """按学号清理存量：不满意的学生连同其考勤数据一起删。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved_enabled = settings.hb_student_id_scope_enabled
        cls._saved_pattern = settings.hb_student_id_pattern

    def setUp(self) -> None:
        settings.hb_student_id_scope_enabled = True
        settings.hb_student_id_pattern = r"(?i)^(cuc)?202\d290\d3\d{3}[0-9A-Z]?$"
        self.db = SessionLocal()
        for table in (
            StudentCourse, AttendanceEvent, AttendanceDaily, Alert, StudentMark,
            Course, Student,
        ):
            self.db.query(table).delete()
        self.db.commit()
        self._seed()

    def tearDown(self) -> None:
        self.db.close()
        settings.hb_student_id_scope_enabled = self._saved_enabled
        settings.hb_student_id_pattern = self._saved_pattern

    def _seed(self) -> None:
        from datetime import date

        from app.models import AttendanceDaily, Alert, StudentMark

        past = date(2026, 9, 15)
        self.db.add_all(
            [
                Course(schedule_guid="g-keep", node_name="海南第一二节", class_date=past),
                Course(schedule_guid="g-both", node_name="海南第三四节", class_date=past),
                Course(schedule_guid="g-drop", node_name="海南第五六节", class_date=past),
            ]
        )
        self.db.add_all(
            [
                Student(student_no="202400000001N", name="本院甲", grade="2023"),
                Student(student_no="202400000001N", name="本院乙", grade="2024"),
                # 本院但无尾字母的人：**必须留下**（上线库里有 96 个这样的人）
                Student(student_no="202400000001", name="本院丙", grade="2024"),
                Student(student_no="202400000001N", name="外院甲", grade="2024"),
                Student(student_no="2024000000011", name="外院乙", grade="2024"),
            ]
        )
        self.db.add_all(
            [
                # 只被"该删的人"引用的课
                AttendanceEvent(source="hanboer", schedule_guid="g-drop",
                                student_no="202400000001N", class_date=past,
                                attend_status="缺勤"),
                # 两个人共用的课：删完一个人后仍有引用，不能删课
                AttendanceEvent(source="hanboer", schedule_guid="g-both",
                                student_no="202400000001N", class_date=past,
                                attend_status="出勤"),
                AttendanceEvent(source="hanboer", schedule_guid="g-both",
                                student_no="202400000001N", class_date=past,
                                attend_status="缺勤"),
                # 完全没被引用的课
                AttendanceEvent(source="hanboer", schedule_guid="g-keep",
                                student_no="202400000001N", class_date=past,
                                attend_status="出勤"),
            ]
        )
        self.db.add_all(
            [
                StudentCourse(student_no="202400000001N", schedule_guid="g-drop",
                              class_date=past, source="hanboer"),
                AttendanceDaily(student_no="202400000001N", class_date=past,
                                has_class=True, state="异常"),
                Alert(student_no="202400000001N", kind="异常", state="异常",
                      consecutive_days=1, first_date=past, last_date=past),
                StudentMark(student_no="202400000001N", action="mark",
                            state="活跃", reason="测试", by="tester"),
            ]
        )
        self.db.commit()

    def test_out_of_scope_students_flags_both_shapes(self) -> None:
        drop, total = scope.out_of_scope_students(self.db)
        self.assertEqual(total, 5)
        self.assertEqual(sorted(drop), ["202400000001N", "2024000000011"])

    def test_dry_run_changes_nothing(self) -> None:
        result = scope.purge_out_of_scope_students(self.db, dry_run=True)
        self.assertEqual(result["students_dropped"], 2)
        self.assertEqual(result["students_deleted"], 0)
        self.assertEqual(self.db.query(Student).count(), 5)
        self.assertEqual(self.db.query(AttendanceEvent).count(), 4)

    def test_purge_removes_students_and_all_their_traces(self) -> None:
        result = scope.purge_out_of_scope_students(self.db)
        self.assertEqual(result["students_deleted"], 2)
        self.assertEqual(result["events_deleted"], 2)
        self.assertEqual(result["daily_deleted"], 1)
        self.assertEqual(result["alerts_deleted"], 1)
        self.assertEqual(result["marks_deleted"], 1)
        self.assertEqual(result["links_deleted"], 1)

        self.assertEqual(
            sorted(s.student_no for s in self.db.query(Student).all()),
            ["202400000001N", "202400000001", "202400000001N"],
        )
        # 只被删掉的人引用过的课被清掉；共用的课留下
        self.assertEqual(
            sorted(c.schedule_guid for c in self.db.query(Course).all()),
            ["g-both", "g-keep"],
        )

    def test_purge_is_noop_when_scope_disabled(self) -> None:
        """最危险的操作：开关关着时绝不能删任何东西。"""
        settings.hb_student_id_scope_enabled = False
        result = scope.purge_out_of_scope_students(self.db)
        self.assertEqual(result["students_dropped"], 0)
        self.assertEqual(result["students_deleted"], 0)
        self.assertEqual(self.db.query(Student).count(), 5)

    def test_purge_twice_is_safe(self) -> None:
        scope.purge_out_of_scope_students(self.db)
        again = scope.purge_out_of_scope_students(self.db)
        self.assertEqual(again["students_dropped"], 0)
        self.assertEqual(again["students_deleted"], 0)

    def test_future_orphan_course_is_kept(self) -> None:
        """今天及以后的孤儿课要留着 —— 它可能就是本院下一节课，明细还没回传。"""
        from datetime import date, timedelta

        from app.models import Course

        future_day = date.today() + timedelta(days=1)
        self.db.add(Course(schedule_guid="g-future", node_name="海南第七八节",
                           class_date=future_day))
        self.db.add(AttendanceEvent(
            source="hanboer", schedule_guid="g-future",
            student_no="202400000001N", class_date=future_day,
            attend_status="缺勤",
        ))
        self.db.commit()

        scope.purge_out_of_scope_students(self.db)
        kept = {c.schedule_guid for c in self.db.query(Course).all()}
        self.assertIn("g-future", kept)


class TestIngestAppliesStudentScope(unittest.TestCase):
    """写入的**唯一入口**必须真的过闸门 —— 光有 filter 函数不算接好。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved_enabled = settings.hb_student_id_scope_enabled
        cls._saved_pattern = settings.hb_student_id_pattern

    def setUp(self) -> None:
        settings.hb_student_id_scope_enabled = True
        settings.hb_student_id_pattern = r"(?i)^(cuc)?202\d290\d3\d{3}[0-9A-Z]?$"
        self.db = SessionLocal()
        for table in (StudentCourse, AttendanceEvent, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        settings.hb_student_id_scope_enabled = self._saved_enabled
        settings.hb_student_id_pattern = self._saved_pattern

    def _event(self, no: str):
        from app.adapters.base import NormalizedEvent

        return NormalizedEvent(
            source="hanboer", schedule_guid="g1", student_no=no,
            class_date=_day(), attend_status="出勤",
        )

    def test_upsert_events_drops_foreign_ids(self) -> None:
        from app.services.ingest import upsert_events

        n = upsert_events(
            self.db,
            [self._event("202400000001N"), self._event("202400000001084")],
        )
        self.db.commit()
        self.assertEqual(n, 1)
        self.assertEqual(
            [e.student_no for e in self.db.query(AttendanceEvent).all()],
            ["202400000001N"],
        )

    def test_upsert_students_drops_foreign_ids_and_logs_them(self) -> None:
        from app.services.ingest import (
            describe_dropped_ids,
            start_id_scope_accounting,
            take_dropped_ids,
            upsert_students,
        )

        start_id_scope_accounting()
        n = upsert_students(
            self.db,
            [
                {"student_no": "202400000001N", "name": "本院"},
                {"student_no": "202400000001084", "name": "本部"},
            ],
        )
        self.assertEqual(n, 1)
        box = take_dropped_ids()
        note = describe_dropped_ids(box)
        # 挡掉的人必须留下痕迹，否则"采集成功但少了 300 人"没人会发现。
        # 记账里是**规范化后**的学号（先剥 cuc 再过滤），这正是入库时的形态。
        self.assertEqual(box["student"], ["202400000001084"])
        self.assertIn("学号作用域过滤", note)

    def test_accounting_is_quiet_without_window(self) -> None:
        """没开记账窗口时不能报错（CLI 单条调用等场景）。"""
        from app.services.ingest import upsert_students

        n = upsert_students(self.db, [{"student_no": "2024010001", "name": "X"}])
        self.assertEqual(n, 0)

    def test_disabled_scope_writes_everything(self) -> None:
        from app.services.ingest import upsert_events, upsert_students

        settings.hb_student_id_scope_enabled = False
        self.assertEqual(
            upsert_students(self.db, [{"student_no": "2024010001", "name": "X"}]), 1
        )
        self.assertEqual(upsert_events(self.db, [self._event("2024010002")]), 1)


class TestMockDataPassesScope(unittest.TestCase):
    """回归锁定：mock 学号必须按真实形态生成。

    否则一开学号作用域，演示库的 1200 名模拟学生会被**全员挡掉** ——
    页面空白，而"为什么空白"极难猜到（过滤器在写入层，界面毫无提示）。
    """

    def test_mock_students_are_hainan_shaped(self) -> None:
        from app.adapters.mock_source import make_students

        saved = settings.hb_student_id_pattern
        settings.hb_student_id_pattern = r"(?i)^(cuc)?202\d290\d3\d{3}[0-9A-Z]?$"
        try:
            students = make_students(1200)
            self.assertEqual(len(students), 1200)
            bad = [s.student_no for s in students if not scope.student_id_in_scope(s.student_no)]
            self.assertEqual(bad[:5], [])
            # 学号切片（STUDENT_ID_MAJOR_SLICE=6:9）取到的专业码要与学生自带的
            # major_code 一致 —— 对不上时页面上会分成两个看不出区别的组。
            self.assertEqual(
                {s.student_no[6:9] for s in students}, {s.major_code for s in students}
            )
            # 学院码第 5~6 位必须是 29，否则这条"本院"规则形同虚设
            self.assertEqual({s.student_no[4:6] for s in students}, {"29"})
        finally:
            settings.hb_student_id_pattern = saved


class TestStudentsCliSafety(unittest.TestCase):
    """``app.cli students`` 的防误删闸门。

    为什么单独测：线上库首跑这条命令要删 4924/5957 = 83% 的学生。而
    「学号正则写错一位」和「上游真的灌进来一堆外院学生」在报告上长得一模一样，
    所以大比例删除必须多敲一个 ``--yes``，把"手滑"和"确实要删"分开。
    """

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()
        cls._saved_enabled = settings.hb_student_id_scope_enabled
        cls._saved_pattern = settings.hb_student_id_pattern

    def setUp(self) -> None:
        settings.hb_student_id_scope_enabled = True
        settings.hb_student_id_pattern = r"(?i)^(cuc)?202\d290\d3\d{3}[0-9A-Z]?$"
        self.db = SessionLocal()
        for table in (AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        settings.hb_student_id_scope_enabled = self._saved_enabled
        settings.hb_student_id_pattern = self._saved_pattern

    def _run_cli(self, argv: list[str]) -> int:
        from app.cli import main

        return main(argv)

    def _seed_mixed(self, ours: int, theirs: int) -> None:
        for i in range(ours):
            self.db.add(Student(student_no=f"202429013{i:03d}N", name=f"本院{i}"))
        for i in range(theirs):
            # 学院码 20，不是本院
            self.db.add(Student(student_no=f"202420102{i:04d}", name=f"外院{i}"))
        self.db.commit()

    def test_report_only_by_default(self) -> None:
        self._seed_mixed(ours=2, theirs=1)
        self.assertEqual(self._run_cli(["students"]), 0)
        self.assertEqual(self.db.query(Student).count(), 3)

    def test_small_purge_needs_no_extra_flag(self) -> None:
        """小比例删除不该被拦住（否则正常清理会很烦）。"""
        self._seed_mixed(ours=50, theirs=1)
        self.assertEqual(self._run_cli(["students", "--purge"]), 0)
        self.assertEqual(self.db.query(Student).count(), 50)

    def test_big_purge_requires_yes(self) -> None:
        self._seed_mixed(ours=2, theirs=200)
        self.assertEqual(self._run_cli(["students", "--purge"]), 1)
        self.assertEqual(self.db.query(Student).count(), 202, "未确认时不得删任何东西")

    def test_big_purge_proceeds_with_yes(self) -> None:
        self._seed_mixed(ours=2, theirs=200)
        self.assertEqual(self._run_cli(["students", "--purge", "--yes"]), 0)
        self.assertEqual(
            sorted(s.student_no for s in self.db.query(Student).all()),
            ["202400000001N", "202400000001N"],
        )


class TestCollectUsesScope(unittest.TestCase):
    """采集入口必须真的调用过滤器 —— 光有 filter 函数不算接好。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = settings.hb_campus_keywords

    def setUp(self) -> None:
        settings.hb_campus_keywords = "海南"

    def tearDown(self) -> None:
        settings.hb_campus_keywords = self._saved

    def test_apply_scope_drops_other_campus(self) -> None:
        from app.services.collect import _apply_scope

        kept = _apply_scope(
            [
                course("1", node_name="海南第一二节", building_name="A座"),
                course("2", node_name="第一二节", building_name="48号教学楼"),
                course("3", node_name="海南第七八节", building_name="B座"),
            ]
        )
        self.assertEqual([c.schedule_guid for c in kept], ["1", "3"])

    def test_apply_scope_noop_without_keywords(self) -> None:
        from app.services.collect import _apply_scope

        settings.hb_campus_keywords = ""
        kept = _apply_scope([course("1", node_name="第一二节")])
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
