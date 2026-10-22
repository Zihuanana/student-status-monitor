"""名册/课表导入。

为什么这件事很关键
------------------
判定「整日无签到」需要知道**该生当天是否有课**；而真实考勤接口通常只返回
**有考勤动作**的人 —— 整节课没来的学生查无此人。`app/services/infer.py` 用
「班级应到名册 − 已观测到的学生」反推缺勤，是发现这类学生的**唯一手段**。

它的前提是：``Student.class_name`` 与 ``Course.class_name`` **能对上**。
这两边一个来自学校名册、一个来自考勤系统，写法经常不一致
（"智能科学与技术241班" vs "智能241" vs "24智能1班"）。对不上就静默失效 ——
不报错、页面也正常，只是永远发现不了旷课学生。

因此导入后必须做**关联校验**（:func:`roster_check`），把对不上的班级列出来。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.logging_conf import get_logger
from app.core.ids import normalize_student_no
from app.models import Course, MajorAlias, Student
from app.services.scope import describe_student_scope, student_id_in_scope
from app.services.spreadsheet import cell, detect_columns, find_header_row, iter_data_rows, read_table

log = get_logger("service.roster")


def mask_pii(value: str) -> str:
    """把可能含个人信息的值打码，同时保留足够排错的"形状"。

    - 学号 ``202400000001N`` → ``2023******6N``（保留年份与尾号，能看出是哪个年级）
    - 姓名 ``张笑颜`` → ``张**``（保留姓氏，够定位到人又不暴露全名）
    - 数字/短串同理

    为什么默认要打码：导入失败时报告会打印"跳过样例"，那是**原始整行**。
    老师把这段输出贴出来排错时，学号姓名就一起泄露了。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 2:
        return v[0] + "*"
    # 学号形态：数字，或"数字+单个字母后缀"
    body = v[:-1] if (len(v) > 1 and v[-1].isalpha() and v[:-1].isdigit()) else v
    if body.isdigit() and len(body) >= 6:
        tail = v[len(body):]
        return v[:4] + "*" * (len(body) - 6) + body[-2:] + tail
    # 其它（姓名、班级名等）：保留首字
    return v[0] + "*" * (len(v) - 1)


@dataclass
class ImportReport:
    """导入结果，供 CLI 打印与测试断言。"""

    kind: str
    source: str = ""
    encoding: str = ""
    header_row: int = -1
    columns: dict[str, int] = field(default_factory=dict)
    total_rows: int = 0
    imported: int = 0
    skipped: int = 0
    #: 其中因「学号不是本院形态」被挡掉的行数（学号作用域，见 app/services/scope.py）
    out_of_scope: int = 0
    skipped_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 为 True 时 skipped_samples 里是**原始内容**（默认打码，见 mask_pii）
    show_raw: bool = False

    def _sample(self, row: list[str], prefix: str = "") -> str:
        """一行"跳过样例"。默认打码，只有显式要求时才给原文。"""
        cells = row[:6] if self.show_raw else [mask_pii(c) for c in row[:6]]
        return prefix + "|".join(cells)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "encoding": self.encoding,
            "header_row": self.header_row,
            "columns": sorted(self.columns.keys()),
            "total_rows": self.total_rows,
            "imported": self.imported,
            "skipped": self.skipped,
            "out_of_scope": self.out_of_scope,
            "skipped_samples": self.skipped_samples,
            "warnings": self.warnings,
        }


def _grade_from(student_no: str, explicit: str, year_slice: str = "0:4") -> str:
    """年级优先取表格里的列；没有就从学号切片推。"""
    if explicit.strip():
        return explicit.strip()
    try:
        a, _, b = year_slice.partition(":")
        return student_no[int(a or 0) : int(b) if b else 4]
    except (ValueError, IndexError):
        return ""


def _major_code_from(student_no: str, explicit: str, major_slice: str = "4:6") -> str:
    if explicit.strip():
        return explicit.strip()
    try:
        a, _, b = major_slice.partition(":")
        return student_no[int(a or 0) : int(b) if b else 6]
    except (ValueError, IndexError):
        return ""


