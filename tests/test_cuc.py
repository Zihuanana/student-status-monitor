# -*- coding: utf-8 -*-
"""中传教务适配器的单元测试。

**全部用假 JSON**，绝不访问真实接口 —— 这既是测试独立性要求，
也是《数据与程序隔离规范》的硬约定（真实学生数据不出内网）。

覆盖三类容易出错的地方：
  1. 解析：周次/星期/节次/时间 —— 用**真实课表里出现过的形态**测
     （截图实测：``2-3周``、``5周``、``1-4周,6-7周``）
  2. 响应信封：对方返回的 data/list/result/rows 各种包法
  3. 字段名容错：键名对不上时靠候选键或 CUC_FIELDS_* 显式映射救回来
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from tests import dbguard  # noqa: E402

dbguard.isolate("cuc")

from app.adapters.cuc import (  # noqa: E402
    CANDIDATES,
    CucClient,
    CucError,
    parse_date,
    parse_datetime,
    parse_period,
    parse_weekday,
    parse_weeks,
)
from app.core.config import settings  # noqa: E402


class TestParseWeeks(unittest.TestCase):
    """周次解析 —— 形态取自真实课表截图。"""

    def test_single_range(self) -> None:
        self.assertEqual(parse_weeks("2-3周"), [2, 3])

    def test_single_week(self) -> None:
        self.assertEqual(parse_weeks("5周"), [5])

    def test_multi_segment(self) -> None:
        """``1-4周,6-7周`` 是截图里真实出现的形态。"""
        self.assertEqual(parse_weeks("1-4周,6-7周"), [1, 2, 3, 4, 6, 7])

    def test_multi_segment_wide(self) -> None:
        self.assertEqual(
            parse_weeks("5-7周,9-17周"),
            [5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17],
        )

    def test_odd_weeks(self) -> None:
        self.assertEqual(parse_weeks("1-16周(单)"), [1, 3, 5, 7, 9, 11, 13, 15])

    def test_even_weeks(self) -> None:
        self.assertEqual(parse_weeks("1-16周(双)"), [2, 4, 6, 8, 10, 12, 14, 16])

    def test_full_width_comma(self) -> None:
        self.assertEqual(parse_weeks("1-2周，4周"), [1, 2, 4])

    def test_reversed_range_is_normalized(self) -> None:
        self.assertEqual(parse_weeks("7-5周"), [5, 6, 7])

    def test_empty_and_garbage(self) -> None:
        """解析不出来就返回空 —— 宁可少算一次课，也不能算到错误日期上。"""
        for text in ("", "   ", "abc", "无", None):
            self.assertEqual(parse_weeks(text), [])

    def test_absurd_range_skipped(self) -> None:
        """明显越界的区间跳过，不生成上千个周次。"""
        self.assertEqual(parse_weeks("1-9999周"), [])


class TestParseWeekday(unittest.TestCase):
    def test_chinese_text(self) -> None:
        self.assertEqual(parse_weekday("星期一"), 1)
        self.assertEqual(parse_weekday("星期六"), 6)
        self.assertEqual(parse_weekday("星期日"), 7)
        self.assertEqual(parse_weekday("星期天"), 7)

    def test_short_form(self) -> None:
        self.assertEqual(parse_weekday("周四"), 4)

    def test_digit_in_text(self) -> None:
        self.assertEqual(parse_weekday("星期4"), 4)

    def test_number(self) -> None:
        self.assertEqual(parse_weekday(3), 3)

    def test_out_of_range(self) -> None:
        self.assertEqual(parse_weekday(0), 0)
        self.assertEqual(parse_weekday(9), 0)
        self.assertEqual(parse_weekday(None), 0)


class TestParsePeriod(unittest.TestCase):
    def test_int(self) -> None:
        self.assertEqual(parse_period(1), 1)
        self.assertEqual(parse_period(7), 7)

    def test_string(self) -> None:
        self.assertEqual(parse_period("5"), 5)
        self.assertEqual(parse_period("5-6"), 5)
        self.assertEqual(parse_period("第3节"), 3)

    def test_empty(self) -> None:
        self.assertEqual(parse_period(None), 0)
        self.assertEqual(parse_period(""), 0)


class TestParseDatetime(unittest.TestCase):
    def test_space_separated(self) -> None:
        self.assertEqual(
            parse_datetime("2026-10-22 14:30:00"), datetime(2026, 10, 22, 14, 30)
        )

    def test_iso(self) -> None:
        self.assertEqual(
            parse_datetime("2026-10-22T14:30:00"), datetime(2026, 10, 22, 14, 30)
        )

    def test_with_millis_and_z(self) -> None:
        self.assertEqual(
            parse_datetime("2026-10-22T14:30:00.000Z"), datetime(2026, 10, 22, 14, 30)
        )

    def test_date_only(self) -> None:
        self.assertEqual(parse_date("2026-10-22"), date(2026, 10, 22))

    def test_unix_seconds(self) -> None:
        got = parse_datetime(1792650000)
        self.assertIsNotNone(got)
        self.assertEqual(got.year, 2026)  # type: ignore[union-attr]

    def test_garbage(self) -> None:
        self.assertIsNone(parse_datetime("不是时间"))
        self.assertIsNone(parse_datetime(None))


class TestEnvelope(unittest.TestCase):
    """响应信封：对方把数据包在哪儿都能掏出来。"""

    def setUp(self) -> None:
        self._saved = {k: getattr(settings, k) for k in ("cuc_enabled", "cuc_base_url")}
        settings.cuc_enabled = True
        settings.cuc_base_url = "https://example.invalid/api"
        self.client = CucClient()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)

    def test_bare_list(self) -> None:
        rows = self.client._rows([{"a": 1}, {"a": 2}])
        self.assertEqual(len(rows), 2)

    def test_data_list(self) -> None:
        self.assertEqual(len(self.client._rows({"data": [{"a": 1}]})), 1)

    def test_data_list_wrapper(self) -> None:
        """``{"data":{"list":[...]}}`` 是常见的两级包法。"""
        got = self.client._rows({"code": 0, "data": {"list": [{"a": 1}, {"a": 2}]}})
        self.assertEqual(len(got), 2)

    def test_result_records(self) -> None:
        got = self.client._rows({"success": True, "result": {"records": [{"a": 1}]}})
        self.assertEqual(len(got), 1)

    def test_rows_key(self) -> None:
        self.assertEqual(len(self.client._rows({"rows": [{"a": 1}]})), 1)

    def test_business_error_code_raises(self) -> None:
        """HTTP 200 但业务码非 0，必须报错而不是当成空数据。"""
        with self.assertRaises(CucError) as ctx:
            self.client._rows({"code": 500, "msg": "无权限"})
        self.assertIn("无权限", str(ctx.exception))

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(self.client._rows({"data": []}), [])

    def test_non_dict_raises(self) -> None:
        with self.assertRaises(CucError):
            self.client._rows("just a string")


class TestFieldTolerance(unittest.TestCase):
    """字段名容错：键名和我们猜的不一样时也能取到值。"""

    def setUp(self) -> None:
        self._saved = {
            k: getattr(settings, k)
            for k in ("cuc_enabled", "cuc_base_url", "cuc_fields_student",
                      "cuc_fields_timetable")
        }
        settings.cuc_enabled = True
        settings.cuc_base_url = "https://example.invalid/api"
        settings.cuc_fields_student = ""
        settings.cuc_fields_timetable = ""
        self.client = CucClient()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)

    def test_candidate_key(self) -> None:
        row = {"xh": "202400000001N", "xm": "张三"}
        self.assertEqual(self.client._text(row, "student", "student_no"), "202400000001N")
        self.assertEqual(self.client._text(row, "student", "name"), "张三")

    def test_english_camel_case(self) -> None:
        row = {"studentNo": "202400000001N", "studentName": "张三"}
        self.assertEqual(self.client._text(row, "student", "student_no"), "202400000001N")
        self.assertEqual(self.client._text(row, "student", "name"), "张三")

    def test_case_insensitive_fallback(self) -> None:
        row = {"XH": "202400000001N"}
        self.assertEqual(self.client._text(row, "student", "student_no"), "202400000001N")

    def test_explicit_mapping_wins(self) -> None:
        """键名完全不在候选列表里时，靠 CUC_FIELDS_* 显式映射救回来。"""
        settings.cuc_fields_student = "student_no=some_weird_key;name=another_key"
        row = {"some_weird_key": "202400000001N", "another_key": "李四"}
        self.assertEqual(self.client._text(row, "student", "student_no"), "202400000001N")
        self.assertEqual(self.client._text(row, "student", "name"), "李四")

    def test_missing_returns_empty(self) -> None:
        self.assertEqual(self.client._text({}, "student", "name"), "")


class TestNormalization(unittest.TestCase):
    """fetch_* 的归一化（不经过网络，直接打桩 _get）。"""

    def setUp(self) -> None:
        self._saved = {
            k: getattr(settings, k)
            for k in ("cuc_enabled", "cuc_base_url", "cuc_term",
                      "cuc_path_calendar", "cuc_path_enrollments",
                      "cuc_path_timetable", "cuc_path_students")
        }
        settings.cuc_enabled = True
        settings.cuc_base_url = "https://example.invalid/api"
        settings.cuc_term = "2026-2027学年秋季学期"
        self.client = CucClient()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(settings, k, v)

    def test_timetable_normalized(self) -> None:
        """一行课表 → CucTimetable，周次/节次/星期都解析好。"""
        settings.cuc_path_timetable = "/timetable"
        self.client._get = lambda *a, **k: [  # type: ignore[method-assign]
            {
                "kcbm": "2291000001",
                "jxbbm": "01",
                "xsmc": "高级算法",
                "jlid": "100000000000001",
                "zc": "1-4周,6-7周",
                "ksjc": "1",
                "jsjc": "2",
                "xq": "星期四",
                "jsbm": "C108",
                "xnxq": "2026-2027学年秋季学期",
            }
        ]
        got = self.client.fetch_timetable()
        self.assertEqual(len(got), 1)
        t = got[0]
        self.assertEqual(t.course_no, "2291000001")
        self.assertEqual(t.teaching_class, "01")
        self.assertEqual(t.course_name, "高级算法")
        self.assertEqual(t.record_id, "100000000000001")
        self.assertEqual(t.weeks, [1, 2, 3, 4, 6, 7])
        self.assertEqual(t.weeks_raw, "1-4周,6-7周")
        self.assertEqual(t.period_span, (1, 2))
        self.assertEqual(t.weekday, 4)
        self.assertEqual(t.classroom, "C108")

    def test_two_teaching_classes_same_course_not_merged(self) -> None:
        """**核心回归**：同一课程号的两个课序号必须区分开。

        真实课表里 2291000001「高级算法」有 01 和 01S01 两个教学班，
        周次/星期/节次/教室全不同。只按课程号连接会把它们合并，
        学生就会凭空多出几次课。
        """
        settings.cuc_path_timetable = "/timetable"
        self.client._get = lambda *a, **k: [  # type: ignore[method-assign]
            {"kcbm": "2291000001", "jxbbm": "01S01", "xsmc": "高级算法",
             "zc": "2-3周", "ksjc": "1", "jsjc": "2", "xq": "星期一", "jsbm": "C301"},
            {"kcbm": "2291000001", "jxbbm": "01", "xsmc": "高级算法",
             "zc": "5周", "ksjc": "1", "jsjc": "1", "xq": "星期六", "jsbm": "C108"},
        ]
        got = self.client.fetch_timetable()
        self.assertEqual(len(got), 2)
        by_class = {t.teaching_class: t for t in got}
        self.assertIn("01", by_class)
        self.assertIn("01S01", by_class)
        # 时间必须各不相同
        self.assertNotEqual(by_class["01"].weeks, by_class["01S01"].weeks)
        self.assertNotEqual(by_class["01"].weekday, by_class["01S01"].weekday)

    def test_calendar_normalized_to_monday(self) -> None:
        """校历给的不是周一，也要规范到那一周的周一。"""
        settings.cuc_path_calendar = "/calendar"
        self.client._get = lambda *a, **k: [{"jxksrq": "2026-09-03"}]  # type: ignore[method-assign]
        cal = self.client.fetch_calendar()
        self.assertEqual(cal.week1_monday, date(2026, 8, 31))
        self.assertEqual(cal.week1_monday.weekday(), 0)

    def test_calendar_unparseable_raises(self) -> None:
        settings.cuc_path_calendar = "/calendar"
        self.client._get = lambda *a, **k: [{"jxksrq": "开学第一周"}]  # type: ignore[method-assign]
        with self.assertRaises(CucError) as ctx:
            self.client.fetch_calendar()
        self.assertIn("CUC_FIELDS_CALENDAR", str(ctx.exception))

    def test_enrollment_normalized(self) -> None:
        settings.cuc_path_enrollments = "/enrollments"
        self.client._get = lambda *a, **k: [  # type: ignore[method-assign]
            {"xh": "202400000001N", "xnxq": "2026-2027学年秋季学期",
             "xklb": "必修", "jxbbm": "01", "kcbm": "2291000001"}
        ]
        got = self.client.fetch_enrollments()
        self.assertEqual(len(got), 1)
        e = got[0]
        self.assertEqual(e.student_no, "202400000001N")
        self.assertEqual(e.category, "必修")
        self.assertEqual(e.teaching_class, "01")
        self.assertEqual(e.course_no, "2291000001")

    def test_student_strips_cuc_prefix(self) -> None:
        """接口可能返回 ``cuc2024…`` 前缀，要剥掉。"""
        settings.cuc_path_students = "/students"
        self.client._get = lambda *a, **k: [{"xh": "202400000001N", "xm": "张三"}]  # type: ignore[method-assign]
        got = self.client.fetch_students()
        self.assertEqual(got[0].student_no, "202400000001N")

    def test_rows_without_student_no_skipped(self) -> None:
        settings.cuc_path_enrollments = "/enrollments"
        self.client._get = lambda *a, **k: [  # type: ignore[method-assign]
            {"xh": "", "kcbm": "2291000001"},
            {"xh": "202400000001N", "kcbm": "2291000001"},
        ]
        self.assertEqual(len(self.client.fetch_enrollments()), 1)


class TestProbeShape(unittest.TestCase):
    """probe 输出里**不能出现任何真实值** —— 只报类型与长度。"""

    def test_shape_hides_values(self) -> None:
        from app.adapters.cuc import _shape

        shaped = _shape({"xh": "202400000001N", "xm": "张三", "n": 42, "ok": True})
        self.assertEqual(shaped["xh"], "str(13)")
        self.assertEqual(shaped["xm"], "str(2)")
        self.assertEqual(shaped["n"], "number")
        self.assertEqual(shaped["ok"], "bool")
        # 关键：不能把真实值带出来
        dump = str(shaped)
        self.assertNotIn("202400000001N", dump)
        self.assertNotIn("张三", dump)

    def test_shape_nested(self) -> None:
        from app.adapters.cuc import _shape

        shaped = _shape({"code": 0, "data": {"list": [{"a": 1}]}})
        self.assertEqual(shaped["code"], "number")
        self.assertIn("list", str(shaped))


class TestDisabledByDefault(unittest.TestCase):
    def test_client_refuses_when_disabled(self) -> None:
        saved = settings.cuc_enabled
        settings.cuc_enabled = False
        try:
            with self.assertRaises(CucError) as ctx:
                CucClient()
            self.assertIn("CUC_ENABLED", str(ctx.exception))
        finally:
            settings.cuc_enabled = saved

    def test_client_refuses_without_base_url(self) -> None:
        saved = (settings.cuc_enabled, settings.cuc_base_url)
        settings.cuc_enabled = True
        settings.cuc_base_url = ""
        try:
            with self.assertRaises(CucError) as ctx:
                CucClient()
            self.assertIn("CUC_BASE_URL", str(ctx.exception))
        finally:
            settings.cuc_enabled, settings.cuc_base_url = saved


class TestCandidatesCoverage(unittest.TestCase):
    """每个数据源都必须有候选键 —— 少一个就会拿不到值。"""

    def test_all_kinds_present(self) -> None:
        for kind in ("calendar", "student", "enrollment", "timetable", "checkin"):
            self.assertIn(kind, CANDIDATES, f"缺少 {kind} 的候选键")

    def test_no_empty_candidate(self) -> None:
        for kind, fields in CANDIDATES.items():
            for name, keys in fields.items():
                if name == "raw":
                    continue
                self.assertTrue(keys, f"{kind}.{name} 的候选键为空")


if __name__ == "__main__":
    unittest.main()
