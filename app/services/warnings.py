# -*- coding: utf-8 -*-
"""四级预警判定（2026-10-13 按学校口径实现）。

规则原文（学校给的四条）
------------------------
1. **学生无记录预警**：学生当日有课，且至少一门相关课程在智慧课堂或畅课平台
   产生了考勤数据，但未查询到该学生的有效考勤记录，则该日计为一个
   「有效统计日无记录」。连续三个有效统计日均无记录时，系统生成**待核查提醒**。
2. **监测覆盖不足提醒**：学生在统计周期（7 天）内有课，但相关课程在智慧课堂、
   畅课平台**均未产生可用的考勤数据**，导致系统无法通过现有考勤数据了解其近期
   活动情况时，生成提醒，提示示例辅导员通过其他方式进一步核实。
3. **长期缺少记录提醒**：在最近 14 个自然日内，学生有课且相关课程产生了可用
   考勤数据，但个人有效考勤记录持续偏少，即使未达到连续三个有效统计日无记录
   的条件，系统仍生成提醒。**若统计周期内考勤数据覆盖不足，则生成监测覆盖
   不足提醒，不直接判定为学生缺少考勤记录。**
4. **数据异常提醒**：课表数据缺失、课程信息无法匹配、接口同步失败或考勤数据
   异常等情况，生成提醒，提示相关人员核查数据。

设计要点
--------
- 这四条是**提醒**，不是学生状态。学生的 5 种状态（活跃/异常/…）保持不变，
  本模块只负责"该给谁发哪种提醒"。一个学生可能同时命中多条（例如既有
  无记录、又有覆盖不足），所以返回的是**集合**，不是单一结论。
- 「覆盖不足」与「缺勤」必须严格分开：前者是**系统看不到**，后者是
  **学生没来**。把覆盖不足当成缺勤，就是拿"我们没数据"去指控学生。
  这是本项目反复踩过的坑（见 docs/选课名单录入-20261010.md）。
- 「有效统计日」的定义是：该生当日有课 **且** 那些课确实产出过考勤数据。
  课没数据的日子不算数 —— 否则"平台没传数据"会被算成"学生没来"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AlertKind
from app.core.logging_conf import get_logger
from app.models import AttendanceEvent, Course, Student, StudentCourse

log = get_logger("service.warnings")


@dataclass(slots=True)
class Warning:
    """一条预警。"""

    kind: AlertKind
    student_no: str
    reason: str
    first_date: date
    last_date: date
    days: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def is_student_side(self) -> bool:
        return self.kind.is_student_side


@dataclass(slots=True)
class DayFact:
    """该生某一天的上课与考勤事实。"""

    day: date
    has_class: bool = False
    #: 当天的课里，**确实产出过考勤数据**的那些课的数量（有效统计日的依据）
    covered_sessions: int = 0
    #: 当天该生自己有没有有效考勤记录（出勤/迟到/早退/请假都算"有记录"）
    has_own_record: bool = False
    present: bool = False
    leave: bool = False

    @property
    def is_effective(self) -> bool:
        """这天算不算「有效统计日」：有课，且那天的课确实有考勤数据。"""
        return self.has_class and self.covered_sessions > 0


def _present_values() -> set[str]:
    values = set(settings.present_status_set)
    values.add("请假")   # 请假也是"有记录"，不算失联
    return values


def load_day_facts(
    db: Session, student_nos: set[str], start: date, end: date
) -> dict[str, dict[date, DayFact]]:
    """装载窗口内每个学生每天的上课/考勤事实。

    三份数据拼出来：

    1. **该生选了什么课**（``student_courses``）—— 回答"哪天可能有课"；
    2. **那些课有没有考勤源**（``Course.covered``）—— 回答"这天算不算有效统计日"；
    3. **该生自己的签到记录**（``attendance_events``）—— 回答"他有没有记录"。
    """
    out: dict[str, dict[date, DayFact]] = {no: {} for no in student_nos}
    if not student_nos:
        return out

    # 1) 学生的选课关系 + 课程是否有考勤源
    rows = db.execute(
        select(
            StudentCourse.student_no,
            Course.class_date,
            StudentCourse.class_date,
            Course.covered,
        )
        .join(Course, Course.schedule_guid == StudentCourse.schedule_guid)
        .where(StudentCourse.student_no.in_(student_nos))
    ).all()
    for no, course_date, link_date, covered in rows:
        day = course_date or link_date
        if day is None or not (start <= day <= end):
            continue
        fact = out[no].setdefault(day, DayFact(day=day))
        fact.has_class = True
        if covered:
            fact.covered_sessions += 1

    # 2) 该生自己的签到记录（有记录的日子）
    present_values = _present_values()
    ev_rows = db.execute(
        select(
            AttendanceEvent.student_no,
            AttendanceEvent.class_date,
            AttendanceEvent.attend_status,
            AttendanceEvent.sign_in_time,
        ).where(
            AttendanceEvent.student_no.in_(student_nos),
            AttendanceEvent.user_type == 2,
            AttendanceEvent.class_date >= start,
            AttendanceEvent.class_date <= end,
        )
    ).all()
    for no, day, status, sign_in in ev_rows:
        fact = out[no].setdefault(day, DayFact(day=day))
        if status in present_values or sign_in is not None:
            fact.has_own_record = True
        if status == "请假":
            fact.leave = True
        elif status in settings.present_status_set or sign_in is not None:
            fact.present = True
    return out


def consecutive_no_record_days(
    days: dict[date, DayFact], now: datetime, max_lookback: int = 60
) -> list[date]:
    """最近连续多少个「有效统计日」完全没有该生的记录（从近往远数）。

    只数**有效统计日**：课没有考勤数据的日子不算 —— 那属于覆盖不足，
    不是学生缺勤。
    """
    today = now.date()
    effective = [
        d for d, f in sorted(days.items(), reverse=True)
        if f.is_effective and d <= today
    ]
    streak: list[date] = []
    for d in effective:
        f = days[d]
        if f.has_own_record:
            break
        streak.append(d)
        if len(streak) >= max_lookback:
            break
    return sorted(streak)


def evaluate_warnings(
    db: Session,
    student_nos: set[str],
    now: datetime | None = None,
    facts: dict[str, dict[date, DayFact]] | None = None,
) -> dict[str, list[Warning]]:
    """算出每个学生当前命中的预警（可能多条）。"""
    now = now or datetime.now()
    today = now.date()
    if not student_nos:
        return {}

    span_days = max(14, settings.monitor_coverage_days, settings.long_term_missing_days)
    start = today - timedelta(days=span_days - 1)
    if facts is None:
        facts = load_day_facts(db, student_nos, start, today)

    coverage_start = today - timedelta(days=settings.monitor_coverage_days - 1)
    long_start = today - timedelta(days=settings.long_term_missing_days - 1)

    # 学生名册信息（预警描述里要写清是谁）
    names = {
        no: nm
        for no, nm in db.execute(
            select(Student.student_no, Student.name).where(
                Student.student_no.in_(student_nos)
            )
        ).all()
    }
    _ = names

    out: dict[str, list[Warning]] = {}
    for no in student_nos:
        days = facts.get(no) or {}
        if not days:
            continue
        fired: list[Warning] = []

        # ── 规则 1：学生无记录预警 ──────────────────────────
        streak = consecutive_no_record_days(days, now)
        if len(streak) >= settings.no_record_days:
            fired.append(
                Warning(
                    kind=AlertKind.NO_RECORD,
                    student_no=no,
                    reason=(
                        f"连续 {len(streak)} 个有效统计日无有效考勤记录"
                        f"（有课且课程已产生考勤数据，但查不到该生的记录）"
                    ),
                    first_date=streak[0],
                    last_date=streak[-1],
                    days=len(streak),
                    detail={"dates": [d.isoformat() for d in streak]},
                )
            )

        # ── 规则 2：监测覆盖不足提醒 ────────────────────────
        cov_days = {d: f for d, f in days.items() if coverage_start <= d <= today}
        class_days = [d for d, f in cov_days.items() if f.has_class]
        covered_days = [d for d, f in cov_days.items() if f.covered_sessions > 0]
        if class_days and not covered_days:
            fired.append(
                Warning(
                    kind=AlertKind.LOW_COVERAGE,
                    student_no=no,
                    reason=(
                        f"最近 {settings.monitor_coverage_days} 天内有课"
                        f"（{len(class_days)} 天），但这些课程在各考勤平台"
                        "均未产生可用考勤数据，系统无法了解其近期活动情况"
                    ),
                    first_date=min(class_days),
                    last_date=max(class_days),
                    days=len(class_days),
                    detail={
                        "window_days": settings.monitor_coverage_days,
                        "class_days": [d.isoformat() for d in sorted(class_days)],
                    },
                )
            )

        # ── 规则 3：长期缺少记录提醒 ────────────────────────
        # 前置条件：14 天内有课，且相关课程确实产生过考勤数据；
        # 若整个窗口覆盖不足 → 只报覆盖不足，不判"缺少记录"（规则明文要求）。
        long_days = {d: f for d, f in days.items() if long_start <= d <= today}
        long_class = [d for d, f in long_days.items() if f.has_class]
        long_covered = [d for d, f in long_days.items() if f.covered_sessions > 0]
        if long_covered and AlertKind.NO_RECORD not in {w.kind for w in fired}:
            effective = [d for d in long_days if long_days[d].is_effective]
            recorded = [d for d in effective if long_days[d].has_own_record]
            ratio = (len(recorded) / len(effective)) if effective else 1.0
            # ⚠ 要求"看得到的日子"足够多才下结论。只有 1 个有效统计日、
            # 恰好那天没记录，不能叫"长期缺少记录" —— 那是偶发，不是长期
            # （实测踩过：全班只上过一天课的新学期开头会集体误报）。
            if (
                len(effective) >= settings.long_term_missing_min_days
                and ratio <= settings.long_term_missing_ratio
                and len(recorded) <= settings.long_term_missing_max_records
            ):
                fired.append(
                    Warning(
                        kind=AlertKind.LONG_TERM_MISSING,
                        student_no=no,
                        reason=(
                            f"最近 {settings.long_term_missing_days} 天内有课"
                            f"{len(long_class)} 天，其中 {len(effective)} 天课程已产生"
                            f"考勤数据，但该生仅有 {len(recorded)} 天有效考勤记录"
                            f"（{ratio * 100:.0f}%），持续偏少"
                        ),
                        first_date=min(effective),
                        last_date=max(effective),
                        days=len(recorded),
                        detail={
                            "effective_days": len(effective),
                            "recorded_days": len(recorded),
                            "ratio": round(ratio, 3),
                            "present_days": sum(
                                1 for d in effective if long_days[d].present
                            ),
                            "leave_days": sum(
                                1 for d in effective if long_days[d].leave
                            ),
                        },
                    )
                )

        if fired:
            out[no] = fired
    return out


def data_anomaly_warnings(db: Session, now: datetime | None = None) -> list[Warning]:
    """规则 4：数据异常提醒（课表缺失 / 课程无法匹配 / 接口失败 / 数据异常）。

    这是**面向管理员**的提醒，与具体学生无关，所以不进学生名单。

    检查项：
    1. 最近一轮采集失败（接口同步失败）；
    2. 有选课关系、但课程行缺失（课程信息无法匹配）；
    3. 有选课关系、但课程既没有课次日期也没有考勤源（课表数据缺失）；
    4. 有选课关系、但课程日期完全落在过去且该课从未产生任何考勤数据。
    """
    from app.models import CollectRun

    now = now or datetime.now()
    today = now.date()
    out: list[Warning] = []

    # 1) 接口同步失败
    last = db.execute(
        select(CollectRun).order_by(CollectRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if last is not None and not last.ok and last.finished_at is not None:
        out.append(
            Warning(
                kind=AlertKind.DATA_ANOMALY,
                student_no="",
                reason=f"最近一轮采集失败：{(last.message or '')[:160]}",
                first_date=last.finished_at.date(),
                last_date=today,
                detail={"job": last.job, "started_at": str(last.started_at)},
            )
        )

    # 2) 选课关系指向不存在的课程行（外键正常情况下不会发生，属数据异常）
    orphan = db.execute(
        select(func.count())
        .select_from(StudentCourse)
        .where(
            ~StudentCourse.schedule_guid.in_(select(Course.schedule_guid))
        )
    ).scalar_one()
    if orphan:
        out.append(
            Warning(
                kind=AlertKind.DATA_ANOMALY,
                student_no="",
                reason=f"有 {orphan} 条选课关系指向不存在的课程行（课程信息无法匹配）",
                first_date=today,
                last_date=today,
                detail={"orphan_links": int(orphan)},
            )
        )

    # 3) 有学生选、但课表没有任何课次日期、也没有考勤源 → 课表数据缺失
    missing_date = db.execute(
        select(func.count(func.distinct(Course.schedule_guid)))
        .select_from(Course)
        .join(StudentCourse, StudentCourse.schedule_guid == Course.schedule_guid)
        .where(Course.class_date.is_(None), Course.covered.is_(False))
    ).scalar_one()
    if missing_date:
        out.append(
            Warning(
                kind=AlertKind.DATA_ANOMALY,
                student_no="",
                reason=(
                    f"有 {missing_date} 门课程既没有课次日期、也没有考勤源"
                    "（课表数据缺失，这些课参与不了判定）"
                ),
                first_date=today,
                last_date=today,
                detail={"courses_without_date": int(missing_date)},
            )
        )

    # 4) 课程日期已过、却一条考勤数据都没有 → 该课考勤数据异常
    stale = db.execute(
        select(func.count())
        .select_from(Course)
        .where(
            Course.class_date.is_not(None),
            Course.class_date < today - timedelta(days=1),
            Course.covered.is_(True),
            ~Course.schedule_guid.in_(select(AttendanceEvent.schedule_guid)),
        )
    ).scalar_one()
    if stale:
        out.append(
            Warning(
                kind=AlertKind.DATA_ANOMALY,
                student_no="",
                reason=f"有 {stale} 节已结束的课（标记为有考勤源）没有产生任何考勤数据",
                first_date=today,
                last_date=today,
                detail={"courses_without_events": int(stale)},
            )
        )
    return out


def warning_summary(db: Session, now: datetime | None = None) -> dict:
    """给页面用：各类预警的当前条数 + 明细。"""
    now = now or datetime.now()
    rows = [
        r[0]
        for r in db.execute(
            select(Student.student_no).where(Student.active.is_(True))
        ).all()
    ]
    per_student = evaluate_warnings(db, set(rows), now=now)
    data_side = data_anomaly_warnings(db, now=now)

    counts = {k.value: 0 for k in AlertKind}
    for warnings in per_student.values():
        for w in warnings:
            counts[w.kind.value] += 1
    for w in data_side:
        counts[w.kind.value] += 1

    detail: list[dict] = []
    for no, warnings in sorted(per_student.items()):
        for w in warnings:
            detail.append(
                {
                    "student_no": no,
                    "kind": w.kind.value,
                    "reason": w.reason,
                    "first_date": w.first_date.isoformat(),
                    "last_date": w.last_date.isoformat(),
                    "days": w.days,
                }
            )
    for w in data_side:
        detail.append(
            {
                "student_no": "",
                "kind": w.kind.value,
                "reason": w.reason,
                "first_date": w.first_date.isoformat(),
                "last_date": w.last_date.isoformat(),
                "days": w.days,
            }
        )
    return {"counts": counts, "detail": detail}