# ─────────────────────────────────────────────────────────────
# 学生名册
# ─────────────────────────────────────────────────────────────
def import_students(
    db: Session, path: Path, dry_run: bool = False, show_raw: bool = False
) -> ImportReport:
    """导入学生名册。必须能拿到「学号 + 班级」，否则反推缺勤无法工作。

    ``show_raw`` 只影响**报告里的样例是否打码**（默认打码），
    不影响实际写入的数据 —— 库里的学号姓名当然是完整的。
    """
    from app.services.ingest import upsert_students

    rows, encoding = read_table(path)
    report = ImportReport(
        kind="students", source=str(path), encoding=encoding, show_raw=show_raw
    )
    if not rows:
        report.warnings.append("文件为空")
        return report

    header_row = find_header_row(rows)
    report.header_row = header_row
    mapping = detect_columns(rows[header_row])
    report.columns = mapping

    if "student_no" not in mapping:
        report.warnings.append(
            "没找到「学号」列 —— 请检查表头（支持：学号/学生学号/编号）"
        )
        return report
    if "class_name" not in mapping:
        report.warnings.append(
            "没找到「班级」列！没有班级就无法反推「整节课没来」的学生，"
            "系统将只能看到接口返回过的人。强烈建议补充该列。"
        )
    if "name" not in mapping:
        report.warnings.append("没找到「姓名」列，将只导入学号")

    payload: list[dict] = []
    seen: set[str] = set()
    #: 被学号作用域挡掉的行数（与"缺学号/重复"分开统计 —— 老师要能分辨
    #: "这份名册里混了别院的人"和"这份名册格式有问题"）
    out_of_scope = 0
    for row in iter_data_rows(rows):
        report.total_rows += 1
        student_no = cell(row, mapping, "student_no")
        if not student_no:
            report.skipped += 1
            if len(report.skipped_samples) < 5:
                report.skipped_samples.append(report._sample(row, "缺学号："))
            continue
        if student_no in seen:
            report.skipped += 1
            if len(report.skipped_samples) < 5:
                shown = student_no if report.show_raw else mask_pii(student_no)
                report.skipped_samples.append(f"重复学号：{shown}")
            continue
        seen.add(student_no)

        # 学号作用域：只要本院（海南国际学院）的学生。名册里混进别院的人
        # 是最常见的污染源，必须在写库前就挡掉。
        if not student_id_in_scope(student_no):
            out_of_scope += 1
            if len(report.skipped_samples) < 5:
                shown = student_no if report.show_raw else mask_pii(student_no)
                report.skipped_samples.append(f"非本院学号：{shown}")
            continue

        grade = _grade_from(student_no, cell(row, mapping, "grade"))
        major_code = _major_code_from(student_no, cell(row, mapping, "major_code"))
        payload.append(
            {
                "student_no": student_no,
                "name": cell(row, mapping, "name"),
                "grade": grade,
                "major_code": major_code,
                "major_name": cell(row, mapping, "major_name"),
                "class_name": cell(row, mapping, "class_name"),
                "org_name": cell(row, mapping, "org_name"),
                "enroll_year": int(grade) if grade.isdigit() and len(grade) == 4 else None,
            }
        )

    report.imported = len(payload)
    report.out_of_scope = out_of_scope
    if out_of_scope:
        report.warnings.append(
            f"有 {out_of_scope} 行学号不是本院形态，已跳过（规则：{describe_student_scope()}）。"
            "若这里面确实有本院学生，请核对 HB_STUDENT_ID_PATTERN。"
        )
    if dry_run:
        return report

    # 分批写入，避免超大文件一次性占用内存。
    # source_kind="roster"：名册里的姓名是**权威值**，可以覆盖考勤接口带回来的
    # 账号昵称（见 Student.name_source 的说明）。
    for i in range(0, len(payload), 500):
        upsert_students(db, payload[i : i + 500], source_kind="roster")
    db.commit()
    log.info("导入学生 %d 名（跳过 %d 行）", report.imported, report.skipped)
    return report


# ─────────────────────────────────────────────────────────────
# 课表
# ─────────────────────────────────────────────────────────────
_WEEKDAY_MAP = {
    "1": 0, "一": 0, "周一": 0, "星期一": 0, "monday": 0, "mon": 0,
    "2": 1, "二": 1, "周二": 1, "星期二": 1, "tuesday": 1, "tue": 1,
    "3": 2, "三": 2, "周三": 2, "星期三": 2, "wednesday": 2, "wed": 2,
    "4": 3, "四": 3, "周四": 3, "星期四": 3, "thursday": 3, "thu": 3,
    "5": 4, "五": 4, "周五": 4, "星期五": 4, "friday": 4, "fri": 4,
    "6": 5, "六": 5, "周六": 5, "星期六": 5, "saturday": 5, "sat": 5,
    "7": 6, "日": 6, "天": 6, "周日": 6, "周天": 6, "星期日": 6, "sunday": 6, "sun": 6,
}


def parse_weekday(raw: str) -> int | None:
    key = str(raw or "").strip().lower().replace("星期", "周")
    if not key:
        return None
    if key in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[key]
    # 形如 "周1"、"星期3"
    digits = "".join(ch for ch in key if ch.isdigit())
    if digits and digits in _WEEKDAY_MAP:
        return _WEEKDAY_MAP[digits]
    for token, value in _WEEKDAY_MAP.items():
        if token in key:
            return value
    return None


