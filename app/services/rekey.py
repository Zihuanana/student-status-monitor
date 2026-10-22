# -*- coding: utf-8 -*-
"""学号规范化迁移：把库里已经带前缀的学号改掉。

    python -m app.cli rekey-students --dry-run
    python -m app.cli rekey-students

为什么需要专门的迁移而不是"清库重来"
--------------------------------------
``students.student_no`` 是主键，学号一变，**五张表**的关联都得跟着改：

    students / student_courses / attendance_events / attendance_daily / alerts

其中 ``student_courses.student_no`` 有真正的外键约束（ON DELETE CASCADE），
所以顺序必须是「先建新行 → 再改子表 → 最后删旧行」；
反过来的话级联会把选课关系一起删掉。

另示示例辅导员的手动标记、以后导入的名册信息都挂在 ``students`` 行上，
清库重来会把这些人工录入的东西全部丢掉 —— 那是无法从接口重建的数据。
"""

from __future__ import annotations

from app.core.ids import needs_normalizing, normalize_student_no
from app.core.logging_conf import get_logger

log = get_logger("service.rekey")

#: 子表：(表名, 学号列名)。改这些表时学生行**必须已经存在**。
#: ``student_marks`` 也在列：它没有外键约束（流水表刻意不加），
#: 漏掉的话规范化之后那些手工标记的流水会变成查不到的孤儿行。
_CHILD_TABLES = (
    ("student_courses", "student_no"),
    ("attendance_events", "student_no"),
    ("attendance_daily", "student_no"),
    ("alerts", "student_no"),
    ("student_marks", "student_no"),
)


def find_stale_student_nos(db) -> dict[str, str]:
    """找出「库里还带前缀」的学号 → 规范化后的值。"""
    from sqlalchemy import select

    from app.models import Student

    out: dict[str, str] = {}
    for row in db.execute(select(Student.student_no)).scalars():
        if needs_normalizing(row):
            out[row] = normalize_student_no(row)
    return out


def rekey_students(db, dry_run: bool = False) -> dict:
    """把带前缀的学号改成规范化形式。返回统计信息。"""
    from sqlalchemy import text

    mapping = find_stale_student_nos(db)
    result = {
        "students_seen": len(mapping),
        "renamed": 0,
        "skipped_exists": 0,
        "children_updated": 0,
        "dry_run": dry_run,
    }
    if not mapping:
        return result

    if dry_run:
        result["renamed"] = len(mapping)
        return result

    from sqlalchemy import select

    from app.models import Student

    for old, new in mapping.items():
        # 目标学号已经存在 → 不能硬改，否则主键冲突。
        # 保守跳过并报出来，让人工确认（正常数据里不该出现）。
        if db.execute(
            select(Student.student_no).where(Student.student_no == new)
        ).first():
            log.warning("跳过 %s → %s：目标学号已存在，需人工合并", old, new)
            result["skipped_exists"] += 1
            continue

        src = db.get(Student, old)
        if src is None:
            continue

        # 1) 先建新行（字段照抄），这样子表的外键才有落点
        db.add(
            Student(
                student_no=new,
                name=src.name,
                grade=src.grade,
                major_code=src.major_code,
                major_name=src.major_name,
                class_name=src.class_name,
                org_name=src.org_name,
                enroll_year=src.enroll_year,
                active=src.active,
                manual_state=src.manual_state,
                manual_marked_at=src.manual_marked_at,
                manual_reason=src.manual_reason,
                manual_by=src.manual_by,
                created_at=src.created_at,
            )
        )
        db.flush()

        # 2) 再改子表
        for table, column in _CHILD_TABLES:
            r = db.execute(
                text(f'UPDATE "{table}" SET "{column}" = :new WHERE "{column}" = :old'),
                {"new": new, "old": old},
            )
            result["children_updated"] += r.rowcount or 0

        # 3) 最后删旧行（此时 student_courses 已无引用，级联不会误删）
        db.delete(src)
        db.flush()
        result["renamed"] += 1

    db.commit()
    log.warning(
        "学号规范化完成：改名 %d 人，子表更新 %d 行，跳过 %d 人",
        result["renamed"], result["children_updated"], result["skipped_exists"],
    )
    return result
