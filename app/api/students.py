"""学生状态接口：总览、单人周课表、搜索。"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import StudentState
from app.core.config import settings
from app.core.security import require_session
from app.schemas import GroupOut, MarkIn, OverviewOut, StudentCard, WeekOut
from app.services.ingest import dashboard_rows, student_week

router = APIRouter(tags=["students"], dependencies=[Depends(require_session)])

#: 「仅显示异常」包含的状态
ALERT_STATES = {StudentState.ABNORMAL.value, StudentState.SEVERE.value}


@router.get("/overview", response_model=OverviewOut)
def overview(
    db: Session = Depends(get_db),
    show: str = Query("all", pattern="^(all|abnormal|today_class)$"),
    keyword: str = Query("", max_length=32),
) -> OverviewOut:
    """一次性返回全部学生的当前状态，按「年级+专业」分组。

    1200 人规模下单次响应约 200KB，比前端分页拉取更简单也更省事；
    前端自行做展开/收起/筛选，交互零延迟。
    """
    rows = dashboard_rows(db)
    now = datetime.now()

    if keyword.strip():
        kw = keyword.strip().lower()
        rows = [r for r in rows if kw in r["name"].lower() or kw in r["student_no"].lower()]

    if show == "abnormal":
        rows = [r for r in rows if r["state"] in ALERT_STATES]
    elif show == "today_class":
        rows = [r for r in rows if r["last_class_date"] == now.date().isoformat()]

    counts: dict[str, int] = {s.value: 0 for s in StudentState}
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        grouped.setdefault(r["group"], []).append(r)

    state_order = {s.value: -s.priority for s in StudentState}
    groups: list[GroupOut] = []
    for key in sorted(grouped.keys(), reverse=True):
        members = grouped[key]
        # 组内排序：越异常越靠前，其次按学号
        members.sort(key=lambda r: (state_order.get(r["state"], 0), r["student_no"]))
        gcounts: dict[str, int] = {}
        for m in members:
            gcounts[m["state"]] = gcounts.get(m["state"], 0) + 1
        grade = members[0]["grade"]
        major = members[0]["major_name"]
        groups.append(
            GroupOut(
                key=key,
                grade=grade,
                major=major,
                total=len(members),
                counts=gcounts,
                students=[StudentCard(**m) for m in members],
            )
        )

    return OverviewOut(
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        data_mode=settings.data_mode,
        totals={
            "students": len(rows),
            "groups": len(groups),
            "abnormal": sum(counts.get(s, 0) for s in ALERT_STATES),
        },
        counts=counts,
        groups=groups,
    )


@router.get("/students/{student_no}/week", response_model=WeekOut)
def week(
    student_no: str,
    db: Session = Depends(get_db),
    anchor: str | None = Query(None, description="任意一天 YYYY-MM-DD，默认今天"),
) -> WeekOut:
    anchor_date: date | None = None
    if anchor:
        try:
            anchor_date = date.fromisoformat(anchor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="anchor 需为 YYYY-MM-DD") from exc
    data = student_week(db, student_no, anchor=anchor_date)
    return WeekOut(**data)


@router.get("/students/{student_no}", response_model=StudentCard)
def one(student_no: str, db: Session = Depends(get_db)) -> StudentCard:
    for row in dashboard_rows(db):
        if row["student_no"] == student_no:
            return StudentCard(**row)
    raise HTTPException(status_code=404, detail="未找到该学生")


@router.get("/states")
def states() -> dict:
    """前端图例用。"""
    return {
        "states": [
            {"value": s.value, "level": s.level, "priority": s.priority}
            for s in sorted(StudentState, key=lambda x: -x.priority)
        ]
    }


@router.post("/students/{student_no}/mark", response_model=StudentCard)
def mark_student(
    student_no: str,
    payload: MarkIn,
    db: Session = Depends(get_db),
) -> StudentCard:
    """老师手动标记某个学生为「活跃」，解除预警。

    规则（老师定的）：**「标记后，视作一次没有课程的签到」** ——
    这次标记相当于学生刚签到过一次（只不过那天没课），
    于是标记之前的连续无签到全部清零。

    但它**不赠送任何豁免**：标记完第二天仍然没签到，就继续报异常；
    连续三个有课日没签到，就继续报严重异常。

    **可以重复标记**：再来一次就把"签到时刻"刷新到现在，从此刻重新起算。
    每次都会在 ``student_marks`` 里留一条流水（谁、何时、为什么），
    否则重复标记会把上一次的原因覆盖掉，事后没人说得清。
    """
    from app.models import Student
    from app.services.marks import apply_mark

    student = db.get(Student, student_no)
    if student is None:
        raise HTTPException(status_code=404, detail="未找到该学生")

    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="请填写标记原因（会被记录）")

    apply_mark(db, student, state=payload.state, reason=reason, by=payload.by)
    db.commit()

    for row in dashboard_rows(db):
        if row["student_no"] == student_no:
            return StudentCard(**row)
    raise HTTPException(status_code=404, detail="标记后未能读取该学生状态")


@router.delete("/students/{student_no}/mark", response_model=StudentCard)
def unmark_student(student_no: str, db: Session = Depends(get_db)) -> StudentCard:
    """撤销手动标记，立刻恢复按考勤判定。撤销本身也会记一条流水。"""
    from app.models import Student
    from app.services.marks import clear_mark

    student = db.get(Student, student_no)
    if student is None:
        raise HTTPException(status_code=404, detail="未找到该学生")

    clear_mark(db, student)
    db.commit()

    for row in dashboard_rows(db):
        if row["student_no"] == student_no:
            return StudentCard(**row)
    raise HTTPException(status_code=404, detail="撤销后未能读取该学生状态")


@router.get("/students/{student_no}/mark-history")
def get_mark_history(
    student_no: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """标记流水：谁在什么时候、为什么标的（重复标记也能看到前后几次）。"""
    from app.services.marks import mark_count, mark_history

    items = mark_history(db, student_no, limit=limit)
    return {
        "student_no": student_no,
        "total": len(items),
        "mark_times": mark_count(db, student_no),
        "items": items,
    }


@router.get("/marks")
def list_marks(db: Session = Depends(get_db)) -> dict:
    """所有示示例辅导员手动标记过的学生。

    注意：标记**不是豁免**，所以清单里可能有人当前又是「异常」——
    `state_now` 就是给他看这个的（标记过、但之后又没签到）。
    """
    out = []
    for row in dashboard_rows(db):
        if row.get("manual_marked"):
            out.append(
                {
                    "student_no": row["student_no"],
                    "name": row["name"],
                    "group": row["group"],
                    "reason": row.get("manual_reason", ""),
                    "by": row.get("manual_by", ""),
                    "marked_at": row.get("manual_marked_at"),
                    "state_now": row.get("state"),
                }
            )
    out.sort(key=lambda r: (r["marked_at"] or "", r["student_no"]))
    return {"total": len(out), "items": out}