def import_enrollments(
    db: Session, path: Path, dry_run: bool = False, show_raw: bool = False
) -> ImportReport:
    """导入**教务处选课名单**（学生级：谁选了哪门课）。

    与 :func:`import_schedule` 的区别
    --------------------------------
    ``import_schedule`` 要的是"课表"（有星期、节次），用来回答
    **某天有没有课**；而教务处导出的是"选课名单"（学生 × 课程），
    它回答的是**谁该上这门课**。两者都建 ``StudentCourse``，
    但名单没有星期节次，因此名单里的课**不能**参与"某天缺勤"的判定。

    本函数做三件事：

    1. **权威名册覆盖**：名单里的姓名会覆盖库内姓名（有人在学校改过名，
       采集接口带回来的可能是旧名或昵称）；
    2. **建课**：名单里有、库里没有的课建成 ``covered=False`` 的课程行
       —— 教务有这门课，但翰博尔（录播系统）看不到它的出勤。
       这类课在页面上**置灰**，且不参与异常判定与缺勤反推。
    3. **建应到关系**：按课程名把学生挂到课上。库里已有同名课时优先复用
       —— 翰博尔的课是有真实课次计划的，挂上去才能真正判断"哪天有课"。

    ⚠ 名单能提供"谁选了课"，但**不能**回答"哪天上课"。要让这些人多出来的
    课真正进入每日判定，还需要一份带**上课时间**的课表
    （``import schedule``）。在那之前，本函数只让课程可见。
    """
    from app.adapters.base import NormalizedCourse
    from app.models import StudentCourse
    from app.services.ingest import upsert_courses, upsert_students

    rows, encoding = read_table(path)
    report = ImportReport(
        kind="enrollments", source=str(path), encoding=encoding, show_raw=show_raw
    )
    if not rows:
        report.warnings.append("文件为空")
        return report

    header_row = find_header_row(rows)
    report.header_row = header_row
    mapping = detect_columns(rows[header_row])
    report.columns = mapping

    if "student_no" not in mapping:
        report.warnings.append("没找到「学号」列 —— 请检查表头")
        return report
    if "course_name" not in mapping:
        report.warnings.append("没找到「课程名」列 —— 这份表不是选课名单？")
        return report

    # ── 1. 解析：学生（取权威姓名/班级）+ 选课对 ──────────────
    students: dict[str, dict] = {}
    pairs: list[tuple[str, str, str]] = []  # (学号, 课程名, 课程号)
    out_of_scope = 0
    for row in iter_data_rows(rows):
        report.total_rows += 1
        raw_no = cell(row, mapping, "student_no")
        course_name = cell(row, mapping, "course_name")
        if not raw_no or not course_name:
            report.skipped += 1
            if len(report.skipped_samples) < 5:
                report.skipped_samples.append(report._sample(row))
            continue
        no = normalize_student_no(raw_no)
        if not no:
            report.skipped += 1
            continue
        if not student_id_in_scope(raw_no):
            out_of_scope += 1
            if len(report.skipped_samples) < 5:
                shown = raw_no if report.show_raw else mask_pii(raw_no)
                report.skipped_samples.append(f"非本院学号：{shown}")
            continue

        if no not in students:
            grade = _grade_from(no, cell(row, mapping, "grade"))
            students[no] = {
                "student_no": no,
                "name": cell(row, mapping, "name"),
                "grade": grade,
                "major_code": _major_code_from(no, cell(row, mapping, "major_code")),
                "major_name": cell(row, mapping, "major_name"),
                "class_name": cell(row, mapping, "class_name"),
                "org_name": cell(row, mapping, "org_name"),
                "enroll_year": int(grade) if grade.isdigit() and len(grade) == 4 else None,
            }
        pairs.append((no, course_name, cell(row, mapping, "course_no")))

    report.imported = len(pairs)
    report.out_of_scope = out_of_scope
    if out_of_scope:
        report.warnings.append(
            f"有 {out_of_scope} 行学号不是本院形态，已跳过"
            f"（规则：{describe_student_scope()}）"
        )
    if not students:
        report.warnings.append("没有解析出任何学生")
        return report
    if dry_run:
        report.warnings.append(
            f"将导入 {len(students)} 名学生、{len(pairs)} 条选课关系"
        )
        return report

    # ── 2. 名册：库里已有的先比对姓名，改过名的要能看见 ───────
    existing_rows = {
        r[0]: r[1]
        for r in db.execute(
            select(Student.student_no, Student.name).where(
                Student.student_no.in_(list(students))
            )
        ).all()
    }
    renamed = [
        (no, existing_rows[no], students[no]["name"])
        for no in students
        if no in existing_rows
        and (students[no]["name"] or "").strip()
        and (existing_rows[no] or "").strip() != (students[no]["name"] or "").strip()
    ]
    new_students = [no for no in students if no not in existing_rows]
    upsert_students(db, list(students.values()), source_kind="roster")

    # ── 3. 建课：库里有同名课就复用，没有的建成"看不到考勤"的课 ──
    existing_courses: dict[str, list[tuple[str, bool]]] = {}
    for guid, name, covered, cdate in db.execute(
        select(Course.schedule_guid, Course.course_name, Course.covered, Course.class_date)
    ).all():
        if name:
            existing_courses.setdefault(name.strip(), []).append((guid, bool(covered)))

    by_name: dict[str, str] = {}
    created: list[dict] = []
    for _no, course_name, course_no in pairs:
        key = course_name.strip()
        if key in by_name:
            continue
        found = existing_courses.get(key)
        if found:
            # 优先挑有考勤源的：那是有真实课次计划的课
            found.sort(key=lambda item: (not item[1], item[0]))
            by_name[key] = found[0][0]
            continue
        # 库里没有 → 建一门"教务有、考勤看不到"的课
        seg = (course_no or "").strip() or "x"
        guid = f"roster-{seg}-{abs(hash(key)) & 0xFFFFFFFF:08x}"
        by_name[key] = guid
        created.append(
            {
                "schedule_guid": guid,
                "course_name": key,
                "title": key,
                "class_name": "",
                "teacher_name": "",
                "classroom_name": "",
                "classroom_no": "",
                "node_name": "",
                "week_name": "",
                "covered": False,
            }
        )

    if created:
        for i in range(0, len(created), 500):
            batch = created[i : i + 500]
            upsert_courses(
                db,
                [
                    NormalizedCourse(
                        schedule_guid=c["schedule_guid"],
                        source="roster",
                        title=c["title"],
                        course_name=c["course_name"],
                        covered=False,  # 关键：教务名单的课没有考勤源
                    )
                    for c in batch
                ],
            )
        db.commit()

    # ── 4. 应到关系（名单里的课大多没有课次日期，class_date 留空）──
    linked = _link_enrollments(db, pairs, by_name)
    db.commit()

    if new_students:
        report.warnings.append(f"新增 {len(new_students)} 名学生（名单里有、库里没有）")
    if renamed:
        head = "、".join(f"{no}：「{old}」→「{new}」" for no, old, new in renamed[:6])
        more = f"…（共 {len(renamed)} 人）" if len(renamed) > 6 else ""
        report.warnings.append(f"按名单更正了 {len(renamed)} 个姓名：{head}{more}")
    covered_used = sum(
        1
        for name, guid in by_name.items()
        if any(guid == g and cov for g, cov in existing_courses.get(name, []))
    )
    report.warnings.append(
        "课程：新增 %d 门（教务有、翰博尔看不到，页面置灰、不参与异常判定），"
        "复用已有 %d 门（其中 %d 门有考勤源）；建立应到关系 %d 条"
        % (len(created), len(by_name) - len(created), covered_used, linked)
    )
    log.info(
        "选课名单导入：学生 %d，新增课程 %d，复用课程 %d，应到关系 %d",
        len(students), len(created), len(by_name) - len(created), linked,
    )
    return report


