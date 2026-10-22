# -*- coding: utf-8 -*-
"""手动标记的读写，以及**流水**记录。

两个概念要分清：

- **当前标记**：存在 ``students`` 表的 4 个字段上。判定链路按它把
  标记日及以前的有课日切掉（"视作一次签到"），所以必须在学生行上，读一行就够。
- **标记流水**：存在 ``student_marks`` 表。重复标记会覆盖"当前标记"，
  光靠它就查不到"上一次是谁、为什么标的"。这是一个要能追责的操作，
  所以每次标记/撤销都单独记一条，只增不改。

    python -m app.cli marks --student-no 202400000001N
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_conf import get_logger
from app.models import Student, StudentMark

log = get_logger("service.marks")


def record_mark(
    db: Session,
    student_no: str,
    action: str,
    *,
    state: str = "",
    reason: str = "",
    by: str = "",
) -> StudentMark:
    """往流水里追加一条。``action`` 为 ``mark`` 或 ``unmark``。"""
    row = StudentMark(
        student_no=student_no,
        action=action,
        state=state,
        reason=reason,
        by=by,
        created_at=datetime.now(),
    )
    db.add(row)
    return row


def apply_mark(
    db: Session,
    student: Student,
    *,
    state: str = "活跃",
    reason: str,
    by: str = "老师",
) -> StudentMark:
    """设置"当前标记"并记一条流水。

    **重复调用是合法的**：每次都会把 ``manual_marked_at`` 刷新成现在，
    等价于"又视作一次签到" —— 把此刻之前的连续无签到再清零一遍。
    这正示示例辅导员想要的"标记完还能再标记"。
    """
    student.manual_state = (state or "活跃").strip() or "活跃"
    student.manual_marked_at = datetime.now()
    student.manual_reason = (reason or "").strip()[:255]
    student.manual_by = (by or "老师").strip()[:64]
    return record_mark(
        db,
        student.student_no,
        "mark",
        state=student.manual_state,
        reason=student.manual_reason,
        by=student.manual_by,
    )


def clear_mark(db: Session, student: Student, *, by: str = "老师") -> StudentMark:
    """撤销"当前标记"并记一条流水。"""
    reason = student.manual_reason or ""
    row = record_mark(db, student.student_no, "unmark", reason=reason, by=by)
    student.manual_state = ""
    student.manual_marked_at = None
    student.manual_reason = ""
    student.manual_by = ""
    return row


def mark_history(db: Session, student_no: str, limit: int = 50) -> list[dict]:
    """某学生的标记流水，新的在前。"""
    rows = db.execute(
        select(StudentMark)
        .where(StudentMark.student_no == student_no)
        .order_by(StudentMark.created_at.desc(), StudentMark.id.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": r.id,
            "action": r.action,
            "state": r.state,
            "reason": r.reason,
            "by": r.by,
            "at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
        }
        for r in rows
    ]


def mark_count(db: Session, student_no: str) -> int:
    """这名学生被标记过几次（不含撤销）。页面用来提示"已多次标记"。"""
    from sqlalchemy import func

    return int(
        db.execute(
            select(func.count(StudentMark.id)).where(
                StudentMark.student_no == student_no,
                StudentMark.action == "mark",
            )
        ).scalar()
        or 0
    )
