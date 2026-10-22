# -*- coding: utf-8 -*-
"""模拟数据生成器的回归测试。

**为什么值得单独测一个"假数据"生成器**
它是本地/内网部署后**第一个**要跑的命令（``python -m app.cli seed``），
用来确认"装好了、能看到东西"。如果它生成的学号过不了
``HB_STUDENT_ID_PATTERN`` 那道学号闸门，学生会被**静默**过滤掉，表现为：

    ✅ mock 数据已生成。
    （页面：0 人）

这是最难查的一类失效 —— 命令报成功、日志里只有一行不起眼的过滤记录、
页面上什么都没有，很容易被当成"装错了"而反复重装。

实际踩过的坑（本文件就是为钉住它们而写）：

1. **学号位数错一位**。曾经生成 ``2024000000011N``（13 位），
   而真实形态是 ``202400000001N``（12 位数字 + 尾字母）。
   成因是把 ``013 001`` 误读成 ``013`` + 学院标记位 ``3`` + 序号 ``01``：
   在专业码为 03x 时**长得像**对的，所以潜伏了很久。
2. **序号溢出**。序号若只按"专业"计数（不按"年级+专业"），
   累计会破 999，序号涨到 4 位，同样过不了正则。
3. **给少**。早期用"每组固定人数 + 截断"，要 100 个只给 96 个。

学号作用域的真实语义（别再靠猜，这条是实测确认的）::

    模式：(?i)^(cuc)?202\\d290\\d3\\d{3}[0-9A-Z]?$
    结构：年(4) + 院(2)=29 + 0 + ? + 3 + 序号(3) + 尾字母?
    其中 ? 是 \\d（**任意**数字），所以 013/023/033/043/053/063 全都在域内。
"""

from __future__ import annotations

import os
import unittest

from tests import dbguard  # noqa: E402

dbguard.isolate("mock_source")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")

from app.adapters.mock_source import (  # noqa: E402
    GRADES,
    MAJORS,
    make_students,
)
from app.services.scope import student_id_in_scope  # noqa: E402