def _link_enrollments(
    db: Session, pairs: list[tuple[str, str, str]], by_name: dict[str, str]
) -> int:
    """建立「学生 → 课程」应到关系。返回新增条数。"""
    from app.models import StudentCourse

    dates = {
        guid: cdate
        for guid, cdate in db.execute(
            select(Course.schedule_guid, Course.class_date).where(
                Course.schedule_guid.in_(list(set(by_name.values())) or ["-"])
            )
        ).all()
    }
    existing = {
        (r[0], r[1])
        for r in db.execute(
            select(StudentCourse.student_no, StudentCourse.schedule_guid).where(
                StudentCourse.schedule_guid.in_(list(set(by_name.values())) or ["-"])
            )
        ).all()
    }
    added = 0
    seen: set[tuple[str, str]] = set()
    for no, course_name, _course_no in pairs:
        guid = by_name.get(course_name.strip())
        if not guid or (no, guid) in existing or (no, guid) in seen:
            continue
        seen.add((no, guid))
        db.add(
            StudentCourse(
                student_no=no,
                schedule_guid=guid,
                class_date=dates.get(guid),
                source="roster",
            )
        )
        added += 1
    db.flush()
    return added


def repair_names(db: Session, path: Path, dry_run: bool = False) -> dict:
    """**只按名单修正姓名**（不动选课关系、不动班级）。

    为什么单独一个入口：采集侧会把翰博尔账号昵称写进姓名（见
    ``Student.name_source``）。修好写入规则之后，库里那些已经被写坏的名字
    还得按名单刷一遍 —— 而重新完整导入一次选课名单会顺带重挂几万条关系，
    没必要。这个函数只做一件事：拿名单里的姓名覆盖 ``students.name``。
    """
    from app.core.ids import normalize_student_no
    from app.models import Student

    rows, encoding = read_table(path)
    if not rows:
        return {"students": 0, "fixed": 0, "renamed": [], "encoding": encoding}

    header_row = find_header_row(rows)
    mapping = detect_columns(rows[header_row])
    if "student_no" not in mapping or "name" not in mapping:
        raise ValueError("名单里需要「学号」和「姓名」两列")

    wanted: dict[str, str] = {}
    for row in iter_data_rows(rows):
        no = normalize_student_no(cell(row, mapping, "student_no"))
        nm = cell(row, mapping, "name")
        if no and nm:
            wanted.setdefault(no, nm)

    existing = {
        s.student_no: s.name
        for s in db.execute(
            select(Student).where(Student.student_no.in_(list(wanted)))
        ).scalars()
    }
    renamed = [
        (no, existing[no], wanted[no])
        for no in sorted(wanted)
        if no in existing and (existing[no] or "").strip() != wanted[no].strip()
    ]
    if dry_run:
        return {"students": len(existing), "fixed": len(renamed),
                "renamed": renamed, "encoding": encoding}

    # 1) 名字不一致的：按名单改回来
    for no, _old, new in renamed:
        stu = db.get(Student, no)
        if stu is None:
            continue
        stu.name = new
        stu.name_source = "roster"   # 标记为权威来源，采集侧不再覆盖

    # 2) 名字本来就一致的：也要打上「名册来源」标记。
    #    否则它们的来源还是 attendance，下一轮采集又会用账号昵称覆盖 ——
    #    今天没坏不等于明天不坏（学生随时可以改自己的账号名）。
    same = [no for no in wanted if no in existing and no not in {r[0] for r in renamed}]
    for i in range(0, len(same), 500):
        db.execute(
            update(Student)
            .where(Student.student_no.in_(same[i : i + 500]))
            .values(name_source="roster")
        )
    db.commit()
    log.warning(
        "按名单修正 %d 个姓名，并把 %d 人标记为「名册来源」（采集不再覆盖）",
        len(renamed), len(same),
    )
    return {"students": len(existing), "fixed": len(renamed),
            "renamed": renamed, "marked": len(same), "encoding": encoding}


