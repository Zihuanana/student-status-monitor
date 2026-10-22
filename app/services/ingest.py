"""采集入库与分析：把适配器输出写入数据库，并重算判定结果、生成告警。

设计要点
--------
- **幂等**：``attendance_events`` 以 (source, schedule_guid, student_no, class_date)
  唯一键 upsert，重复采集不会产生重复数据。
- **持久化总线**：事件表本身就是消息队列（outbox）。采集写事件 → 分析器重算
  受影响学生的判定结果。这样即使分析器崩溃/重启，事件也不会丢，可以重放。
- **三源 OR 逻辑**：同一 (schedule_guid, student_no, class_date) 下，只要**任一**
  考勤源标记为已到，即视为已签到。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.adapters.base import NormalizedCourse, NormalizedEvent
from app.core.config import settings
from app.core.enums import AttendStatus, Source
from app.core.ids import normalize_student_no
from app.core.logging_conf import get_logger
from app.models import (
    Alert,
    AttendanceDaily,
    AttendanceEvent,
    Course,
    MajorAlias,
    Student,
    StudentCourse,
)
from app.services.attendance import (
    DayEvaluation,
    SessionFact,
    evaluate,
    evaluate_from_days,
)
from app.services.scope import filter_student_ids, student_id_in_scope

log = get_logger("service.ingest")


# ─────────────────────────────────────────────────────────────
# 被学号作用域挡掉的数据：记账
# ─────────────────────────────────────────────────────────────
# 为什么不用返回值：``upsert_events`` / ``upsert_students`` 的返回值各处都在用
# （采集消息、集成测试断言），改成元组会波及一大片。而"挡了多少人"又是
# **必须可见**的 —— 一个静默丢弃数据的过滤器比不装过滤器更危险。
# 所以用一段显式的记账窗口包住写入，由调用方（采集 / CLI）取走并写进日志。
_DROP_LOCK = threading.Lock()
_DROP_BOX: dict | None = None


def start_id_scope_accounting() -> None:
    """开始一段记账窗口（可嵌套复用时以最外层为准）。"""
    global _DROP_BOX
    with _DROP_LOCK:
        if _DROP_BOX is None:
            _DROP_BOX = {"event": [], "student": []}


def note_dropped_ids(kind: str, student_nos: Iterable[str]) -> None:
    """记下被学号作用域挡掉的学号（没有窗口时静默忽略）。"""
    box = _DROP_BOX
    if box is None:
        return
    with _DROP_LOCK:
        box.setdefault(kind, []).extend(n for n in student_nos if n)


def take_dropped_ids() -> dict:
    """结束记账窗口并取走结果：``{kind: [学号, ...]}``。"""
    global _DROP_BOX
    with _DROP_LOCK:
        box, _DROP_BOX = _DROP_BOX, None
    return box or {"event": [], "student": []}


def describe_dropped_ids(box: dict, sample: int = 3) -> str:
    """把记账结果压成一行日志文本（学号打码，日志里不留全量学号）。"""
    events = len(box.get("event") or [])
    students = sorted(set(box.get("student") or []))
    if not events and not students:
        return ""
    parts = []
    if students:
        head = "、".join(students[:sample])
        more = f" 等 {len(students)} 人" if len(students) > sample else ""
        parts.append(f"学生 {len(students)} 名（{head}{more}）")
    if events:
        parts.append(f"签到事件 {events} 条")
    return "学号作用域过滤：" + "，".join(parts)


# ─────────────────────────────────────────────────────────────
# 写入：课程 / 选课关系 / 签到事件
# ─────────────────────────────────────────────────────────────
def upsert_courses(db: Session, courses: list[NormalizedCourse]) -> int:
    """写入或更新课程（录播计划）。返回受影响条数。"""
    if not courses:
        return 0
    count = 0
    for c in courses:
        if not c.schedule_guid:
            continue
        values = {
            "schedule_guid": c.schedule_guid,
            "title": c.title,
            "course_name": c.course_name,
            "course_no": getattr(c, "course_no", ""),
            "teacher_name": c.teacher_name,
            "classroom_name": c.classroom_name,
            "classroom_no": c.classroom_no,
            "building_name": c.building_name,
            "week_name": c.week_name,
            "node_name": c.node_name,
            "class_name": c.class_name,
            "org_name": c.org_name,
            "start_time": c.start_time,
            "end_time": c.end_time,
            "class_date": c.class_date,
            "should_student_count": c.should_student_count,
            "attend_student_count": c.attend_student_count,
            "absent_student_count": c.absent_student_count,
            "late_student_count": c.late_student_count,
            "leave_student_count": c.leave_student_count,
            "attendance_rate": c.attendance_rate,
            "covered": bool(getattr(c, "covered", True)),
        }
        stmt = sqlite_insert(Course).values(**values)
        update_cols = {k: getattr(stmt.excluded, k) for k in values if k != "schedule_guid"}
        db.execute(stmt.on_conflict_do_update(index_elements=["schedule_guid"], set_=update_cols))
        count += 1
    db.flush()
    return count


def upsert_events(db: Session, events: list[NormalizedEvent]) -> int:
    """幂等写入签到事件。返回新写入（非更新）的条数。

    学号不在本院作用域内的事件会被**静默丢弃**（并记数，见
    ``take_dropped_ids``）—— 这是第二道闸门：课程级作用域靠上游的节次命名，
    平台改个名就会静默失效，学号形态则不会。
    """
    if not events:
        return 0

    # 学号规范化放在写入的**唯一入口**：不论数据来自翰博尔、名册推断
    # 还是别的适配器，落库前一律剥掉平台账号前缀（cuc）。
    # 放在这里而不是各适配器里，是因为漏掉任何一个入口都会让同一名学生
    # 在库里出现两行 —— 那种脏数据事后极难合并。
    for e in events:
        e.student_no = normalize_student_no(e.student_no)

    # 再按学号作用域过一道。放在规范化之后：这样匹配的是学号本体
    # （正则里的 cuc 前缀组同时兼容未规范化的历史数据）。
    events, rejected = filter_student_ids(events)
    note_dropped_ids("event", [e.student_no for e in rejected])
    if not events:
        return 0

    # 先查已存在的键，用于统计「新增」
    keys = {(e.source, e.schedule_guid, e.student_no, e.class_date) for e in events}
    existing = set()
    if keys:
        rows = db.execute(
            select(
                AttendanceEvent.source,
                AttendanceEvent.schedule_guid,
                AttendanceEvent.student_no,
                AttendanceEvent.class_date,
            ).where(
                AttendanceEvent.student_no.in_({k[2] for k in keys}),
                AttendanceEvent.class_date.in_({k[3] for k in keys}),
            )
        ).all()
        existing = {(r[0], r[1], r[2], r[3]) for r in rows}

    inserted = 0
    collected_at = datetime.now()
    # 攒批写入：SQLite 单条 INSERT 有固定开销，逐条执行时 2 万条事件会产生
    # 2 万次语句解析与执行（实测是每轮采集的主要耗时来源之一）。
    # 同一 (source, schedule_guid, student_no, class_date) 在**同一批**里
    # 只可能出现一次（适配器天然去重），因此 `executemany` 语义安全。
    batch: list[dict] = []
    for e in events:
        if not e.schedule_guid or not e.student_no:
            continue
        batch.append(
            {
                "source": e.source,
                "schedule_guid": e.schedule_guid,
                "student_no": e.student_no,
                "user_type": e.user_type,
                "class_date": e.class_date,
                "attend_status": e.attend_status,
                "sign_in_time": e.sign_in_time,
                "node_no": e.node_no,
                "classroom_no": e.classroom_no,
                "raw": None,
                "collected_at": collected_at,
            }
        )
        if (e.source, e.schedule_guid, e.student_no, e.class_date) not in existing:
            inserted += 1

    # 每批控制在 500 行以内，规避 SQLite 变量数上限（默认 999/32766 依编译而定）
    for i in range(0, len(batch), 500):
        stmt = sqlite_insert(AttendanceEvent).values(batch[i : i + 500])
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["source", "schedule_guid", "student_no", "class_date"],
                set_={
                    "attend_status": stmt.excluded.attend_status,
                    "sign_in_time": stmt.excluded.sign_in_time,
                    "user_type": stmt.excluded.user_type,
                    "collected_at": stmt.excluded.collected_at,
                    "processed": False,
                },
            )
        )
    db.flush()
    return inserted


def link_student_courses(db: Session, events: list[NormalizedEvent]) -> int:
    """从考勤明细中观测「学生-课程」应到关系，用于判断某天该生是否有课。"""
    if not events:
        return 0
    guids = {e.schedule_guid for e in events if e.schedule_guid}
    dates = {e.class_date for e in events}
    course_dates: dict[str, date | None] = {}
    for row in db.execute(
        select(Course.schedule_guid, Course.class_date).where(Course.schedule_guid.in_(guids))
    ).all():
        course_dates[row[0]] = row[1]

    existing = {
        (r[0], r[1])
        for r in db.execute(
            select(StudentCourse.student_no, StudentCourse.schedule_guid).where(
                StudentCourse.schedule_guid.in_(guids)
            )
        ).all()
    }
    count = 0
    for e in events:
        if not e.schedule_guid or (e.student_no, e.schedule_guid) in existing:
            continue
        db.add(
            StudentCourse(
                student_no=e.student_no,
                schedule_guid=e.schedule_guid,
                class_date=course_dates.get(e.schedule_guid) or e.class_date,
                source=e.source,
            )
        )
        existing.add((e.student_no, e.schedule_guid))
        count += 1
    _ = dates
    db.flush()
    return count


def upsert_students(
    db: Session, students: list[dict], source_kind: str = "attendance"
) -> int:
    """导入/更新学生名册。students: [{student_no, name, grade, major_code, ...}]

    注意：只有提供了非空值时才覆盖已有字段。采集轮询拿不到姓名/专业名，
    若允许空值覆盖，会把导入名册时写好的信息抹掉。

    ``source_kind`` 决定**姓名**的写权限，这是踩过坑加上的：

    - ``"attendance"``（默认，采集/回补自动建人）：姓名只用来**填空**
      （库里没有名字时才写）。翰博尔返回的 ``userName`` 是**学生自己设的
      账号名** —— 实测有人叫「爸爸」「cxy」「Jackie」，每轮采集都会把
      教务处名单里的真名覆盖掉（2026-10-12 排查确认）。
    - ``"roster"``（名册/选课名单导入）：姓名是**权威值**，直接覆盖。

    最后一道学号作用域也在这里：名册导入、采集自动建人、回补三条路都经过
    本函数，挡住不合形态的学号就不必逐个入口去补。
    """
    if not students:
        return 0
    authoritative_name = source_kind == "roster"
    count = 0
    for s in students:
        no = normalize_student_no(s.get("student_no"))
        if not no:
            continue
        if not student_id_in_scope(no):
            note_dropped_ids("student", [no])
            continue
        values = {
            "student_no": no,
            "name": s.get("name") or "",
            "grade": str(s.get("grade") or ""),
            "major_code": str(s.get("major_code") or ""),
            "major_name": s.get("major_name") or "",
            "class_name": s.get("class_name") or "",
            "org_name": s.get("org_name") or "",
            "enroll_year": s.get("enroll_year"),
            "active": True,
            "name_source": "roster" if authoritative_name else "attendance",
        }
        stmt = sqlite_insert(Student).values(**values)
        # 仅用非空的新值更新，避免用空字符串覆盖既有资料
        update_cols: dict = {"updated_at": datetime.now()}
        for col in ("grade", "major_code", "major_name", "class_name", "org_name"):
            update_cols[col] = func.coalesce(
                func.nullif(stmt.excluded[col], ""), getattr(Student, col)
            )
        if authoritative_name:
            update_cols["name"] = func.coalesce(
                func.nullif(stmt.excluded["name"], ""), Student.name
            )
            update_cols["name_source"] = "roster"
        else:
            # 采集侧：只在"库里没有名字"或"现有名字来自考勤系统"时才写。
            # 名册给的名字（name_source='roster'）一律不动 —— 否则学生把
            # 翰博尔账号名改成昵称，页面上的名字就会跟着变。
            update_cols["name"] = case(
                (
                    or_(
                        Student.name == "",
                        Student.name.is_(None),
                        func.coalesce(Student.name_source, "attendance") != "roster",
                    ),
                    func.coalesce(func.nullif(stmt.excluded["name"], ""), Student.name),
                ),
                else_=Student.name,
            )
            update_cols["name_source"] = case(
                (
                    func.coalesce(Student.name_source, "attendance") != "roster",
                    func.coalesce(
                        func.nullif(stmt.excluded["name"], ""),
                        func.coalesce(Student.name_source, "attendance"),
                    ),
                ),
                else_=Student.name_source,
            )
        db.execute(
            stmt.on_conflict_do_update(index_elements=["student_no"], set_=update_cols)
        )
        count += 1
    db.flush()
    return count


# ─────────────────────────────────────────────────────────────
# 分析：重算「学生 × 日期」判定
# ─────────────────────────────────────────────────────────────
def _load_session_facts(
    db: Session, student_nos: set[str], start: date, end: date
) -> dict[str, list[SessionFact]]:
    """按学生装载课时事实（事件 + 选课关系），供状态机使用。

    性能要点
    --------
    这里曾经把**窗口内每一行事件**都取成 ORM 对象、再把**全部课程**加载进字典，
    结果是 1200 人 / 60 天（15.8 万条事件）下单次要 5.3 秒；按保留期 400 天
    推算约 35 秒，页面基本不可用。

    现在把「按人按课聚合」下推给 SQL，只回传**每人每节课一行**，
    并且只加载窗口内真正被引用到的课程。
    """
    facts: dict[str, list[SessionFact]] = defaultdict(list)
    if not student_nos:
        return facts

    present_values = sorted(settings.present_status_set) or ["出勤"]
    absent_value = AttendStatus.ABSENT.value
    leave_value = AttendStatus.LEAVE.value

    # 1) 事件按「学生 + 课程 + 日期」聚合：一节课一行
    agg = (
        select(
            AttendanceEvent.student_no.label("student_no"),
            AttendanceEvent.schedule_guid.label("schedule_guid"),
            AttendanceEvent.class_date.label("class_date"),
            func.max(AttendanceEvent.node_no).label("node_no"),
            func.max(AttendanceEvent.classroom_no).label("classroom_no"),
            func.max(AttendanceEvent.sign_in_time).label("last_sign_in"),
            func.sum(
                case((AttendanceEvent.attend_status.in_(present_values), 1), else_=0)
            ).label("present_cnt"),
            func.sum(
                case((AttendanceEvent.attend_status == absent_value, 1), else_=0)
            ).label("absent_cnt"),
            func.sum(
                case((AttendanceEvent.attend_status == leave_value, 1), else_=0)
            ).label("leave_cnt"),
            func.count(AttendanceEvent.id).label("event_cnt"),
        )
        .where(
            AttendanceEvent.student_no.in_(student_nos),
            AttendanceEvent.class_date >= start,
            AttendanceEvent.class_date <= end,
        )
        .group_by(
            AttendanceEvent.student_no,
            AttendanceEvent.schedule_guid,
            AttendanceEvent.class_date,
        )
    )
    rows = db.execute(agg).all()

    # 2) 只加载被引用到的课程（而不是整张表）
    guids = {r.schedule_guid for r in rows}
    course_info: dict[str, Course] = {}
    for i in range(0, len(guids), 900):  # 规避 SQLite 变量数上限
        chunk = list(guids)[i : i + 900]
        for c in db.execute(
            select(Course).where(Course.schedule_guid.in_(chunk))
        ).scalars():
            course_info[c.schedule_guid] = c

    for r in rows:
        course = course_info.get(r.schedule_guid)
        facts[r.student_no].append(
            SessionFact(
                schedule_guid=r.schedule_guid,
                class_date=r.class_date,
                start_time=course.start_time if course else None,
                end_time=course.end_time if course else None,
                present=bool(r.present_cnt),
                leave=bool(r.leave_cnt),
                absent_recorded=bool(r.absent_cnt),
                sign_in_time=r.last_sign_in,
                course_name=(course.course_name or course.title) if course else "",
                node_name=r.node_no or (course.node_name if course else ""),
                classroom_name=(course.classroom_name or r.classroom_no)
                if course
                else (r.classroom_no or ""),
                # 课程行缺失时按"有覆盖"处理：这条路上来的都是**真实签到事件**，
                # 有事件本身就证明我们能看到这间教室。
                covered=bool(course.covered) if course is not None else True,
            )
        )

    # 3) 应到但完全没有事件的课（接口延迟/漏传）→ 补成「无记录」课时
    observed = {(s, f.schedule_guid) for s, fl in facts.items() for f in fl}
    sc_rows = db.execute(
        select(
            StudentCourse.student_no,
            StudentCourse.schedule_guid,
            StudentCourse.class_date,
        ).where(
            StudentCourse.student_no.in_(student_nos),
            StudentCourse.class_date >= start,
            StudentCourse.class_date <= end,
        )
    ).all()

    missing_guids = {
        r.schedule_guid
        for r in sc_rows
        if (r.student_no, r.schedule_guid) not in observed
    } - set(course_info)
    for i in range(0, len(missing_guids), 900):
        chunk = list(missing_guids)[i : i + 900]
        for c in db.execute(
            select(Course).where(Course.schedule_guid.in_(chunk))
        ).scalars():
            course_info[c.schedule_guid] = c

    for r in sc_rows:
        if (r.student_no, r.schedule_guid) in observed:
            continue
        course = course_info.get(r.schedule_guid)
        if course is None:
            continue
        if not course.covered:
            # 没有考勤源的课**不进判定**（缺一次签到不代表没来，我们看不到
            # 那间教室）——它的出现只由下面「按课次日期展开」那段负责，
            # 依据是课程表里的真实课次日期，而不是"从没观测到"。
            continue
        facts[r.student_no].append(
            SessionFact(
                schedule_guid=r.schedule_guid,
                class_date=course.class_date or r.class_date or start,
                start_time=course.start_time,
                end_time=course.end_time,
                course_name=course.course_name or course.title,
                node_name=course.node_name,
                classroom_name=course.classroom_name,
                # 这条路补的是「有应到关系、但一条事件都没有」的课。
                # 若这门课本来就没有考勤源（教务名单里的课），补出来的
                # SessionFact 会被状态机当缺勤 —— 所以这里必须把标记带上。
                covered=bool(course.covered),
            )
        )

    # 4) 没有考勤源、但**教务课表给出了具体课次日期**的课：
    #    按日期挂到周课表上（页面置灰显示），不进判定。
    #    为什么要单独一段：第 3 段是按"应到关系"补课的，对这些课它会补出
    #    "从没观测到 → 缺勤"；而它们的正确语义是"这天有课，但我们看不到
    #    来没来"。第 4 段的依据是课程表里的**课次日期**，不是观测。
    #
    # ⚠ 日期要用 **Course.class_date**（课表给的课次），不是
    # ``StudentCourse.class_date``（选课名单导入时留空）—— 早期版本按后者
    # 过滤，结果"有课次日期"的课一节都出不来，周课表里静默看不见。
    uncovered = db.execute(
        select(
            StudentCourse.student_no,
            Course.schedule_guid,
            Course.class_date,
            Course.start_time,
            Course.end_time,
            Course.course_name,
            Course.title,
            Course.node_name,
            Course.classroom_name,
        )
        .join(Course, Course.schedule_guid == StudentCourse.schedule_guid)
        .where(
            StudentCourse.student_no.in_(student_nos),
            Course.covered.is_(False),
            Course.class_date.is_not(None),
            Course.class_date >= start,
            Course.class_date <= end,
        )
    ).all()
    for r in uncovered:
        facts[r.student_no].append(
            SessionFact(
                schedule_guid=r.schedule_guid,
                class_date=r.class_date,
                start_time=r.start_time,
                end_time=r.end_time,
                course_name=r.course_name or r.title or "",
                node_name=r.node_name,
                classroom_name=r.classroom_name,
                covered=False,
            )
        )
    return facts


def rebuild_daily(
    db: Session,
    student_nos: set[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    now: datetime | None = None,
    commit: bool = True,
    commit_dates: set[date] | None = None,
    facts: dict | None = None,
) -> int:
    """重算指定学生在指定日期区间内的判定结果，并返回写入的天记录数。

    ``commit_dates`` —— 只把快照写回这些日期（``None`` 表示窗口内全部写回）。

    为什么需要它：连续异常的判定必须看一段历史窗口（默认 60 天），
    但**每轮轮询真正变化的只有今天**。早期实现每轮都把整段窗口 DELETE + INSERT，
    1200 人 × 60 天 = 5.28 万行/轮，实测单轮 20.5 秒，而实际变化只有 1200 行
    —— 44 倍写放大，且长事务会长时间持有写锁。
    现在判定窗口不变（规则不受影响），写回范围收窄到变化的日期。

    ``facts`` 可传入已装载的课时事实（整段判定窗口），避免重复装载 ——
    调用方（`rebuild_from_events`）本来就要为告警算同一份数据。
    """
    now = now or datetime.now()
    end = end or now.date()
    start = start or (end - timedelta(days=30))

    targets = student_nos
    if targets is None:
        targets = {r[0] for r in db.execute(select(Student.student_no)).all()}
    if not targets:
        return 0

    # 老师的手动标记要参与判定：豁免期内一律判为活跃，
    # 豁免一过就恢复正常规则（连续 N 个有课日仍未签到 → 严重异常）。
    marks = _load_manual_marks(db, targets)

    # 判定窗口与写回窗口分开：对齐到同一天，避免"判定了 60 天却只写 1 天"
    if start > end:
        start = end

    db.execute(
        delete(AttendanceDaily).where(
            AttendanceDaily.student_no.in_(targets),
            AttendanceDaily.class_date >= start,
            AttendanceDaily.class_date <= end,
            *(
                [AttendanceDaily.class_date.in_(sorted(commit_dates))]
                if commit_dates
                else []
            ),
        )
    )

    # 关键：只装载**要写回的日期**的逐节课明细。
    # 判定（连续异常）需要整段历史窗口，但写快照只需要变化当天的明细。
    load_start = min(commit_dates) if commit_dates else start
    load_end = max(commit_dates) if commit_dates else end
    all_facts = facts if facts is not None else _load_session_facts(
        db, targets, load_start, load_end
    )

    written = 0
    for student_no in targets:
        sessions = all_facts.get(student_no, [])
        if not sessions:
            continue
        verdict = evaluate(sessions, now=now, mark=marks.get(student_no))
        by_date: dict[date, list[SessionFact]] = defaultdict(list)
        for s in sessions:
            by_date[s.class_date].append(s)
        for d, ss in by_date.items():
            if not (start <= d <= end):
                continue
            if commit_dates is not None and d not in commit_dates:
                continue
            from app.services.attendance import summarize_day

            summary = summarize_day(d, ss, now)
            state = ""
            if d == verdict.last_class_date:
                state = verdict.state.value
            db.add(
                AttendanceDaily(
                    student_no=student_no,
                    class_date=d,
                    has_class=summary.session_count > 0,
                    has_signed=summary.present_count > 0,
                    has_leave=summary.leave_count > 0,
                    session_count=summary.session_count,
                    missed_count=summary.missed_count,
                    present_count=summary.present_count,
                    pending_count=summary.pending_count,
                    day_complete=summary.day_complete,
                    all_missed=summary.all_missed,
                    state=state,
                    last_sign_in_time=summary.last_sign_in_time,
                    computed_at=now,
                )
            )
            written += 1
    if commit:
        db.commit()
    else:
        db.flush()
    return written


def infer_grade_from_student_no(student_no: str) -> str:
    """从学号里推断年级（4 位年份）。

    不要用固定切片：不同学校的学号格式差别很大，本项目就踩过 ——
    翰博尔返回的是 ``202400000001084`` 这类"前缀 + 年份 + 序号"，
    按 ``[:4]`` 切片会得到 ``cuc2``。

    这里先找 4 位年份（20xx），找不到再退回切片。
    """
    import re

    m = re.search(r"(20\d{2})", student_no or "")
    if m:
        return m.group(1)
    a, _, b = settings.student_id_year_slice.partition(":")
    try:
        return (student_no or "")[int(a or 0): int(b) if b else 4]
    except (ValueError, IndexError):
        return ""


def infer_major_code_from_student_no(student_no: str) -> str:
    """从学号里取专业代码片段；取不到就返回空（不要瞎猜）。

    翰博尔的 ``userNo`` 形如 ``202400000001N`` —— 前面有 ``cuc`` 前缀、
    后面有 ``N`` 后缀，学号本体 ``202400000001`` 夹在中间。
    早期版本要求整个串是纯数字，于是所有学生都被判成"未知专业"，
    页面上 749 个人挤在一个分组里。这里先**抠出中间的数字段**再切片。
    """
    import re

    a, _, b = settings.student_id_major_slice.partition(":")
    try:
        start, stop = int(a or 0), int(b) if b else 6
    except ValueError:
        return ""

    text = student_no or ""
    if not text.isdigit():
        # 取最长的连续数字段当学号本体；纯数字学号走上面的快路径。
        runs = re.findall(r"\d+", text)
        if not runs:
            return ""
        text = max(runs, key=len)
    return text[start:stop]


def _load_manual_marks(db, student_nos) -> dict[str, "ManualMark"]:
    """批量装载学生的手动标记（只取有标记的行）。"""
    from sqlalchemy import select

    if not student_nos:
        return {}
    rows = db.execute(
        select(Student).where(
            Student.student_no.in_(list(student_nos)),
            Student.manual_state != "",
        )
    ).scalars()
    out: dict[str, ManualMark] = {}
    for s in rows:
        m = manual_mark_of(s)
        if m is not None:
            out[s.student_no] = m
    return out


def manual_mark_of(student) -> "ManualMark | None":
    """把学生行上的手动标记转成状态机认识的 :class:`ManualMark`。"""
    from app.services.attendance import ManualMark

    if not student.manual_state or student.manual_marked_at is None:
        return None
    return ManualMark(
        marked_on=student.manual_marked_at.date(),
        state=student.manual_state,
        reason=student.manual_reason or "",
        by=student.manual_by or "",
    )


def major_alias_map(db) -> dict[str, str]:
    """专业代码 → 专业名称。库里的 ``major_aliases`` 表优先。"""
    from sqlalchemy import select

    return {r.code: r.name for r in db.execute(select(MajorAlias)).scalars()}


def sync_major_aliases(db, commit: bool = True) -> int:
    """把配置里的 ``MAJOR_ALIASES`` 同步进 ``major_aliases`` 表。

    为什么不只读配置：这张表**可以由名册导入覆盖**（信息化处给的名册里
    通常带专业名），所以库里已有的值优先，配置只负责"打底"。
    更新时只补**不存在**的代码，不覆盖库里已有的名称。
    """
    from sqlalchemy import select

    existing = {r.code for r in db.execute(select(MajorAlias)).scalars()}
    added = 0
    for code, name in settings.major_alias_map.items():
        if code in existing:
            continue
        db.add(MajorAlias(code=code, name=name))
        added += 1
    if commit and added:
        db.commit()
    return added


def display_major(major_name: str, major_code: str, aliases: dict[str, str]) -> str:
    """页面/邮件里显示的专业名：名册 > 别名表 > 代码 > 未知。"""
    return (
        (major_name or "").strip()
        or aliases.get(major_code, "")
        or (f"专业{major_code}" if major_code else "未知专业")
    )


def recompute_student_identifiers(db, commit: bool = True) -> int:
    """按当前的学号切片规则，重算所有学生的年级/专业代码。

    为什么需要单独一个函数：``STUDENT_ID_YEAR_SLICE`` / ``STUDENT_ID_MAJOR_SLICE``
    是**配置**，改了它之后 ``rebuild``（只重算天记录）不会让库里已有的
    ``students.grade`` / ``major_code`` 跟着变 —— 页面依旧按旧分组显示，
    看起来像"配置没生效"。实测踩过：把切片从 ``4:6`` 改成 ``6:9`` 后，
    749 人还是全挤在一个"专业29"里。
    """
    from sqlalchemy import select

    from app.models import Student

    touched = 0
    for stu in db.execute(select(Student)).scalars():
        grade = infer_grade_from_student_no(stu.student_no)
        major_code = infer_major_code_from_student_no(stu.student_no)
        if grade != (stu.grade or "") or major_code != (stu.major_code or ""):
            stu.grade = grade
            stu.major_code = major_code
            touched += 1
    if commit and touched:
        db.commit()
    return touched


def rebuild_from_events(
    db: Session, events: list[NormalizedEvent], now: datetime | None = None
) -> dict:
    """采集流水线：写课程/事件/选课关系 → 重算受影响学生 → 生成告警。"""
    from app.services.alerts import evaluate_and_alert

    now = now or datetime.now()
    students = {e.student_no for e in events}
    if not students:
        return {"events": 0, "inserted": 0, "daily": 0, "alerts": 0}

    inserted = upsert_events(db, events)
    link_student_courses(db, events)
    # 先提交「写入」部分，把写锁尽早释放。
    # 后面装载 60 天历史是**纯读**，非常慢（实测 2～4.5 秒）；
    # 若让它留在写事务里，页面读取会被拖慢 3～4 倍
    # （实测：无并发 1.19s → 采集进行中 3.80s）。SQLite 是单写者，
    # 缩短写事务窗口比"少提交一次"重要得多。
    db.commit()

    dates = [e.class_date for e in events]
    start, end = min(dates), max(dates)
    # 判定：装载 60 天窗口的历史事实，算一次 verdict 后同时供
    # 「写天快照」与「生成告警」使用 —— 二者此前各自装载一遍同样的数据，
    # 实测占单轮采集一半以上的时间。
    verdict_start = start - timedelta(days=60)
    verdict_end = max(end, now.date())
    verdict_facts = _load_session_facts(db, students, verdict_start, verdict_end)

    daily = rebuild_daily(
        db,
        student_nos=students,
        start=verdict_start,
        end=verdict_end,
        now=now,
        commit=False,
        commit_dates=set(dates),
        facts=verdict_facts,
    )
    alerts = evaluate_and_alert(db, students, now=now, commit=False, facts=verdict_facts)
    db.commit()
    log.info(
        "采集完成：事件 %d 条（新增 %d），天记录 %d，告警 %d",
        len(events), inserted, daily, alerts,
    )
    return {"events": len(events), "inserted": inserted, "daily": daily, "alerts": alerts}


# ─────────────────────────────────────────────────────────────
# 查询：仪表盘数据
# ─────────────────────────────────────────────────────────────
def _build_student_row(
    student: Student,
    verdict,
    major: str,
    day: DayEvaluation | None,
    courses: list[str] | None = None,
) -> dict:
    """把「学生 + 判定结果」整理成统一结构。

    仪表盘卡片与告警邮件都用它，保证**老师收到的邮件与页面看到的一致**
    （否则会出现"邮件的判定说明和页面不一致"这种最难排查的问题）。
    """
    return {
        "student_no": student.student_no,
        "name": student.name or student.student_no,
        "grade": student.grade,
        "major_code": student.major_code,
        "major_name": major,
        "class_name": student.class_name,
        "org_name": student.org_name,
        "group": f"{student.grade}级{major}" if student.grade else major,
        "state": verdict.state.value,
        "level": verdict.state.level,
        "reason": verdict.reason,
        "consecutive_missed_days": verdict.consecutive_missed_days,
        "last_class_date": verdict.last_class_date.isoformat() if verdict.last_class_date else None,
        "last_sign_in_time": verdict.last_sign_in_time.strftime("%Y-%m-%d %H:%M")
        if verdict.last_sign_in_time
        else None,
        # 老师手动标记：页面要能看出"这个学生示示例辅导员确认过"
        "manual_marked": verdict.manual_marked,
        # 精确到分钟：老师要求卡片上直接显示「手动标记：2026-09-20 12:37」
        "manual_marked_at": (
            student.manual_marked_at.strftime("%Y-%m-%d %H:%M")
            if student.manual_marked_at
            else None
        ),
        "manual_reason": student.manual_reason or "",
        "manual_by": student.manual_by or "",
        # 供告警邮件使用的上下文
        "date": day.class_date.isoformat() if day is not None else None,
        "session_count": day.session_count if day is not None else 0,
        "courses": courses or [],
    }


def _courses_by_date(
    db: Session, student_nos: set[str], days: set[date]
) -> dict[tuple[str, date], list[str]]:
    """查「某学生某天有几节课、分别是什么课」，用于告警邮件。"""
    if not student_nos or not days:
        return {}
    rows = db.execute(
        select(
            AttendanceEvent.student_no,
            AttendanceEvent.class_date,
            Course.course_name,
            Course.title,
            Course.node_name,
        )
        .join(Course, Course.schedule_guid == AttendanceEvent.schedule_guid, isouter=True)
        .where(
            AttendanceEvent.user_type == 2,
            AttendanceEvent.student_no.in_(student_nos),
            AttendanceEvent.class_date.in_(sorted(days)),
        )
    ).all()

    out: dict[tuple[str, date], list[str]] = defaultdict(list)
    for no, day, course_name, title, node in rows:
        label = course_name or title or "未命名课程"
        if node:
            label = f"{label}（{node}）"
        bucket = out[(no, day)]
        if label not in bucket:
            bucket.append(label)
    return dict(out)


def dashboard_rows(db: Session, now: datetime | None = None) -> list[dict]:
    """返回所有在读学生的当前状态（用于仪表盘卡片）。"""
    now = now or datetime.now()
    students = list(db.execute(select(Student).where(Student.active.is_(True))).scalars())
    if not students:
        return []

    nos = {s.student_no for s in students}
    # 只取最近 30 天：连续异常判定最多需要「最近 N 个有课日」，
    # 30 天足以覆盖任何合理长假，同时显著降低聚合量。
    days = _load_day_evaluations(
        db, nos, now.date() - timedelta(days=30), now.date(), now
    )
    alias = major_alias_map(db)

    out: list[dict] = []
    for s in students:
        daily = days.get(s.student_no, [])
        verdict = evaluate_from_days(daily, now=now, mark=manual_mark_of(s))
        major = display_major(s.major_name, s.major_code, alias)
        # 找到判定所依据的那一天，附带当天课程名（告警邮件用）
        anchor = None
        for d in daily:
            if d.class_date == verdict.last_class_date:
                anchor = d
                break
        out.append(_build_student_row(s, verdict, major, anchor))
    return out


def student_status_detail(
    db: Session, student_nos: set[str], now: datetime | None = None
) -> dict[str, dict]:
    """单个/少量学生的详细状态（含缺勤当天课程名与最近缺勤日期）。

    告警邮件用它把「缺了哪几节课」写清楚 —— 老师不必打开页面就能判断
    这个学生是真旷课还是有别的情况（实习、生病、转专业……）。
    """
    now = now or datetime.now()
    if not student_nos:
        return {}

    students = {
        s.student_no: s
        for s in db.execute(
            select(Student).where(Student.student_no.in_(student_nos))
        ).scalars()
    }
    days = _load_day_evaluations(
        db, student_nos, now.date() - timedelta(days=60), now.date(), now,
        include_unobserved=True,
    )

    # 需要课程名的日期：每个学生最近 3 个「整日无签到」
    needed: set[date] = set()
    per_student_dates: dict[str, list[date]] = {}
    for no, daily in days.items():
        missed = sorted(
            (d.class_date for d in daily if d.all_missed), reverse=True
        )[:3]
        per_student_dates[no] = missed
        needed.update(missed)

    course_map = _courses_by_date(db, set(days.keys()), needed)

    out: dict[str, dict] = {}
    alias = major_alias_map(db)
    for no in student_nos:
        student = students.get(no)
        if student is None:
            continue
        daily = days.get(no, [])
        verdict = evaluate_from_days(daily, now=now, mark=manual_mark_of(student))
        anchor = next(
            (d for d in daily if d.class_date == verdict.last_class_date), None
        )
        courses = course_map.get((no, anchor.class_date), []) if anchor else []
        row = _build_student_row(
            student,
            verdict,
            display_major(student.major_name, student.major_code, alias),
            anchor,
            courses,
        )
        row["recent_missed_dates"] = [
            d.isoformat() for d in per_student_dates.get(no, [])
        ]
        row["recent_missed_courses"] = {
            d.isoformat(): course_map.get((no, d), [])
            for d in per_student_dates.get(no, [])
        }
        out[no] = row
    return out


def _load_day_evaluations(
    db: Session,
    student_nos: set[str],
    start: date,
    end: date,
    now: datetime,
    include_unobserved: bool = False,
) -> dict[str, list[DayEvaluation]]:
    """按「学生 × 日期」在 SQL 侧聚合，供总览页判定使用。

    与 :func:`_load_session_facts` 的关系：后者返回逐节课明细（周课表需要），
    本函数只返回每天一行汇总。总览页面一次要算 1200 名学生，
    逐节课装载在 60 天窗口下是 15.8 万行对象（实测约 4.6 秒），
    按天聚合后约 3.6 万行，且字段全部由 SQL 算出。

    ``include_unobserved``：是否把「有应到关系但完全没有签到记录」的课
    也算进来（这些课按已结束未签到处理）。总览页面无需如此精确，
    但**告警判定**需要 —— 否则"整节课没来"的学生会被漏掉。
    """
    out: dict[str, list[DayEvaluation]] = defaultdict(list)
    if not student_nos:
        return out

    present_values = sorted(settings.present_status_set) or [AttendStatus.PRESENT.value]
    absent_value = AttendStatus.ABSENT.value
    leave_value = AttendStatus.LEAVE.value

    ended = and_(
        Course.end_time.is_not(None),
        Course.end_time <= now,
    )
    unfinished = or_(Course.end_time.is_(None), Course.end_time > now)
    has_sign = or_(
        AttendanceEvent.sign_in_time.is_not(None),
        AttendanceEvent.attend_status.in_(present_values),
    )
    # 没有考勤源覆盖的课（教务名单里有、录播系统看不到的课）：
    # 照常计入 session_count（"这天有课"是事实），但不计入缺勤、也不计入 pending
    # —— 我们连这节课有没有人都看不到，谈不上"未签到"或"还没结束"。
    no_course_row = Course.schedule_guid.is_(None)
    no_source = or_(no_course_row, Course.covered.is_(False))

    stmt = (
        select(
            AttendanceEvent.student_no.label("student_no"),
            AttendanceEvent.class_date.label("class_date"),
            func.count(AttendanceEvent.id).label("session_count"),
            func.sum(case((has_sign, 1), else_=0)).label("present_count"),
            func.sum(case((AttendanceEvent.attend_status == leave_value, 1), else_=0)).label(
                "leave_count"
            ),
            func.sum(
                case((and_(unfinished, no_source.is_(False)), 1), else_=0)
            ).label("pending_count"),
            func.sum(
                case((and_(ended, has_sign.is_(False), no_source.is_(False)), 1), else_=0)
            ).label("missed_count"),
            func.sum(case((no_source, 1), else_=0)).label("no_source_count"),
            func.max(AttendanceEvent.sign_in_time).label("last_sign_in"),
        )
        .join(Course, Course.schedule_guid == AttendanceEvent.schedule_guid, isouter=True)
        .where(
            AttendanceEvent.user_type == 2,
            AttendanceEvent.student_no.in_(student_nos),
            AttendanceEvent.class_date >= start,
            AttendanceEvent.class_date <= end,
        )
        .group_by(AttendanceEvent.student_no, AttendanceEvent.class_date)
    )

    for r in db.execute(stmt).all():
        session_count = int(r.session_count or 0)
        present = int(r.present_count or 0)
        leave = int(r.leave_count or 0)
        pending = int(r.pending_count or 0)
        no_src = int(r.no_source_count or 0)
        # 没有课程结束时间的课无法判定"是否已结束"，按已结束处理（与逐节课路径一致）
        missed = session_count - present - leave - pending - no_src
        completed = pending == 0
        # 「整日无签到」只在**有可判定课时**时成立：全是看不到的课 → 不指控
        judged = session_count - no_src
        out[r.student_no].append(
            DayEvaluation(
                class_date=r.class_date,
                session_count=session_count,
                present_count=present,
                missed_count=max(0, missed),
                pending_count=pending,
                leave_count=leave,
                day_complete=completed,
                all_missed=bool(judged > 0 and completed and not present and not leave),
                last_sign_in_time=r.last_sign_in,
                no_source_count=no_src,
            )
        )
    _ = absent_value

    if not include_unobserved:
        return out

    # 合并「有应到关系但完全没有事件」的课：按已结束未签到处理
    observed = {(no, d.class_date) for no, ds in out.items() for d in ds}
    sc_rows = db.execute(
        select(
            StudentCourse.student_no,
            StudentCourse.schedule_guid,
            StudentCourse.class_date,
        ).where(
            StudentCourse.student_no.in_(student_nos),
            StudentCourse.class_date >= start,
            StudentCourse.class_date <= end,
        )
    ).all()
    missing: dict[tuple[str, date], int] = defaultdict(int)
    need_guids: set[str] = set()
    for r in sc_rows:
        if (r.student_no, r.class_date) in observed:
            continue
        missing[(r.student_no, r.class_date)] += 1
        need_guids.add(r.schedule_guid)

    if not missing:
        return out

    # 这些课是否已结束 + **有没有考勤源覆盖**：以课程行判断。
    # 没有覆盖的课（教务名单里有、录播系统看不到）不能算未签到 ——
    # 这正是"录了名单就集体误报"的那条路，必须在这里掐掉。
    ended_guids: set[str] = set()
    uncovered_guids: set[str] = set()
    guids = sorted(need_guids)
    for i in range(0, len(guids), 900):
        chunk = guids[i : i + 900]
        for guid, end_time, covered in db.execute(
            select(Course.schedule_guid, Course.end_time, Course.covered).where(
                Course.schedule_guid.in_(chunk)
            )
        ).all():
            if not covered:
                uncovered_guids.add(guid)
            elif end_time is None or end_time <= now:
                ended_guids.add(guid)

    guid_by_pair: dict[tuple[str, date], list[str]] = defaultdict(list)
    for r in sc_rows:
        if (r.student_no, r.class_date) in observed:
            continue
        guid_by_pair[(r.student_no, r.class_date)].append(r.schedule_guid)

    by_student: dict[str, list[DayEvaluation]] = defaultdict(list)
    for (no, day), _count in missing.items():
        pairs = guid_by_pair[(no, day)]
        no_src = sum(1 for g in pairs if g in uncovered_guids)
        count = len(pairs) - no_src
        ended = sum(1 for g in pairs if g in ended_guids)
        pending = count - ended
        existing = next((d for d in out.get(no, []) if d.class_date == day), None)
        if existing is not None:
            existing.session_count += count
            existing.no_source_count += no_src
            existing.pending_count += pending
            existing.missed_count += ended
            existing.day_complete = existing.pending_count == 0
            judged = existing.session_count - existing.no_source_count
            existing.all_missed = bool(
                judged > 0
                and existing.day_complete
                and not existing.present_count
                and not existing.leave_count
            )
        elif count or no_src:
            judged = count
            by_student[no].append(
                DayEvaluation(
                    class_date=day,
                    session_count=count + no_src,
                    present_count=0,
                    missed_count=ended,
                    pending_count=pending,
                    leave_count=0,
                    day_complete=pending == 0,
                    all_missed=bool(judged > 0 and pending == 0),
                    no_source_count=no_src,
                )
            )
    for no, ds in by_student.items():
        out.setdefault(no, []).extend(ds)
    return out


def student_week(db: Session, student_no: str, anchor: date | None = None, now: datetime | None = None) -> dict:
    """某学生某一周的课表 + 每节课签到状态（前端涂色用）。"""
    from app.services.attendance import classify_session, summarize_day, week_range

    now = now or datetime.now()
    anchor = anchor or now.date()
    week_start, week_end = week_range(anchor)

    student = db.get(Student, student_no)
    facts = _load_session_facts(db, {student_no}, week_start, week_end).get(student_no, [])
    facts.sort(key=lambda f: (f.class_date, f.start_time or datetime.min))

    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        day_facts = [f for f in facts if f.class_date == d]
        summary = summarize_day(d, day_facts, now) if day_facts else None
        days.append(
            {
                "date": d.isoformat(),
                "weekday": "一二三四五六日"[d.weekday()],
                "is_today": d == now.date(),
                "has_class": bool(day_facts),
                "state": _day_label(summary),
                "no_source_count": getattr(summary, "no_source_count", 0) if summary else 0,
                "sessions": [
                    {
                        "schedule_guid": f.schedule_guid,
                        "course_name": f.course_name,
                        "node_name": f.node_name,
                        "classroom": f.classroom_name,
                        "start": f.start_time.strftime("%H:%M") if f.start_time else "",
                        "end": f.end_time.strftime("%H:%M") if f.end_time else "",
                        "state": classify_session(f, now).value,
                        "signed": f.present,
                        # 前端据此置灰：这门课我们看不到"来没来"，
                        # 显示它但不参与判定，避示示例辅导员以为学生旷课。
                        "covered": bool(f.covered),
                        "sign_in_time": f.sign_in_time.strftime("%H:%M") if f.sign_in_time else None,
                    }
                    for f in day_facts
                ],
            }
        )

    verdict = evaluate(facts, now=now, mark=manual_mark_of(student) if student else None)
    return {
        "student_no": student_no,
        "name": student.name if student else student_no,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "week_label": f"{week_start:%Y-%m-%d} ~ {week_end:%m-%d}",
        "state": verdict.state.value,
        "level": verdict.state.level,
        "reason": verdict.reason,
        "manual_marked": verdict.manual_marked,
        "manual_marked_at": (
            student.manual_marked_at.strftime("%Y-%m-%d %H:%M")
            if student and student.manual_marked_at
            else None
        ),
        "manual_reason": (student.manual_reason if student else "") or "",
        "manual_by": (student.manual_by if student else "") or "",
        "days": days,
    }


def _day_label(summary) -> str:
    if summary is None:
        return "无课"
    if summary.present_count > 0:
        return "已签到"
    if summary.pending_count > 0 or not summary.day_complete:
        return "进行中"
    if summary.leave_count > 0:
        return "请假"
    return "旷课"


def collect_stats(db: Session) -> dict:
    """采集健康度汇总。"""
    from app.models import CollectRun

    last = db.execute(
        select(CollectRun).order_by(CollectRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    total_events = db.execute(select(func.count(AttendanceEvent.id))).scalar_one()
    total_students = db.execute(
        select(func.count(Student.student_no)).where(Student.active.is_(True))
    ).scalar_one()
    total_courses = db.execute(select(func.count(Course.schedule_guid))).scalar_one()
    return {
        "last_run_at": last.started_at.strftime("%Y-%m-%d %H:%M:%S") if last else None,
        "last_run_ok": bool(last.ok) if last else None,
        "last_run_message": last.message if last else "",
        "total_events": total_events,
        "total_students": total_students,
        "total_courses": total_courses,
        "sources": [s.label for s in Source],
    }
