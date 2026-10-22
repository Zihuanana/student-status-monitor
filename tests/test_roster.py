# -*- coding: utf-8 -*-
"""名册/课表导入与关联校验测试。

重点覆盖国内教务导出的真实情况：
- CSV 是 **GBK** 编码（不是 UTF-8）
- 表头前面有标题行
- 班级名在名册与考勤系统之间写法不一致 —— 这会让缺勤反推**静默失效**

数据库说明
----------
``app.core.config.settings`` 与 ``app.core.db`` 都是**模块级单例**，测试模块之间
无法真正隔离数据库（``init_db()`` 还按模块级 ``engine`` 的闭包建表，改
``db_module.engine`` 也不会生效）。因此本模块不另建库，而是与其它测试模块
**共用同一个测试库**：在导入 app 之前把环境变量指到临时目录，并让每个测试类
在自己的 setUp 里清空相关表，从而保证互不干扰。

> 踩坑记录：曾经尝试在 setUpClass 里重建引擎并替换 ``db_module.engine``，
> 结果表被建到另一个库上，CLI 命令读不到数据 —— 单独跑通过、全套跑必失败。
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

# ── 必须在导入 app.* 之前设置：指向临时目录，避免污染工作区 data/ ──
from tests import dbguard  # noqa: E402

# 必须在导入 app.* 之前隔离数据库 —— app.core.db 的引擎在首个导入者处
# 就已绑定，晚设置会连到工作区真实库（曾把演示数据清空）。
_DB_PATH = dbguard.isolate("roster")

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import AttendanceEvent, Course, Student, StudentCourse  # noqa: E402
from app.services.roster import (  # noqa: E402
    import_schedule,
    import_students,
    parse_weekday,
    roster_check,
)
from app.services.spreadsheet import (  # noqa: E402
    detect_columns,
    find_header_row,
    read_table,
    sniff_text,
)

# 夹具文件与隔离库放在同一临时目录
OUT = Path(_DB_PATH).parent


def write_csv(name: str, text: str, encoding: str = "utf-8") -> Path:
    p = OUT / name
    p.write_bytes(text.encode(encoding))
    return p


def write_xlsx(name: str, rows: list[list[str]], sheet_name: str = "Sheet1") -> Path:
    """用标准库手写一个最小 XLSX（验证我们不依赖 openpyxl）。"""
    def col_letter(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, rem = divmod(i - 1, 26)
            s = chr(65 + rem) + s
        return s

    shared: list[str] = []
    index: dict[str, int] = {}

    def sid(text: str) -> int:
        if text not in index:
            index[text] = len(shared)
            shared.append(text)
        return index[text]

    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{col_letter(c)}{r}"
            if value == "":
                continue
            cells.append(f'<c r="{ref}" t="s"><v>{sid(value)}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{t}</t></si>" for t in shared)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )

    p = OUT / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return p


ROSTER_CSV = """学号,姓名,年级,专业代码,专业名称,班级,学院
2024010001,张三,2024,01,智能科学与技术,智能科学与技术241班,海南国际学院
2024010002,李四,2024,01,智能科学与技术,智能科学与技术241班,海南国际学院
2024020001,王五,2024,02,数字媒体技术,数字媒体技术241班,海南国际学院
"""

SCHEDULE_CSV = """班级,课程名称,教师,星期,节次,开始时间,结束时间,教室,周次
智能科学与技术241班,高等数学,0107t1,周一,第一节,08:00,09:35,Q3101,1-16
智能科学与技术241班,大学物理,0231t2,周三,第二节,10:00,11:35,Q3202,1-16
数字媒体技术241班,数字图像处理,0455t3,周二,第一节,08:00,09:35,Q2101,1-16
"""


class _DbTestCase(unittest.TestCase):
    """统一清库的基类，保证与其它测试模块共用库时互不干扰。"""

    @classmethod
    def setUpClass(cls) -> None:
        dbguard.ensure_schema()

    def setUp(self) -> None:
        self.db = SessionLocal()
        for table in (StudentCourse, AttendanceEvent, Course, Student):
            self.db.query(table).delete()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()


class TestSpreadsheetParsing(unittest.TestCase):
    def test_sniff_gbk(self) -> None:
        raw = "学号,姓名\n1,张三\n".encode("gb18030")
        text, enc = sniff_text(raw)
        self.assertIn("学号", text)
        self.assertEqual(enc, "gb18030")

    def test_sniff_utf8_bom(self) -> None:
        raw = "学号,姓名\n1,张三\n".encode("utf-8-sig")
        text, enc = sniff_text(raw)
        self.assertEqual(enc, "utf-8-sig")

    def test_read_gbk_csv(self) -> None:
        p = write_csv("roster_gbk.csv", ROSTER_CSV, encoding="gb18030")
        rows, enc = read_table(p)
        self.assertIn("gb18030", enc)
        self.assertEqual(rows[0][0], "学号")
        self.assertEqual(rows[1][1], "张三")

    def test_read_utf8_bom_csv(self) -> None:
        p = write_csv("roster_bom.csv", ROSTER_CSV, encoding="utf-8-sig")
        rows, _ = read_table(p)
        self.assertEqual(rows[0][0], "学号")

    def test_read_xlsx(self) -> None:
        p = write_xlsx("roster.xlsx", [["学号", "姓名", "班级"], ["2024010001", "张三", "智能241班"]])
        rows, enc = read_table(p)
        self.assertTrue(enc.startswith("XLSX"))
        self.assertEqual(rows[0], ["学号", "姓名", "班级"])
        self.assertEqual(rows[1][1], "张三")

    def test_xls_binary_rejected_with_helpful_message(self) -> None:
        p = OUT / "old.xls"
        p.write_bytes(b"\xd0\xcf\x11\xe0")
        with self.assertRaises(ValueError) as ctx:
            read_table(p)
        self.assertIn("另存为", str(ctx.exception))

    def test_find_header_row_skips_title(self) -> None:
        rows = [
            ["海南国际学院2024级学生名册"],
            [],
            ["学号", "姓名", "班级"],
            ["2024010001", "张三", "智能241班"],
        ]
        self.assertEqual(find_header_row(rows), 2)

    def test_detect_columns_variants(self) -> None:
        mapping = detect_columns(["学生学号", "学生姓名", "行政班", "专业名称"])
        self.assertEqual(mapping["student_no"], 0)
        self.assertEqual(mapping["name"], 1)
        self.assertEqual(mapping["class_name"], 2)
        self.assertEqual(mapping["major_name"], 3)


class TestWeekdayParsing(unittest.TestCase):
    def test_common_forms(self) -> None:
        self.assertEqual(parse_weekday("周一"), 0)
        self.assertEqual(parse_weekday("星期一"), 0)
        self.assertEqual(parse_weekday("1"), 0)
        self.assertEqual(parse_weekday("周3"), 2)
        self.assertEqual(parse_weekday("星期日"), 6)
        self.assertEqual(parse_weekday("Sunday"), 6)

    def test_unknown(self) -> None:
        self.assertIsNone(parse_weekday(""))
        self.assertIsNone(parse_weekday("待定"))


class TestImport(_DbTestCase):
    def test_import_roster_gbk(self) -> None:
        p = write_csv("r1.csv", ROSTER_CSV, encoding="gb18030")
        report = import_students(self.db, p)
        self.assertEqual(report.imported, 3)
        self.assertEqual(report.skipped, 0)
        self.assertFalse([w for w in report.warnings if "班级" in w], "班级列应能识别")

        zhang = self.db.get(Student, "2024010001")
        self.assertIsNotNone(zhang)
        self.assertEqual(zhang.name, "张三")
        self.assertEqual(zhang.grade, "2024")
        self.assertEqual(zhang.major_code, "01")
        self.assertEqual(zhang.major_name, "智能科学与技术")
        self.assertEqual(zhang.class_name, "智能科学与技术241班")
        self.assertEqual(zhang.enroll_year, 2024)

    def test_grade_and_major_inferred_from_student_no(self) -> None:
        csv_text = "学号,姓名,班级\n2024030042,赵六,智能241班\n"
        p = write_csv("r2.csv", csv_text)
        import_students(self.db, p)
        s = self.db.get(Student, "2024030042")
        self.assertEqual(s.grade, "2024")
        self.assertEqual(s.major_code, "03")

    def test_import_xlsx_roster(self) -> None:
        p = write_xlsx(
            "r.xlsx",
            [
                ["学号", "姓名", "班级"],
                ["2024010001", "张三", "智能科学与技术241班"],
                ["2024010002", "李四", "智能科学与技术241班"],
            ],
        )
        report = import_students(self.db, p)
        self.assertEqual(report.imported, 2)
        self.assertEqual(self.db.get(Student, "2024010002").name, "李四")

    def test_duplicate_student_no_skipped(self) -> None:
        csv_text = "学号,姓名\n2024010001,张三\n2024010001,张三重复\n"
        p = write_csv("dup.csv", csv_text)
        report = import_students(self.db, p)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.skipped, 1)
        self.assertTrue(any("重复" in s for s in report.skipped_samples))

    def test_missing_class_column_warns_loudly(self) -> None:
        """没有班级列 → 缺勤反推无法工作，必须明确警告而不是静默通过。"""
        p = write_csv("noclass.csv", "学号,姓名\n2024010001,张三\n")
        report = import_students(self.db, p)
        self.assertEqual(report.imported, 1)
        self.assertTrue(
            any("班级" in w for w in report.warnings),
            f"应警告缺少班级列，实际 warnings={report.warnings}",
        )

    def test_missing_student_no_column(self) -> None:
        p = write_csv("noid.csv", "姓名,班级\n张三,智能241班\n")
        report = import_students(self.db, p)
        self.assertEqual(report.imported, 0)
        self.assertTrue(any("学号" in w for w in report.warnings))

    def test_dry_run_does_not_write(self) -> None:
        p = write_csv("dry.csv", ROSTER_CSV)
        report = import_students(self.db, p, dry_run=True)
        self.assertEqual(report.imported, 3)
        self.assertEqual(self.db.query(Student).count(), 0, "dry-run 不应写库")

    def test_import_schedule_builds_roster_links(self) -> None:
        import_students(self.db, write_csv("r3.csv", ROSTER_CSV))
        report = import_schedule(self.db, write_csv("s3.csv", SCHEDULE_CSV))
        self.assertEqual(report.imported, 3)

        links = self.db.execute(select(StudentCourse)).scalars().all()
        smart = [l for l in links if l.student_no in ("2024010001", "2024010002")]
        self.assertEqual(len(smart), 4)
        self.assertTrue(all(l.source == "schedule" for l in smart))

    def test_schedule_without_roster_warns(self) -> None:
        report = import_schedule(self.db, write_csv("s4.csv", SCHEDULE_CSV))
        self.assertEqual(report.imported, 3)
        self.assertTrue(any("名册" in w for w in report.warnings))

    def test_parse_weekday_column_present(self) -> None:
        import_students(self.db, write_csv("r5.csv", ROSTER_CSV))
        report = import_schedule(self.db, write_csv("s5.csv", SCHEDULE_CSV))
        self.assertIn("weekday", report.columns)


class TestRosterCheck(_DbTestCase):
    """关联校验：班级名对不上时，缺勤反推会静默失效，必须能查出来。"""

    def test_matched_classes_can_infer(self) -> None:
        import_students(self.db, write_csv("rc1.csv", ROSTER_CSV))
        self.db.add(
            Course(
                schedule_guid="g1",
                course_name="高等数学",
                class_name="智能科学与技术241班",
            )
        )
        self.db.commit()

        check = roster_check(self.db)
        self.assertTrue(check["can_infer"])
        self.assertIn("智能科学与技术241班", check["matched_classes"])
        self.assertEqual(check["matched_student_count"], 2)

    def test_mismatched_class_names_detected(self) -> None:
        """名册写「智能241班」、考勤写「数字媒体技术241班」→ 必须报出不可用。"""
        import_students(
            self.db,
            write_csv("rc2.csv", "学号,姓名,班级\n2024010001,张三,智能241班\n"),
        )
        self.db.add(
            Course(
                schedule_guid="g2",
                course_name="高等数学",
                class_name="数字媒体技术241班",
            )
        )
        self.db.commit()

        check = roster_check(self.db)
        self.assertFalse(check["can_infer"], "班级对不上时不应报告可用")
        self.assertIn("智能241班", check["only_in_roster"])
        self.assertIn("数字媒体技术241班", check["only_in_courses"])

    def test_no_data_reports_not_available(self) -> None:
        check = roster_check(self.db)
        self.assertFalse(check["can_infer"])

    def test_partial_match_reports_uncovered_students(self) -> None:
        """部分班级对不上时，必须点出还有多少学生没被覆盖 ——
        否则会误以为"可用"而漏掉真正需要监测的那批人。"""
        from app.services.roster import format_check

        import_students(
            self.db,
            write_csv(
                "rc3.csv",
                "学号,姓名,班级\n"
                "2024010001,张三,智能科学与技术241班\n"
                "2024010002,李四,智能科学与技术241班\n"
                "2024090001,王五,写法不同的班\n",
            ),
        )
        self.db.add(
            Course(
                schedule_guid="g3",
                course_name="高等数学",
                class_name="智能科学与技术241班",
            )
        )
        self.db.commit()

        check = roster_check(self.db)
        self.assertTrue(check["can_infer"])
        self.assertEqual(check["matched_student_count"], 2)
        self.assertEqual(check["students_total"], 3)

        text = format_check(check)
        self.assertIn("班级名覆盖 2 人", text)
        self.assertIn("共 3 人", text)
        self.assertIn("部分可用", text)
        self.assertNotIn("✅ 缺勤反推可用：全部学生", text)

    def test_full_match_reports_all_covered(self) -> None:
        from app.services.roster import format_check

        import_students(self.db, write_csv("rc4.csv", ROSTER_CSV))
        self.db.add(
            Course(
                schedule_guid="g4", course_name="高等数学",
                class_name="智能科学与技术241班",
            )
        )
        self.db.add(
            Course(
                schedule_guid="g5", course_name="数字图像处理",
                class_name="数字媒体技术241班",
            )
        )
        self.db.commit()

        check = roster_check(self.db)
        self.assertEqual(check["matched_student_count"], check["students_total"])
        self.assertIn("✅ 缺勤反推可用：全部学生", format_check(check))

    def test_short_class_names_are_recognized_as_the_same_class(self) -> None:
        """回归锁定：教务写「智能241班」、考勤写「智能科学与技术241班」是同一个班。

        旧版本按字符串直接比，这两个永远对不上 —— 而后果不是报错，
        是**永远发现不了旷课学生**（最危险的那种静默失效）。
        ``class_key()`` 把它们都折成 ``(2024, 智能科学与技术, 1)``。
        """
        from app.services.roster import class_key

        self.assertEqual(
            class_key("智能241班"), class_key("智能科学与技术241班")
        )
        self.assertEqual(class_key("数媒241班"), class_key("数字媒体技术241班"))
        self.assertEqual(class_key("视传252班"), class_key("视觉传达设计252班"))
        # 「人工智能导论」里的"智能"不能被当成「智能科学与技术」
        self.assertNotEqual(class_key("人工智能241班"), class_key("智能科学与技术241班"))

    def test_truly_different_majors_do_not_match(self) -> None:
        """回归锁定：归一化不能把不同的班糊在一起 —— 那会造成误报。"""
        import_students(
            self.db,
            write_csv("rc2b.csv", "学号,姓名,班级\n2024010001,张三,智能241班\n"),
        )
        self.db.add(
            Course(
                schedule_guid="g2b",
                course_name="高等数学",
                class_name="数字媒体技术241班",
            )
        )
        self.db.commit()

        check = roster_check(self.db)
        self.assertFalse(check["can_infer"], "不同专业不应报告可用")
        self.assertIn("智能241班", check["only_in_roster"])
        self.assertIn("数字媒体技术241班", check["only_in_courses"])


class TestStudentLifecycle(_DbTestCase):
    """停用 / 删除学生。

    停用不是可有可无的功能：毕业生若不停用，其历史事件仍在库里，
    判定会一直认为他「有课且未签到」→ **永久告警**。
    """

    def setUp(self) -> None:
        super().setUp()
        import_students(self.db, write_csv("lc.csv", ROSTER_CSV))

    def _run_cli(self, argv: list[str]) -> int:
        from app.cli import main

        return main(argv)

    def test_deactivate_by_grade_hides_from_dashboard(self) -> None:
        from app.services.ingest import dashboard_rows

        before = {r["student_no"] for r in dashboard_rows(self.db)}
        self.assertIn("2024010001", before)

        self.assertEqual(self._run_cli(["deactivate", "--grade", "2024"]), 0)

        after = {r["student_no"] for r in dashboard_rows(self.db)}
        for no in ("2024010001", "2024020001"):
            self.assertNotIn(no, after, f"{no} 停用后不应再出现在页面/告警里")

        # 是停用而不是删除：名册记录仍在，active=False
        self.db.expire_all()
        s = self.db.get(Student, "2024010001")
        self.assertFalse(s.active)

    def test_deactivate_dry_run_does_not_change(self) -> None:
        self.assertEqual(self._run_cli(["deactivate", "--grade", "2024", "--dry-run"]), 0)
        self.db.expire_all()
        s = self.db.get(Student, "2024010001")
        self.assertTrue(s.active, "dry-run 不应真的停用")

    def test_deactivate_requires_a_selector(self) -> None:
        self.assertEqual(self._run_cli(["deactivate"]), 1)

    def test_delete_students_removes_related_rows(self) -> None:
        from app.models import Alert, AttendanceDaily

        # 用一个其它测试不会用到的日期，避免与共用库里的同名唯一键冲突
        unique_day = date(2019, 3, 4)
        self.db.query(AttendanceDaily).filter(
            AttendanceDaily.student_no == "2024010001"
        ).delete()
        self.db.query(AttendanceEvent).filter(
            AttendanceEvent.student_no == "2024010001"
        ).delete()
        self.db.commit()

        self.db.add(
            AttendanceEvent(
                source="hanboer", schedule_guid="lc-guid", student_no="2024010001",
                user_type=2, class_date=unique_day, attend_status="缺勤",
            )
        )
        self.db.add(AttendanceDaily(student_no="2024010001", class_date=unique_day))
        self.db.add(
            Alert(
                student_no="2024010001", kind="异常", state="异常",
                first_date=unique_day, last_date=unique_day,
            )
        )
        self.db.commit()

        code = self._run_cli(["delete-students", "--student-no", "2024010001"])
        self.assertEqual(code, 0)
        self.db.expire_all()

        self.assertIsNone(self.db.get(Student, "2024010001"))
        self.assertEqual(
            self.db.query(AttendanceEvent)
            .filter(AttendanceEvent.student_no == "2024010001")
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(AttendanceDaily)
            .filter(AttendanceDaily.student_no == "2024010001")
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(Alert).filter(Alert.student_no == "2024010001").count(), 0
        )
        # 其他学生不受影响
        self.assertIsNotNone(self.db.get(Student, "2024010002"))

    def test_delete_requires_explicit_student_no(self) -> None:
        self.assertEqual(self._run_cli(["delete-students"]), 1)

    def test_delete_unknown_student_reports(self) -> None:
        self.assertEqual(self._run_cli(["delete-students", "--student-no", "9999999999"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)



class TestPiiMasking(unittest.TestCase):
    """导入报告默认对个人信息打码。

    学生名册涉密，而导入失败时报告会打印跳过样例 —— 那是**原始整行**。
    老师把这段贴出来排错时，学号姓名就一起出去了。
    所以默认必须打码，且打码后仍要能定位问题。
    """

    def test_student_no_keeps_shape(self) -> None:
        from app.services.roster import mask_pii

        self.assertEqual(mask_pii("202400000001N"), "2023******06N")
        self.assertEqual(mask_pii("202400000001"), "2023******06")

    def test_name_keeps_surname_only(self) -> None:
        from app.services.roster import mask_pii

        self.assertEqual(mask_pii("张笑颜"), "张**")
        self.assertEqual(mask_pii("王毅"), "王*")

    def test_empty_and_short(self) -> None:
        from app.services.roster import mask_pii

        self.assertEqual(mask_pii(""), "")
        self.assertEqual(mask_pii("x"), "x*")

    def test_no_pii_survives_in_masked_form(self) -> None:
        """打码结果里不能再出现完整的学号或姓名。"""
        from app.services.roster import mask_pii

        self.assertNotIn("013006", mask_pii("202400000001N"))
        for name in ("张笑颜", "示例用户", "王泺轩"):
            masked = mask_pii(name)
            self.assertNotEqual(masked, name)
            for ch in name[1:]:
                self.assertNotIn(ch, masked)

    def test_report_masks_by_default_and_show_raw_opens_it(self) -> None:
        from app.services.roster import ImportReport

        row = ["202400000001", "测试甲", "智能241科学与技术", "测试241班"]
        masked = ImportReport(kind="students")._sample(row, "缺学号：")
        self.assertIn("2023******01", masked)
        self.assertNotIn("202400000001", masked)
        self.assertNotIn("测试甲", masked)

        raw = ImportReport(kind="students", show_raw=True)._sample(row)
        self.assertIn("202400000001", raw)