def import_schedule(
    db: Session, path: Path, dry_run: bool = False, show_raw: bool = False
) -> ImportReport:
    """导入课表，建立「班级 → 课程」与「学生 → 课程」应到关系。

    课表用于回答「某班在某天某节有什么课」，进而：
      1. 采集时知道该去抓哪些课；
      2. 结合学生名册，得出「应到名单」，从而反推缺勤。
    """
    from app.models import StudentCourse
    from app.services.ingest import upsert_courses

    rows, encoding = read_table(path)
    report = ImportReport(
        kind="schedule", source=str(path), encoding=encoding, show_raw=show_raw
    )
    if not rows:
        report.warnings.append("文件为空")
        return report

    header_row = find_header_row(rows)
    report.header_row = header_row
    mapping = detect_columns(rows[header_row])
    report.columns = mapping

    if "course_name" not in mapping:
        report.warnings.append("没找到「课程」列")
        return report
    if "class_name" not in mapping:
        report.warnings.append("没找到「班级」列，无法建立应到关系")
        return report

    # 班级 → 学生
    rosters: dict[str, list[str]] = {}
    for student_no, class_name in db.execute(
        select(Student.student_no, Student.class_name).where(
            Student.active.is_(True), Student.class_name != ""
        )
    ).all():
        rosters.setdefault(class_name, []).append(student_no)

    courses: list[dict] = []
    links: list[tuple[str, str, str]] = []  # (class_name, guid, course_name)
    for row in iter_data_rows(rows):
        report.total_rows += 1
        class_name = cell(row, mapping, "class_name")
        course_name = cell(row, mapping, "course_name")
        if not class_name or not course_name:
            report.skipped += 1
            if len(report.skipped_samples) < 5:
                report.skipped_samples.append(report._sample(row))
            continue

        weekday = parse_weekday(cell(row, mapping, "weekday"))
        guid = f"tbl-{abs(hash((class_name, course_name, cell(row, mapping, 'node_name'), cell(row, mapping, 'start_time')))) & 0xFFFFFFFF:08x}"
        courses.append(
            {
                "schedule_guid": guid,
                "course_name": course_name,
                "class_name": class_name,
                "teacher_name": cell(row, mapping, "teacher_name"),
                "classroom_name": cell(row, mapping, "classroom"),
                "classroom_no": cell(row, mapping, "classroom"),
                "node_name": cell(row, mapping, "node_name"),
                "week_name": cell(row, mapping, "weeks"),
                "_weekday": weekday,
            }
        )
        links.append((class_name, guid, course_name))
        report.imported += 1

    if dry_run:
        return report

    # 课程落库（时间留空，等考勤接口按 scheduleGuid 回填真实起止时间）
    from app.adapters.base import NormalizedCourse

    for i in range(0, len(courses), 500):
        batch = courses[i : i + 500]
        upsert_courses(
            db,
            [
                NormalizedCourse(
                    schedule_guid=c["schedule_guid"],
                    source="schedule",
                    course_name=c["course_name"],
                    class_name=c["class_name"],
                    teacher_name=c["teacher_name"],
                    classroom_name=c["classroom_name"],
                    classroom_no=c["classroom_no"],
                    node_name=c["node_name"],
                    week_name=c["week_name"],
                )
                for c in batch
            ],
        )

    # 建立「学生 → 课程」应到关系
    existing = {
        (r[0], r[1])
        for r in db.execute(select(StudentCourse.student_no, StudentCourse.schedule_guid)).all()
    }
    added = 0
    for class_name, guid, _course in links:
        for student_no in rosters.get(class_name, []):
            if (student_no, guid) in existing:
                continue
            db.add(StudentCourse(student_no=student_no, schedule_guid=guid, source="schedule"))
            existing.add((student_no, guid))
            added += 1
    db.commit()

    if not rosters:
        report.warnings.append(
            "库内还没有学生名册（或名册缺班级），因此只导入了课程、未建立应到关系。"
            "请先导入学生名册再执行本命令，缺勤反推才能生效。"
        )
    else:
        unmatched = {c for c, _g, _n in links if c not in rosters}
        if unmatched:
            report.warnings.append(
                "以下课表班级在名册里找不到对应学生，应到关系为空："
                + "、".join(sorted(unmatched)[:8])
                + ("…" if len(unmatched) > 8 else "")
            )
    log.info("导入课程 %d 条，建立应到关系 %d 条", report.imported, added)
    return report


