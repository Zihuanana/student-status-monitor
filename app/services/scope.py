# -*- coding: utf-8 -*-
"""校区作用域过滤 —— 只保留「我们学院」的课。

为什么需要
----------
翰博尔平台是**多校区共用**的。一次 ``getAttendanceStatistics`` 会把这套平台里
所有校区的课全部返回，接口本身**没有**校区/院系过滤参数（已实测：``orgName``、
``className``、``buildingName``、``orgId``、``schoolName`` 传了都被忽略）。

实测 cuc.smartclass.cn 近 31 天 742 门课：

===========  ========  ==========================================
范围          课程数     楼栋
===========  ========  ==========================================
海南校区       152       C座(100) / A座(33) / B座(19)
其它校区       590       48号教学楼(564) / 1号教学楼(26)
===========  ========  ==========================================

两拨学生的学号**零重叠**，所以必须在这里切开，否则：

- 库里会混进 7000+ 不是我们学院的学生，页面完全不可读；
- 每轮采集要抓 700 多门课的明细，白白消耗接口配额与服务器资源。

判定方式
--------
按「节次名」等文本字段做关键词匹配。海南校区的节次叫「海南第一二节」，
其它校区叫「第一二节」——这是这套平台里唯一天然带上校区的字段。
楼栋分布与节次名**完全一致**（海南课 = A/B/C 座，152 = 100+33+19），
两个信号互为交叉验证，说明这个切分是可靠的，不是巧合。

关键词通过 ``HB_CAMPUS_KEYWORDS`` 配置（逗号分隔，留空表示不过滤）。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from app.adapters.base import NormalizedCourse

#: 参与关键词匹配的字段。按「最可靠 → 最兜底」排列。
_HAYSTACK_FIELDS = (
    "node_name",
    "building_name",
    "classroom_name",
    "org_name",
    "class_name",
    "course_name",
)


def scope_keywords() -> tuple[str, ...]:
    """从配置读出作用域关键词。留空表示不做任何过滤。"""
    from app.core.config import settings

    return tuple(settings.campus_keyword_list)


def course_haystack(course) -> str:
    """把参与匹配的字段拼成一段文本。

    同时接受 ``NormalizedCourse`` 与 ``Course`` ORM 对象 —— 两者字段名一致，
    采集时用它过滤入参，清理历史脏数据时用它过滤库里已有的行。
    """
    return "|".join(str(getattr(course, f, "") or "") for f in _HAYSTACK_FIELDS)


def text_in_scope(text: str, keywords: Sequence[str] | None = None) -> bool:
    """文本是否命中作用域关键词。大小写不敏感（中文不受影响）。

    大小写不敏感是必要的：校区名在课程名里可能写成 ``Hainan`` / ``hainan``，
    而 ``海南`` 这种中文关键词做 casefold 是无损的。
    """
    kws = scope_keywords() if keywords is None else keywords
    if not kws:
        return True
    folded = (text or "").casefold()
    return any(k.casefold() in folded for k in kws)


def course_in_scope(
    course: NormalizedCourse, keywords: Sequence[str] | None = None
) -> bool:
    return text_in_scope(course_haystack(course), keywords)


# ─────────────────────────────────────────────────────────────
# 学号作用域 —— 「哪些人是我们学院的」
# ─────────────────────────────────────────────────────────────
# 上面那道闸门切的是**课程**（靠节次名里的「海南」），这里切的是**学生**。
# 为什么两道都要有：课程切分依赖上游怎么给节次起名，平台改个名就静默失效；
# 学号形态是学校定的，不随平台改版变化，是更硬的那道闸门。
def student_id_like():
    """当前配置编译出的学号正则；``None`` 表示不过滤。"""
    from app.core.config import settings

    return settings.student_id_like


def student_id_in_scope(student_no: str | None) -> bool:
    """这个学号是不是本院学生（海南国际学院）。

    匹配的是**原样学号**（可带 ``cuc`` 前缀），因此本函数在入库前调用即可，
    不依赖 ``normalize_student_no`` 是否已经跑过。

    未配置正则 / 开关关闭时一律返回 ``True`` —— 过滤功能没开就绝不拦人。
    """
    pattern = student_id_like()
    if pattern is None:
        return True
    return bool(pattern.match((student_no or "").strip()))


def filter_student_ids(items: Iterable, attr: str = "student_no"):
    """按学号作用域把 ``items`` 切成 (保留, 丢弃)。

    传入的可以是 ``NormalizedEvent`` / ``NormalizedCourse`` / ORM 行 / dict，
    统一用 ``attr`` 取值，避免每个适配器各写一遍。
    """

    def value_of(item):
        if isinstance(item, dict):
            return item.get(attr)
        return getattr(item, attr, None)

    kept: list = []
    dropped: list = []
    for item in items:
        (kept if student_id_in_scope(value_of(item)) else dropped).append(item)
    return kept, dropped


def describe_student_scope() -> str:
    """给日志/命令用的一句话描述。"""
    from app.core.config import settings

    if student_id_like() is None:
        return "（未启用，任何学号都会入库）"
    return f"{settings.hb_student_id_pattern}（前缀可带 cuc）"


def split_by_scope(
    courses: Iterable[NormalizedCourse], keywords: Sequence[str] | None = None
) -> tuple[list[NormalizedCourse], list[NormalizedCourse]]:
    """返回 (作用域内, 作用域外)。"""
    kept: list[NormalizedCourse] = []
    dropped: list[NormalizedCourse] = []
    for c in courses:
        (kept if course_in_scope(c, keywords) else dropped).append(c)
    return kept, dropped


def describe_scope(keywords: Sequence[str] | None = None) -> str:
    kws = scope_keywords() if keywords is None else keywords
    return "、".join(kws) if kws else "（未设置，全平台数据都收）"


def out_of_scope_guids(db) -> tuple[list[str], int]:
    """扫出库里**不属于**本作用域的课程。

    返回 (越界 schedule_guid 列表, 库内课程总数)。用于清理已污染的历史数据。
    未配置关键词时返回 ([], 0)，即「不做任何删除」。
    """
    from sqlalchemy import select

    from app.models import Course

    kws = scope_keywords()
    if not kws:
        return [], 0

    drop: list[str] = []
    total = 0
    for row in db.execute(select(Course)).scalars():
        total += 1
        if not text_in_scope(course_haystack(row), kws):
            drop.append(row.schedule_guid)
    return drop, total


def _chunked(items: Sequence[str], size: int = 500):
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def purge_out_of_scope(db, drop_students: bool = False) -> dict:
    """把库里已经混进来的非本校区数据删干净。

    接入真实数据时很容易先踩一次坑：作用域没配就采集，别的校区的学生全进来了。
    这个函数负责收拾现场，避免只能「清库重来」。

    删除顺序（先删子表，再删主表）：

    1. ``attendance_events`` —— schedule_guid 越界的事件
    2. ``student_courses``  —— 越界课程的选课关系
    3. ``courses``          —— 越界课程本身
    4. ``attendance_daily`` / ``alerts`` —— 只针对 ``drop_students`` 时被清掉的学生

    ``drop_students=True`` 时还会删掉「删完之后一条事件都不剩」的学生。
    默认 **False**：学生行本身无害，留着也不会显示（仪表盘是按事件驱动的），
    而误删真学生会丢姓名/班级等人工补齐的信息。
    """
    from sqlalchemy import delete, select

    from app.models import (
        Alert,
        AttendanceDaily,
        AttendanceEvent,
        Course,
        Student,
        StudentCourse,
    )

    guids, total_courses = out_of_scope_guids(db)
    result = {
        "courses_total": total_courses,
        "courses_dropped": len(guids),
        "courses_deleted": 0,
        "events_deleted": 0,
        "links_deleted": 0,
        "students_deleted": 0,
        "daily_deleted": 0,
        "alerts_deleted": 0,
    }
    if not guids:
        return result

    for part in _chunked(guids):
        result["events_deleted"] += db.execute(
            delete(AttendanceEvent).where(AttendanceEvent.schedule_guid.in_(part))
        ).rowcount or 0
        result["links_deleted"] += db.execute(
            delete(StudentCourse).where(StudentCourse.schedule_guid.in_(part))
        ).rowcount or 0
        result["courses_deleted"] += db.execute(
            delete(Course).where(Course.schedule_guid.in_(part))
        ).rowcount or 0
    db.commit()

    if drop_students:
        remaining = select(AttendanceEvent.student_no).distinct()
        orphans = [
            row[0]
            for row in db.execute(
                select(Student.student_no).where(Student.student_no.not_in(remaining))
            ).all()
        ]
        for part in _chunked(orphans):
            result["daily_deleted"] += db.execute(
                delete(AttendanceDaily).where(AttendanceDaily.student_no.in_(part))
            ).rowcount or 0
            result["alerts_deleted"] += db.execute(
                delete(Alert).where(Alert.student_no.in_(part))
            ).rowcount or 0
            result["students_deleted"] += db.execute(
                delete(Student).where(Student.student_no.in_(part))
            ).rowcount or 0
        db.commit()

    return result


# ─────────────────────────────────────────────────────────────
# 学号作用域：清理「不是我们学院」的学生
# ─────────────────────────────────────────────────────────────
def out_of_scope_students(db, students: Sequence | None = None) -> tuple[list[str], int]:
    """扫出库里学号**不符合本院形态**的学生。

    返回 (越界学号列表, 库内学生总数)。未启用学号作用域时返回 ([], 0)，
    即「不做任何删除」—— 与 ``out_of_scope_guids`` 的安全约定一致。

    ``students`` 可传入已经查好的行，避免调用方为了出报告查两遍库。
    """
    from sqlalchemy import select

    from app.models import Student

    if student_id_like() is None:
        return [], 0

    rows = list(students) if students is not None else list(db.execute(select(Student)).scalars())
    drop = [row.student_no for row in rows if not student_id_in_scope(row.student_no)]
    return drop, len(rows)


def purge_out_of_scope_students(db, dry_run: bool = False) -> dict:
    """删掉学号不符合本院形态的学生，**连同其全部考勤数据**。

    与 ``purge_out_of_scope``（按课程切）不同，这里按**人**切：课程本身不删 ——
    同一门课可能还有本院学生在上面，只有「删完某个学生之后再没人引用」的
    过期课程才会被顺手清掉。

    删除顺序（先删子表，再删主表）：

    1. ``attendance_events`` —— 该生的签到事实
    2. ``attendance_daily``  —— 该生的「学生×日期」判定缓存
    3. ``alerts``            —— 该生已产生的告警
    4. ``student_marks``     —— 该生的手动标记流水
    5. ``student_courses``   —— 该生的选课/应到关系
    6. ``students``          —— 学生行本身

    另外顺手清掉**因此变成孤儿**的课程行（本院 749 人从不引用它、
    且不是今天/未来的课）。不清的话库里会留下永远不显示、也不参与判定的
    死课程；但"今天及以后"的课要留着 —— 它可能正是本院学生下一节课，
    只是明细还没回传。
    """
    from datetime import date, timedelta

    from sqlalchemy import delete, func, select

    from app.models import (
        Alert,
        AttendanceDaily,
        AttendanceEvent,
        Course,
        Student,
        StudentCourse,
        StudentMark,
    )

    drop, total = out_of_scope_students(db)
    result = {
        "students_total": total,
        "students_dropped": len(drop),
        "students_deleted": 0,
        "events_deleted": 0,
        "daily_deleted": 0,
        "alerts_deleted": 0,
        "marks_deleted": 0,
        "links_deleted": 0,
        "courses_deleted": 0,
    }
    if not drop:
        return result

    # 受影响的课程要在删事件之前记下来，否则删完就查不到了
    affected_guids: set[str] = set()
    for part in _chunked(drop):
        affected_guids.update(
            row[0]
            for row in db.execute(
                select(AttendanceEvent.schedule_guid)
                .where(AttendanceEvent.student_no.in_(part))
                .distinct()
            ).all()
        )

    if dry_run:
        return result

    for part in _chunked(drop):
        result["events_deleted"] += db.execute(
            delete(AttendanceEvent).where(AttendanceEvent.student_no.in_(part))
        ).rowcount or 0
        result["daily_deleted"] += db.execute(
            delete(AttendanceDaily).where(AttendanceDaily.student_no.in_(part))
        ).rowcount or 0
        result["alerts_deleted"] += db.execute(
            delete(Alert).where(Alert.student_no.in_(part))
        ).rowcount or 0
        result["marks_deleted"] += db.execute(
            delete(StudentMark).where(StudentMark.student_no.in_(part))
        ).rowcount or 0
        result["links_deleted"] += db.execute(
            delete(StudentCourse).where(StudentCourse.student_no.in_(part))
        ).rowcount or 0
        result["students_deleted"] += db.execute(
            delete(Student).where(Student.student_no.in_(part))
        ).rowcount or 0
    db.commit()

    if affected_guids:
        keep_after = date.today() - timedelta(days=1)
        referenced = {
            row[0]
            for row in db.execute(
                select(AttendanceEvent.schedule_guid)
                .where(AttendanceEvent.schedule_guid.in_(list(affected_guids)))
                .distinct()
            ).all()
        }
        for part in _chunked(sorted(affected_guids)):
            result["courses_deleted"] += db.execute(
                delete(Course).where(
                    Course.schedule_guid.in_(part),
                    _not_referenced(Course, referenced),
                    func.coalesce(Course.class_date, date.min) < keep_after,
                )
            ).rowcount or 0
        db.commit()

    return result


def _not_referenced(course_model, referenced: set[str]):
    """``schedule_guid NOT IN referenced``；``referenced`` 为空时恒真。

    直接写 ``not_in([])`` 在 SQLAlchemy 里会生成恒假条件，把课全留下；
    这里显式返回一个恒真表达式，语义更清楚。
    """
    from sqlalchemy import true

    if not referenced:
        return true()
    return course_model.schedule_guid.not_in(list(referenced))


