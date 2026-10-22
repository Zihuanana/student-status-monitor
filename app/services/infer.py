"""应到未签到推断（补齐接口的「漏人」问题）。

背景
----
考勤详情接口通常只返回**有考勤动作**的人。整节课没来的学生很可能
根本不在返回列表里 —— 如果只按返回结果判断，最该被发现的异常学生
反而会「查无此人」。

两种补齐策略（按可用数据自动选择）
---------------------------------
**A. 名册对比（精确，推荐）**
    已知「班级 → 学生」映射 + 该班级当天有课 →
    应到名册 减去 已观测到的学生 = 推断缺勤。
    使用 ``Course.class_name`` 与 ``Student.class_name`` 对齐；
    班级信息缺失时退化为按 ``ShouldStudentCount`` 数量核对，不生成具体人。

**B. 课程成员制（保守）**
    没有名单时，用历史观测到的「上过该课的学生」作为应到集合。
    必须显式开启（``INFER_FROM_HISTORY=true``），因为它无法发现
    「从来没来过」的新异常，属于兜底手段。

推断出来的记录会带 ``raw={"inferred": true, "reason": ...}``，便于审计，
并在前端标注「推断」。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.base import NormalizedEvent
from app.core.enums import AttendStatus, Source
from app.core.logging_conf import get_logger
from app.models import Course, Student, StudentCourse
from app.services.roster import class_key

log = get_logger("service.infer")


def _max_infer_ratio() -> float:
    """一节课缺勤人数超过该比例时不自动推断。

    这是防止「选课名单不准」被放大成大规模误报的保险丝。但阈值不能太紧：
    小班（十几人）遇到一次集体缺勤就会触发，导致真正旷课的学生被整批放过。
    默认 0.8，可用 INFER_MAX_RATIO 调整。
    """
    from app.core.config import settings

    try:
        value = float(getattr(settings, "infer_max_ratio", 0.8))
    except (TypeError, ValueError):
        return 0.8
    return min(1.0, max(0.1, value))


def _observed(db: Session, schedule_guid: str, class_date: date) -> set[str]:
    """该节课当天已观测到的学生学号。"""
    from app.models import AttendanceEvent

    rows = db.execute(
        select(AttendanceEvent.student_no).where(
            AttendanceEvent.schedule_guid == schedule_guid,
            AttendanceEvent.class_date == class_date,
            AttendanceEvent.user_type == 2,
        )
    ).all()
    return {r[0] for r in rows}


def infer_absentees(
    db: Session,
    courses: list[Course],
    rosters: dict[str, set[str]] | None = None,
    now: datetime | None = None,
    from_history: bool = False,
) -> list[NormalizedEvent]:
    """对已结束的课程推断「应到未签到」的学生，返回补充的考勤事件。"""
    now = now or datetime.now()
    if not courses:
        return []

    rosters = rosters if rosters is not None else class_rosters(db)
    out: list[NormalizedEvent] = []
    skipped_ratio = 0
    skipped_uncovered = 0

    for course in courses:
        if not course.class_date or not course.end_time or course.end_time > now:
            continue  # 还没结束，不下结论
        if not _is_covered(course):
            # 这门课没有考勤源覆盖（教务名单里有、录播系统看不到的课）。
            # 没有观测数据 ≠ 学生没来，推下去就是凭空指控：
            # 名单里 118 门课只有 19 门能考勤，一旦推错就是上千人集体误报。
            skipped_uncovered += 1
            continue

        observed = _observed(db, course.schedule_guid, course.class_date)
        expected: set[str] = set()

        # 班级名必须**归一化后**再比：教务写「2024数字媒体技术（中外合作办学）1班」、
        # 翰博尔写「数字媒体技术241班」，字符串直接比是 0 命中 ——
        # 那样不会报错，只是永远发现不了旷课学生。
        # 三条路按「可信度」排列：
        #   1. 归一化班级名能对上 → 按班取应到（最准）
        #   2. 原始班级名能对上   → 兼容"智能241班"这类简写（折不出专业全名）
        #   3. 选课名单建立的「学生 → 课程」关系 → 谁选了这门课的事实
        key = class_key(course.class_name)
        raw_class = (course.class_name or "").strip()
        if key is not None and key in rosters:
            expected = set(rosters[key])
        elif raw_class and raw_class in rosters:
            expected = set(rosters[raw_class])
        elif from_history:
            expected = {
                r[0]
                for r in db.execute(
                    select(StudentCourse.student_no).where(
                        StudentCourse.schedule_guid == course.schedule_guid
                    )
                ).all()
            }
        else:
            # 权威应到名单：选课名单导入建立的「学生 → 课程」关系。
            # 与 ``from_history`` 的关键区别是**事实 vs 观测**：
            # 选课关系是"谁选了这门课"，包含一次都没来过的学生 ——
            # 而他们正是本系统最该发现的人。
            expected = _enrolled(db, course.schedule_guid)

        if not expected:
            continue

        missing = expected - observed
        if not missing:
            continue
        ratio = len(missing) / max(1, len(expected))
        if ratio > _max_infer_ratio():
            skipped_ratio += 1
            log.warning(
                "课程 %s（%s）推断缺勤 %d/%d 人（%.0f%%），超过阈值已跳过，"
                "请核对班级名单；下一轮仍会重试",
                course.schedule_guid, course.course_name,
                len(missing), len(expected), ratio * 100,
            )
            continue

        for student_no in missing:
            out.append(
                NormalizedEvent(
                    source=Source.HANBOER.value,
                    schedule_guid=course.schedule_guid,
                    student_no=student_no,
                    class_date=course.class_date,
                    attend_status=AttendStatus.ABSENT.value,
                    sign_in_time=None,
                    node_no=course.node_name,
                    classroom_no=course.classroom_no,
                    user_type=2,
                    raw={"inferred": True, "reason": "应到名册有该生但考勤无记录"},
                )
            )

    if skipped_ratio:
        log.warning("本轮有 %d 门课因缺勤比例异常被跳过推断", skipped_ratio)
    if skipped_uncovered:
        log.info(
            "有 %d 门课没有考勤源覆盖，已跳过缺勤推断（教务名单里的课不等于能考勤的课）",
            skipped_uncovered,
        )
    if out:
        log.info("推断补齐 %d 条缺勤记录", len(out))
    return out


def _is_covered(course: Course) -> bool:
    """这门课能不能看到"来没来"。

    ``Course.covered`` 由导入端维护：翰博尔来的课为 ``True``，
    教务名单建立的课（拿不到录播计划）为 ``False``。
    默认 ``True`` 保证老数据行为不变。
    """
    return bool(getattr(course, "covered", True))


def _enrolled(db: Session, schedule_guid: str) -> set[str]:
    """该节课的**权威应到名单**：谁选了这门课（来自选课名单导入）。

    与"历史上观测到上过该课的人"的关键区别：这是**事实**，包含那些
    一次都没来过的学生 —— 而他们正是本系统最该发现的人。
    """
    return {
        r[0]
        for r in db.execute(
            select(StudentCourse.student_no)
            .join(Student, Student.student_no == StudentCourse.student_no)
            .where(
                StudentCourse.schedule_guid == schedule_guid,
                Student.active.is_(True),
            )
        ).all()
    }


def class_rosters(db: Session) -> dict:
    """构建班级应到名册，两种键都放进去：

    - **归一化键** ``(年份, 专业, 班序号)``：跨系统写法差异靠它对上
      （教务「2024数字媒体技术（中外合作办学）1班」↔ 翰博尔「数字媒体技术241班」）；
    - **原始班名**：兼容「智能241班」这种折不出专业全名的写法。

    两者指向同一批学号，取用时先试归一化键、再退原始键。
    """
    from app.services.roster import class_alias_map, class_key

    aliases = class_alias_map(db)
    rosters: dict = defaultdict(set)
    for class_name, student_no in db.execute(
        select(Student.class_name, Student.student_no).where(Student.active.is_(True))
    ).all():
        if not class_name:
            continue
        rosters[class_name].add(student_no)
        key = class_key(class_name)
        if key is None:
            continue
        mapped = aliases.get(str(key))
        rosters[_as_key(mapped) or key].add(student_no)
    return dict(rosters)


def _as_key(text: str | None) -> tuple[str, str, str] | None:
    """把别名表里存的 ``"('2024', '数字媒体技术', '1')"`` 还原成元组。"""
    if not text:
        return None
    import ast

    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, tuple) and len(value) == 3:
        return tuple(str(v) for v in value)  # type: ignore[return-value]
    return None


def rosters_from_events(db: Session) -> dict[str, set[str]]:
    """从已观测的考勤数据反推班级成员（无官方名单时的近似）。"""
    rosters: dict[str, set[str]] = defaultdict(set)
    rows = db.execute(
        select(Course.class_name, Course.schedule_guid).where(Course.class_name.is_not(None))
    ).all()
    guid_to_class = {r[1]: r[0] for r in rows if r[0]}
    if not guid_to_class:
        return {}
    for guid, student_no in db.execute(
        select(StudentCourse.schedule_guid, StudentCourse.student_no)
    ).all():
        klass = guid_to_class.get(guid)
        if klass:
            rosters[klass].add(student_no)
    return dict(rosters)


def reconcile_absentees(
    db: Session,
    now: datetime | None = None,
    hours: int = 24,
    from_history: bool = False,
) -> list[NormalizedEvent]:
    """独立的补齐入口：对最近已结束的课程，用应到名册反推缺勤。

    由采集流程在**写入本批事件之后**调用，这样 ``_observed()`` 看到的是
    包含本批数据在内的最新状态。

    这一点很关键：整节课没来的学生不会出现在接口返回里，因此
    「本批 events 为空」恰恰是最需要补齐的情形 —— 早期版本把补齐逻辑
    挂在 ``if events:`` 之下，导致这类学生永远查无此人。
    """
    courses = pending_inference_courses(db, now=now, hours=hours)
    if not courses:
        return []
    return infer_absentees(db, courses, now=now, from_history=from_history)


def roster_coverage(db: Session) -> tuple[int, int]:
    """返回 (有班级归属的在读学生数, 在读学生总数)，用于提示名单缺失。"""
    total = db.execute(
        select(func.count()).select_from(Student).where(Student.active.is_(True))
    ).scalar_one()
    with_class = db.execute(
        select(func.count())
        .select_from(Student)
        .where(Student.active.is_(True), Student.class_name != "")
    ).scalar_one()
    return int(with_class), int(total)


def pending_inference_courses(db: Session, now: datetime | None = None, hours: int = 24) -> list[Course]:
    """最近结束、可以下结论的课程。"""
    now = now or datetime.now()
    stmt = (
        select(Course)
        .where(
            Course.end_time.is_not(None),
            Course.end_time <= now,
            Course.end_time >= now - timedelta(hours=hours),
            Course.class_date.is_not(None),
            # 没有考勤源覆盖的课不参与推断：我们看不到它的出勤，
            # "应到 - 已观测" 恒等于"应到"，推出来就是全员缺勤。
            Course.covered.is_(True),
        )
        .order_by(Course.end_time.desc())
        .limit(500)
    )
    return list(db.execute(stmt).scalars())