# ─────────────────────────────────────────────────────────────
# 班级名归一化（关联校验与缺勤反推的命门）
# ─────────────────────────────────────────────────────────────
# 同一个班，两边的写法天差地别：
#   教务名册：2024数字媒体技术（中外合作办学）1班
#   翰博尔  ：数字媒体技术241班
# 直接比字符串是 0 命中，而后果不是报错，是**永远发现不了旷课学生**。
# 这里把两边都折成「(年份, 专业核心名, 班序号)」再比。
_MAJOR_CORE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 顺序有意义：先长后短，且短名要排除更长的同前缀词
    # （"人工智能导论"里的"智能"不是"智能科学与技术"）。
    (re.compile("智能科学与技术"), "智能科学与技术"),
    (re.compile("数字媒体技术"), "数字媒体技术"),
    (re.compile("视觉传达设计"), "视觉传达设计"),
    (re.compile("数媒"), "数字媒体技术"),
    (re.compile("视传"), "视觉传达设计"),
    # "智能"是本院智科的常用简写，但必须排除"人工智能"（那是**另一门课**
    # "人工智能导论"里的词，不是专业名）与"智能产品设计"。
    (re.compile(r"(?<!人工)智能(?!产品|制造|科学|装备)"), "智能科学与技术"),
)
_NOISE_WORDS = (
    "中外合作办学", "海南中外合作办学", "中外合作", "国际合作", "海南",
    "学术", "普通班", "本科",
)
_YEAR_PREFIX = re.compile(r"^(20\d{2})\s*级?")
_YEAR_SUFFIX = re.compile(r"(20\d{2})\s*级")
_TAIL_CLASS = re.compile(r"(\d{1,2})\s*班\s*$")
#: 翰博尔写法：``数字媒体技术241班`` = 专业 + 2 位年 + 1 位班号。
#: 必须**先**于「取末尾数字」处理，否则会把 ``241`` 整个当成班号 41。
_SHORT_FORM = re.compile(r"(?<!\d)(\d{2})(\d)\s*班\s*$")