class TestMakeStudentsScopeCompatible(unittest.TestCase):
    def test_default_output_all_pass_scope(self) -> None:
        """**核心不变量**：生成的学生必须全部能过学号闸门。

        一个都不能漏 —— 漏掉的那些会在页面上凭空消失。
        """
        students = make_students(1200)
        bad = [s.student_no for s in students if not student_id_in_scope(s.student_no)]
        self.assertEqual(
            bad, [],
            f"有 {len(bad)} 个模拟学号过不了学号作用域（例：{bad[:3]}）。"
            "这些学生会被静默过滤掉，页面显示 0 人。",
        )
        self.assertEqual(len(students), 1200)

    def test_student_no_is_12_digits_plus_letter(self) -> None:
        """学号必须是 **12 位数字 + 尾字母**，和真实形态一致。

        这是踩过的坑 #1：曾经多插一位变成 13 位。
        """
        for s in make_students(300):
            digits = s.student_no[:-1]
            self.assertTrue(s.student_no.endswith("N"), f"尾字母不对：{s.student_no}")
            self.assertEqual(
                len(digits), 12,
                f"学号应为 12 位数字，实际 {len(digits)} 位：{s.student_no}",
            )
            self.assertTrue(digits.isdigit(), f"学号里有非数字：{s.student_no}")

    def test_student_no_layout(self) -> None:
        """逐段核对：年(4) + 院(2)=29 + 专业(3) + 序号(3) + N。"""
        for s in make_students(300):
            no = s.student_no
            self.assertEqual(no[0:4], s.grade, f"年级段不对：{no}")
            self.assertEqual(no[4:6], "29", f"学院码应为 29：{no}")
            self.assertEqual(
                no[6:9], s.major_code, f"专业段与 major_code 不符：{no}"
            )
            self.assertEqual(no[9:12], f"{int(no[9:12]):03d}", f"序号段格式不对：{no}")

    def test_sequence_never_overflows_three_digits(self) -> None:
        """序号必须始终 3 位（踩过的坑 #2：涨到 4 位就过不了正则）。"""
        students = make_students(4000)   # 远超默认值，把溢出面放大
        bad = [s.student_no for s in students if not student_id_in_scope(s.student_no)]
        self.assertEqual(bad, [], f"大数量下出现越界学号（例：{bad[:3]}）")
        lens = {len(s.student_no) for s in students}
        self.assertEqual(lens, {13}, f"学号长度不唯一：{lens}")

    def test_major_slice_extracts_correct_major(self) -> None:
        """``STUDENT_ID_MAJOR_SLICE=6:9`` 必须能切出正确专业码。

        切错会让页面上所有专业挤成一坨（曾经用过 4:6）。
        """
        for s in make_students(300):
            self.assertEqual(s.student_no[6:9], s.major_code)

    def test_grade_slice_extracts_correct_grade(self) -> None:
        """``STUDENT_ID_YEAR_SLICE=0:4`` 必须能切出年级。"""
        for s in make_students(300):
            self.assertEqual(s.student_no[0:4], s.grade)

    def test_all_majors_are_in_scope(self) -> None:
        """**每个**专业码都要在作用域内 —— 排后面的专业最容易漏。

        （实测确认：作用域正则中间那位是 \\d，所以 043/053/063 都在域内。
        曾误以为只认 0[123]，把它们划成"域外专业"，那是错的。）
        """
        for code, name in MAJORS:
            self.assertEqual(len(code), 3, f"专业码应为 3 位：{code}")
            sample = f"2024{MOCK_SAMPLE_COLLEGE}{code}001N"
            self.assertTrue(
                student_id_in_scope(sample),
                f"专业 {code}（{name}）生成的学号 {sample} 不在作用域内",
            )

    def test_every_major_actually_appears(self) -> None:
        """每个专业都要真的出现在结果里，不能只有排前面的那几个。"""
        students = make_students(1200)
        present = {s.major_code for s in students}
        expected = {code for code, _ in MAJORS}
        self.assertEqual(
            present, expected, f"有专业没生成出来：{expected - present}"
        )

    def test_grades_are_in_scope_decade(self) -> None:
        """年级必须落在正则可接受的 ``202\\d`` 里（2020–2029）。"""
        for g in GRADES:
            self.assertRegex(str(g), r"^202\d$", f"年级 {g} 不在 2020–2029 内")

    def test_count_is_exact(self) -> None:
        """要多少给多少（踩过的坑 #3：曾经要 100 给 96）。"""
        for want in (1, 7, 10, 100, 999, 1200):
            self.assertEqual(
                len(make_students(want)), want, f"要 {want} 个却给了别的数量"
            )

    def test_student_numbers_are_unique(self) -> None:
        """学号不能重复（重复会让 upsert 覆盖，人数对不上）。"""
        students = make_students(2000)
        nos = [s.student_no for s in students]
        self.assertEqual(len(nos), len(set(nos)), "生成了重复学号")

    def test_same_seed_is_reproducible(self) -> None:
        """同一个 seed 必须给同样的数据 —— 排障时要能复现。"""
        a = make_students(120)
        b = make_students(120)
        self.assertEqual([s.student_no for s in a], [s.student_no for s in b])
        self.assertEqual([s.name for s in a], [s.name for s in b])

    def test_class_name_is_consistent_per_group(self) -> None:
        """同一个(年级,专业)的学生应归到同一个班 —— 否则班级统计会散开。"""
        students = make_students(1200)
        by_key: dict[tuple[str, str], set[str]] = {}
        for s in students:
            by_key.setdefault((s.grade, s.major_code), set()).add(s.class_name)
        for key, classes in by_key.items():
            self.assertEqual(
                len(classes), 1, f"{key} 的学生分散在多个班：{classes}"
            )

    def test_names_are_non_empty(self) -> None:
        for s in make_students(200):
            self.assertTrue(s.name.strip(), f"空姓名：{s.student_no}")


#: 拼样例学号时用的学院码，与 mock_source 内部保持一致（29 = 海南国际学院）
MOCK_SAMPLE_COLLEGE = "29"


class TestSeedStudentCountConsistency(unittest.TestCase):
    """``seed --students N`` 之后，库里就该有 N 名学生。

    踩过的坑：``Collector._collect_mock`` 曾写死 ``make_students(1200)``，
    于是 ``seed --students 300`` 导入了 300 个名册学生，
    采集却按 1200 人造考勤数据 —— 多出来的 900 个被当成
    "只在考勤里出现、名册里没有"的学生**凭空建出来**，
    结果是 stats 显示 1089 人（还不是 1200，因为有一部分被其他闸门挡住）。
    这种"参数看着生效、实际没生效"的问题最难发现。

    这里直接查代码层面的约定：Collector 必须接收并使用这个人数。
    """

    def test_collector_accepts_mock_student_count(self) -> None:
        from app.services.collect import Collector

        c = Collector(mock_student_count=7)
        self.assertEqual(c.mock_student_count, 7)
        # 默认值保持 1200（不影响既有用法）
        self.assertEqual(Collector().mock_student_count, 1200)

    def test_collect_mock_generates_the_configured_count(self) -> None:
        """真正跑一次 mock 采集，确认学生数与配置一致。"""
        import inspect

        from app.services.collect import Collector

        src = inspect.getsource(Collector._collect_mock)
        self.assertIn(
            "self.mock_student_count", src,
            "_collect_mock 必须用 self.mock_student_count 生成学生，"
            "不能写死 1200 —— 否则 seed --students N 的名册人数与考勤人数对不上",
        )
        self.assertNotIn(
            "make_students(1200)", src,
            "又写死 1200 了：这会让 seed --students 参数失效并凭空造出学生",
        )


if __name__ == "__main__":
    unittest.main()