def class_key(raw: str) -> tuple[str, str, str] | None:
    """把一个班级名折成可比的键：(年份, 专业, 班序号)。

    >>> class_key("2024数字媒体技术（中外合作办学）1班")
    ('2024', '数字媒体技术', '1')
    >>> class_key("数字媒体技术241班")
    ('2024', '数字媒体技术', '1')

    折不出来（比如翰博尔的 ``公选课(01S01...)``）就返回 ``None``，
    调用方按"没对上"处理 —— 绝不能因为折不出来就误配到别的班。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    year = ""
    for pattern in (_YEAR_PREFIX, _YEAR_SUFFIX):
        m = pattern.search(text)
        if m:
            year = m.group(1)
            text = text.replace(m.group(0), "")
            break

    major = ""
    for pattern, canonical in _MAJOR_CORE_PATTERNS:
        if pattern.search(text):
            major = canonical
            break

    seq = ""
    m = _SHORT_FORM.search(text)
    if m:
        # 「数字媒体技术241班」：241 = 24 年 + 1 班
        if not year:
            year = "20" + m.group(1)
        seq = str(int(m.group(2)))
    else:
        m = _TAIL_CLASS.search(text)
        if m:
            seq = str(int(m.group(1)))
        else:
            # 「智能241」这种没有"班"字的写法
            stripped = re.sub(r"(20\d{2})", "", text)
            m = re.search(r"(\d{1,2})\s*$", stripped.strip())
            if m:
                seq = str(int(m.group(1)))
                if not year:
                    y = re.search(r"(20\d{2})", text)
                    if y:
                        year = y.group(1)

    if not major:
        return None
    if not year:
        year = ""
    if not seq:
        return None
    return (year, major, seq)


def class_alias_map(db: Session) -> dict[str, str]:
    """人工维护的班级别名（``班级别名表``），键为归一化后的写法。

    归一化已经能覆盖实测的写法差异；别名表是留给**折不出来**的那些
    （例如教务处某个班根本不带专业名）的兜底，避免只能改代码。
    """
    from app.models import ClassAlias

    out: dict[str, str] = {}
    for alias, canonical in db.execute(
        select(ClassAlias.alias, ClassAlias.canonical)
    ).all():
        a, c = class_key(alias), class_key(canonical)
        if a and c:
            out[str(a)] = str(c)
    return out


def _class_lookup(
    student_classes: dict[str, int],
    course_classes: dict[str, int],
    aliases: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """返回两边「原班级名 → 归一化键」的映射（只保留折得出来的）。"""
    aliases = aliases or {}
    stu: dict[str, str] = {}
    for name in student_classes:
        key = class_key(name)
        if key is None:
            continue
        text = str(key)
        stu[name] = aliases.get(text, text)
    cou: dict[str, str] = {}
    for name in course_classes:
        key = class_key(name)
        if key is None:
            continue
        text = str(key)
        cou[name] = aliases.get(text, text)
    return stu, cou


# ─────────────────────────────────────────────────────────────
# 关联校验（最关键的一步）
# ─────────────────────────────────────────────────────────────
def roster_check(db: Session) -> dict:
    """检查「学生名册」与「考勤课程」的班级能否对上。

    对不上时系统不会报错，只会**永远发现不了旷课学生** —— 所以必须显式检查。

    比较用的是 :func:`class_key` 归一化之后的键，不是字符串直接相等：
    教务写「2024数字媒体技术（中外合作办学）1班」、翰博尔写「数字媒体技术241班」，
    字符串比是 0 命中，归一化后是同一个班。
    """
    student_classes = {
        r[0]: r[1]
        for r in db.execute(
            select(Student.class_name, func.count())
            .where(Student.active.is_(True), Student.class_name != "")
            .group_by(Student.class_name)
        ).all()
    }
    course_classes = {
        r[0]: r[1]
        for r in db.execute(
            select(Course.class_name, func.count())
            .where(Course.class_name.is_not(None), Course.class_name != "")
            .group_by(Course.class_name)
        ).all()
    }

    total_students = db.execute(
        select(func.count()).select_from(Student).where(Student.active.is_( True))
    ).scalar_one()
    with_class = sum(student_classes.values())

    aliases = class_alias_map(db)
    stu_keys, cou_keys = _class_lookup(student_classes, course_classes, aliases)
    matched_keys = set(stu_keys.values()) & set(cou_keys.values())
    matched = sorted(
        name for name, key in stu_keys.items() if key in matched_keys
    )
    matched_count = sum(student_classes[name] for name in matched)

    # ── 更重要的那条路：靠「学生 → 课程」关系反推（选课名单导入建立）──
    # 本项目实测翰博尔返回的 class_name 全是「公选课(2026...)」占位串，
    # 与名册的行政班名毫无关系，所以班级名匹配恒为 0。真正能用的应到来源
    # 是选课名单建立的 StudentCourse 关系 —— 校验必须报这个数字，
    # 否则会一直显示"完全不可用"，让人误以为名单白导了。
    from app.models import StudentCourse

    linked_students = db.execute(
        select(func.count(func.distinct(StudentCourse.student_no)))
        .select_from(StudentCourse)
        .join(Course, Course.schedule_guid == StudentCourse.schedule_guid)
        .where(Course.covered.is_(True))
    ).scalar_one()
    covered_courses = db.execute(
        select(func.count(func.distinct(Course.course_name))).where(Course.covered.is_(True))
    ).scalar_one()
    total_courses = db.execute(
        select(func.count(func.distinct(Course.course_name)))
    ).scalar_one()

    return {
        "students_total": int(total_students),
        "students_with_class": int(with_class),
        "student_classes": len(student_classes),
        "course_classes": len(course_classes),
        "matched_classes": matched,
        "matched_student_count": int(matched_count),
        "only_in_roster": sorted(
            name for name, key in stu_keys.items() if key not in matched_keys
        ),
        "only_in_courses": sorted(
            name for name, key in cou_keys.items() if key not in matched_keys
        ),
        #: 折不出班级键的名字（这些既不报"对不上"，也不该被当成匹配）
        "unparsed_roster": sorted(set(student_classes) - set(stu_keys)),
        "unparsed_courses": sorted(set(course_classes) - set(cou_keys)),
        #: 靠「学生 → 课程」关系能反推缺勤的人数（真正可用的覆盖）
        "infer_from_links": int(linked_students),
        "covered_courses": int(covered_courses),
        "all_courses": int(total_courses),
        "can_infer": bool(matched) or bool(linked_students),
    }


def format_check(report: dict) -> str:
    total = report["students_total"] or 0
    covered = report["matched_student_count"]
    by_class = len(report["matched_classes"])
    by_link = report.get("infer_from_links", 0)
    covered_courses = report.get("covered_courses", 0)
    all_courses = report.get("all_courses", 0)

    lines = [
        "── 名册 / 课表关联校验 ──",
        f"在读学生：{total}（其中有班级：{report['students_with_class']}）",
        f"名册班级数：{report['student_classes']}　课程班级数：{report['course_classes']}",
        f"按班级名对上的班：{by_class} 个，覆盖 {covered} 人",
        f"按选课关系能反推的学生：{by_link} 人"
        f"（这是本项目真正可靠的那条路：能看到从没来过的学生）",
        f"课程：有考勤源 {covered_courses} / 共 {all_courses} 门"
        f"（没有考勤源的课页面置灰，不参与异常判定）",
    ]

    if report["only_in_roster"]:
        lines.append(
            "⚠ 只在名册里、课程里没有的班级："
            + "、".join(report["only_in_roster"][:10])
            + ("…" if len(report["only_in_roster"]) > 10 else "")
        )
    if report["only_in_courses"]:
        lines.append(
            "⚠ 只在课程里、名册里没有的班级："
            + "、".join(report["only_in_courses"][:10])
            + ("…" if len(report["only_in_courses"]) > 10 else "")
        )

    # 结论只给一条，且以"能覆盖多少人"为准。两条路（班级名 / 选课关系）
    # 只要有一条覆盖到就算数，所以取两者覆盖人数的较大值而不是相加
    # —— 同一个学生可能两边都覆盖得到，相加会虚报。
    if total == 0:
        lines.append("❌ 还没有学生名册，请先执行 import students。")
    elif not report["can_infer"]:
        lines.append(
            "❌ 缺勤反推完全不可用：既没有班级名对应，也没有选课关系，"
            "系统只能看到考勤接口返回过的人，「整节课没来」的学生不会被发现。"
            "请先 `python -m app.cli import enrollments 选课名单.xlsx`。"
        )
    elif max(covered, by_link) >= total:
        lines.append("✅ 缺勤反推可用：全部学生整节课没来也会被发现。")
    else:
        lines.append(
            f"⚠ 缺勤反推部分可用：班级名覆盖 {covered} 人、选课关系覆盖 {by_link} 人"
            f"（共 {total} 人）。两者都没覆盖到的学生，整节课没来仍不会被发现。"
        )
    return "\n".join(lines)


def dump_report(report: ImportReport) -> str:
    d = report.as_dict()
    lines = [
        f"── 导入结果（{d['kind']}）──",
        f"文件：{d['source']}　解析：{d['encoding']}",
        f"表头行：第 {d['header_row'] + 1} 行　识别到的列：{'、'.join(d['columns'])}",
        f"数据行：{d['total_rows']}　成功：{d['imported']}　跳过：{d['skipped']}",
    ]
    if d.get("out_of_scope"):
        lines.append(
            f"其中非本院学号被挡：{d['out_of_scope']} 行"
            f"（规则：{describe_student_scope()}）"
        )
    if d["skipped_samples"]:
        lines.append("跳过样例：" + "；".join(d["skipped_samples"]))
    for w in d["warnings"]:
        lines.append("⚠ " + w)
    return "\n".join(lines)


_ = (date, datetime, json, MajorAlias, Path)
