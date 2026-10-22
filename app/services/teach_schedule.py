# -*- coding: utf-8 -*-
"""教务课表：把「上课时间地点」解析成可判定的课次。

为什么需要这个模块
-----------------
选课名单（``import enrollments``）只回答 **谁选了哪门课**，不回答 **哪天上课**。
没有"哪天"，那些课就进不了周课表、也进不了每日判定 —— 老师打开某个学生
看到的仍然只有录播系统覆盖的那几门课。

教务处导出的课程表里有这一列：:

    5-7周,9-17周 星期四 7-8节 A402,5-7周,9-14周 星期五 7-8节 A402

本模块把它解析成「星期几 + 第几节 + 教室 + 周次」，再把周次展开成具体日期。

两个实测踩点
------------
1. **一格里的多段排课是用空格分隔的，逗号是"周次"内部的分隔符**
   （``1-2周,5-7周,9-16周 星期五 3-4节 A402,2周 星期日 3-4节 A402``）。
   按逗号切会把一段排课切碎成好几块，节次全都解析不出来。
2. **同名课程要合并成一行**：本院有两个「国际背景下的研究」
   （课序号 01 / 01S01），但**选课名单里根本没有课序号这一列** ——
   按课序号分行只会让同名课程出现两行、选课关系却只能挂在一行上。
   所以按课程名建课程行，多段排课取**最早的一段**作为显示用课次，
   完整周次原样存进 ``week_name`` 便于人工核对。

关于周次口径
------------
第 1 周从哪天算各校不同，用 :data:`SEMESTER_WEEK1_MONDAY` 配置
（``SEMESTER_WEEK1_MONDAY``），默认 2026-09-07。**不写死在代码里** ——
"开学日"每学期都会变，写死就会静默算错日期。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_conf import get_logger
from app.models import Course

log = get_logger("service.teach_schedule")


# ─────────────────────────────────────────────────────────────
# 节次 → 时刻
# ─────────────────────────────────────────────────────────────
# 从翰博尔已有的课表反推（``courses.node_name`` ↔ 起止时间，实测自洽）：
#   第一二节 08:00-09:50   第三四节 10:10-12:00   第五六节 14:00-15:40
#   第七八节 15:50-17:30   第九十节 18:30-20:10
#
# ⚠ 不能"每节 50 分钟、起点相加"来算：第 4 节的结束是 **12:00** 而不是
# 11:00+50=11:50（1-4 节实测 08:00-12:00）。每节的起止都按实测值写死，
# 段与段之间的课间（09:50-10:10、15:40-15:50、17:30-18:30）也照实体现。
_PERIOD_RANGE: dict[int, tuple[time, time]] = {
    1: (time(8, 0), time(8, 50)),
    2: (time(8, 50), time(9, 50)),
    3: (time(10, 10), time(11, 0)),
    4: (time(11, 0), time(12, 0)),
    5: (time(14, 0), time(14, 50)),
    6: (time(14, 50), time(15, 40)),
    7: (time(15, 50), time(16, 40)),
    8: (time(16, 40), time(17, 30)),
    9: (time(18, 30), time(19, 20)),
    10: (time(19, 20), time(20, 10)),
    11: (time(20, 20), time(21, 10)),
    12: (time(21, 10), time(22, 0)),
}


def period_time(period: int) -> tuple[time, time] | None:
    """第 N 节的 (开始, 结束)。超出已知范围返回 ``None``（不瞎猜）。"""
    return _PERIOD_RANGE.get(period)


def period_span(text: str) -> tuple[int, int] | None:
    """``"7-8"`` → ``(7, 8)``；``"5"`` → ``(5, 5)``。"""
    m = re.fullmatch(r"\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*", text or "")
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if not (1 <= a <= 12 and 1 <= b <= 12 and a <= b):
        return None
    return a, b


def slot_times(node: str) -> tuple[time, time] | None:
    """节次区间 → (开始, 结束)。``"1-4"`` → 08:00-12:00。"""
    span = period_span(node)
    if span is None:
        return None
    first, last = span
    head = period_time(first)
    tail = period_time(last)
    if head is None or tail is None:
        return None
    return head[0], tail[1]


# ─────────────────────────────────────────────────────────────
# 「上课时间地点」解析
# ─────────────────────────────────────────────────────────────
_WEEKDAY_CHARS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
#: 一段排课：``<周次>周 星期<X> <节次>节 <教室>``
#:
#: 周次部分用**重复组** ``(?:tok周[,，])*tok周`` —— 写成
#: ``tok(?:[,，]tok)*\s*周`` 会踩回溯：``周`` 落在末尾时前一段被跳过，
#: ``5-7周,9-17周`` 会解析成只剩 ``9-17周``（**静默丢掉前一段**，
#: 表现为"那天明明有课却显示无课"）。
#: 教室紧跟下一段的周次（都是非空白字符），所以不能贪婪匹配。
_MEET_RE = re.compile(
    r"(?P<weeks>(?:\d{1,2}(?:\s*-\s*\d{1,2})?\s*周\s*[,，]\s*)*"
    r"\d{1,2}(?:\s*-\s*\d{1,2})?\s*周)"
    r"\s*(?:星期|周)\s*(?P<wd>[一二三四五六日天])"
    r"\s*(?P<node>\d{1,2}(?:\s*-\s*\d{1,2})?)\s*节"
    r"\s*(?P<room>[A-Za-z0-9\-]*)"
)


@dataclass(slots=True)
class Meeting:
    """一段排课。"""

    weekday: int          # 0 = 周一
    node: str             # "7-8"
    room: str
    weeks: list[int] = field(default_factory=list)

    @property
    def times(self) -> tuple[time, time] | None:
        return slot_times(self.node)


def parse_meetings(text: str) -> list[Meeting]:
    """解析整格「上课时间地点」，返回若干段排课。

    整串扫描而不是按逗号切：逗号在**周次内部**也是分隔符
    （``1-2周,5-7周,9-16周 星期五 3-4节 A402``），按逗号切会把一段
    排课切成三块，节次全部丢失。
    """
    out: list[Meeting] = []
    for m in _MEET_RE.finditer(text or ""):
        weeks: list[int] = []
        # 捕获组里带着 ``周`` 字（重复组必须把它包进去，否则会回溯丢段），
        # 这里先去掉再按逗号切。
        raw_weeks = m.group("weeks").replace("周", "")
        for part in re.split(r"[,，]", raw_weeks):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    lo, hi = int(a), int(b)
                except ValueError:
                    continue
                if 1 <= lo <= hi <= 30:
                    weeks.extend(range(lo, hi + 1))
            else:
                try:
                    w = int(part)
                except ValueError:
                    continue
                if 1 <= w <= 30:
                    weeks.append(w)
        if not weeks:
            continue
        out.append(
            Meeting(
                weekday=_WEEKDAY_CHARS[m.group("wd")],
                node=m.group("node").replace(" ", ""),
                room=(m.group("room") or "").strip(),
                weeks=sorted(set(weeks)),
            )
        )
    return out


def meeting_dates(meeting: Meeting, week1_monday: date) -> list[date]:
    """这段排课在本学期落在哪些日期。"""
    return [
        week1_monday + timedelta(weeks=w - 1, days=meeting.weekday)
        for w in meeting.weeks
    ]


def expand_dates(meetings: list[Meeting], week1_monday: date) -> list[date]:
    """所有排课落在哪些日期（去重排序）。"""
    return sorted({d for m in meetings for d in meeting_dates(m, week1_monday)})


def weeks_label(weeks: list[int]) -> str:
    """[5,6,7,9,…,17] → ``"5-7周,9-17周"``（压缩连续段，便于人看）。

    ``周`` 要跟在**每一段**后面（教务导出就是这种写法：
    ``5-7周,9-17周``），只在末尾加一个会变成 ``5-7,9-17周``，人看着别扭。
    """
    if not weeks:
        return ""
    parts: list[str] = []
    start = prev = weeks[0]
    for w in weeks[1:]:
        if w == prev + 1:
            prev = w
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = w
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(f"{p}周" for p in parts)


# ─────────────────────────────────────────────────────────────
# 导入
# ─────────────────────────────────────────────────────────────
def course_guid_for(course_name: str, slot: tuple[int, str] | None = None) -> str:
    """课程行主键。

    **一个「星期 + 节次」时段一行** —— 实测 72 门课里 64 门每周上多次
    （最多的 18 段），把一门课压成一行、只留最早那段排课，周课表里
    这门课一周就只出现一次。数据本来是够的。

    ``slot=None`` 时是整门课的主键（考勤源那边的课可能没有课表时段）。
    确定性生成 —— 重复导入不会产生重复课程，也不会打断已有的选课关系。
    """
    base = abs(hash(course_name.strip())) & 0xFFFFFFFF
    if slot is None:
        return "tch-%08x" % base
    weekday, node = slot
    seg = re.sub(r"[^0-9A-Za-z]", "", node or "") or "x"
    return "tch-%08x-%d%s" % (base, weekday, seg)


def read_course_sheet(path: Path) -> list[list[str]]:
    """读课程表（``.xls`` / ``.xlsx`` / ``.csv``），返回行列表。"""
    if path.suffix.lower() == ".xls":
        try:
            import xlrd  # type: ignore
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise RuntimeError("读旧版 .xls 需要 xlrd：pip install xlrd") from exc
        book = xlrd.open_workbook(str(path))
        sh = book.sheet_by_index(0)
        rows: list[list[str]] = []
        for r in range(sh.nrows):
            cells = []
            for c in range(sh.ncols):
                v = sh.cell_value(r, c)
                if isinstance(v, float) and v == int(v):
                    v = int(v)
                cells.append("" if v is None else str(v).strip())
            rows.append(cells)
        return rows

    from app.services.spreadsheet import read_table

    rows, _encoding = read_table(path)
    return rows


def import_teaching_schedule(
    db: Session,
    path: Path,
    week1_monday: date,
    dry_run: bool = False,
) -> dict:
    """把教务课程表的「上课时间地点」写进课程行。

    **一个「星期 + 节次」时段一行**（见 :func:`course_guid_for` 的说明）：
    同一门课的多个教学班、多个时段会合并到同一个时段行上，
    周次取并集写进 ``week_name``。

    只补时间，不动 ``covered``：能不能看到出勤由考勤源决定，
    与教务排课无关 —— 复用已有课程行时会把原来的 ``covered`` 留着。

    导入完成后会**重挂选课关系**：把学生挂到该课程的全部时段行上，
    否则周课表只会有其中一天有课。
    """
    rows = read_course_sheet(path)
    stat = {
        "rows": 0, "matched": 0, "created": 0, "meetings": 0, "unparsed": 0,
        "with_time": 0, "courses": 0, "slots": 0, "links_created": 0,
        "links_copied": 0, "rows_pruned": 0,
    }
    if not rows:
        return stat

    header = [str(c).strip() for c in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    missing = [n for n in ("课程名", "课序号", "上课时间地点") if n not in idx]
    if missing:
        raise ValueError("课程表缺少列：%s（表头：%s）" % ("、".join(missing), header))

    i_name, i_where = idx["课程名"], idx["上课时间地点"]
    i_no = idx.get("课程号")
    i_teacher = idx.get("上课教师")
    i_org = idx.get("开课单位")

    def cell(row: list[str], i: int | None) -> str:
        return row[i].strip() if (i is not None and i < len(row)) else ""

    # ── 1. 聚合：课程名 → 时段 → {星期, 节次, 周次并集, 教室, 课程号, 教师} ──
    agg: dict[str, dict[tuple[int, str], dict]] = {}
    for row in rows[1:]:
        if not any(str(c).strip() for c in row):
            continue
        stat["rows"] += 1
        name = cell(row, i_name)
        where = cell(row, i_where)
        if not name:
            continue
        meetings = parse_meetings(where)
        if not meetings:
            stat["unparsed"] += 1
            logger.warning("课程表这行没解析出排课：%s / %s", name, where[:60])
            continue
        stat["meetings"] += len(meetings)

        slots = agg.setdefault(name, {})
        for m in meetings:
            key = (m.weekday, m.node)
            slot = slots.get(key)
            if slot is None:
                slots[key] = {
                    "weeks": set(m.weeks),
                    "room": m.room,
                    "course_no": cell(row, i_no),
                    "teacher": cell(row, i_teacher),
                    "org": cell(row, i_org),
                }
                continue
            slot["weeks"] |= set(m.weeks)
            if not slot["room"] and m.room:
                slot["room"] = m.room

    stat["courses"] = len(agg)

    # ── 2. 落库：每个时段一行 ────────────────────────────────
    wanted: set[str] = set()
    #: 课程名 → 该课程的全部时段行 guid（用于重挂选课关系）
    slots_by_name: dict[str, list[str]] = {}

    for name, slots in agg.items():
        guids: list[str] = []
        ordered = sorted(slots.items(), key=lambda kv: (kv[0][0], kv[0][1]))

        # 该课程已有的行（翰博尔建的、或早期导入建的）。
        # **认领第一段时直接复用旧行的主键，绝不删旧行** ——
        # ``student_courses.schedule_guid`` 有 ON DELETE CASCADE
        # （实测 PRAGMA foreign_keys=1），删课程行会把学生的选课关系一起带走。
        taken: set[str] = set()
        existing_rows = list(
            db.execute(select(Course).where(Course.course_name == name)).scalars()
        )

        for index, ((weekday, node), info) in enumerate(ordered):
            dates = expand_dates(
                [Meeting(weekday=weekday, node=node, room=info["room"],
                         weeks=sorted(info["weeks"]))],
                week1_monday,
            )
            if not dates:
                continue
            first_day = dates[0]
            guid = course_guid_for(name, (weekday, node))
            course = db.get(Course, guid)

            if course is None:
                # 第一段优先认领一条已有的旧行（保住 covered 与全部选课关系），
                # **认领来的行保持它自己的主键不变**，不做任何删除 ——
                # 删课程行会级联带走学生的选课关系。
                legacy = None
                if index == 0:
                    legacy = next(
                        (c for c in existing_rows if c.schedule_guid not in taken), None
                    )
                if legacy is not None:
                    course = legacy
                    stat["matched"] += 1
                else:
                    course = Course(
                        schedule_guid=guid,
                        course_name=name,
                        title=name,
                        covered=False,  # 教务排课不提供出勤
                    )
                    db.add(course)
                    stat["created"] += 1
            else:
                stat["matched"] += 1

            wanted.add(course.schedule_guid)
            taken.add(course.schedule_guid)

            if info["course_no"]:
                course.course_no = info["course_no"]
            if info["teacher"]:
                course.teacher_name = info["teacher"]
            if info["org"]:
                course.org_name = info["org"]
            if info["room"]:
                course.classroom_name = info["room"]
            course.node_name = node
            course.week_name = weeks_label(sorted(info["weeks"]))
            course.class_date = first_day
            times = slot_times(node)
            if times:
                course.start_time = datetime.combine(first_day, times[0])
                course.end_time = datetime.combine(first_day, times[1])
                stat["with_time"] += 1
            # 记**这一行真实的主键**，不是上面算出来的 guid ——
            # 第一段可能是"认领"来的旧行，它的主键与算出来的不同，
            # 记错会让后面的选课关系重挂指向一个不存在的课程行（外键报错）。
            guids.append(course.schedule_guid)
        slots_by_name[name] = guids
        stat["slots"] += len(guids)

    if dry_run:
        db.rollback()
        return stat

    db.flush()
    stat["links_created"] = _relink_slots(db, slots_by_name)
    stat["rows_pruned"] = _prune_stale_rows(db, wanted)
    db.commit()
    log.info(
        "教务课表导入：%d 行 → %d 门课 / %d 个时段（新建 %d、匹配 %d），"
        "重挂选课关系 %d 条，清理旧行 %d 条",
        stat["rows"], stat["courses"], stat["slots"], stat["created"],
        stat["matched"], stat["links_created"], stat["rows_pruned"],
    )
    return stat


def _restore_links_from_original(db: Session, course_name: str, new_guid: str) -> int:
    """课程行换主键后，把选课关系**重新挂回**。

    为什么需要"恢复"而不是"复制"：``student_courses.schedule_guid`` 有
    ``ON DELETE CASCADE``，旧课程行一删，挂在它上面的关系就没了 ——
    所以只能从"谁选了这门课"这个事实重新推导。

    推导来源，按可信度：
      1. 考勤事件（``attendance_events``）—— 谁真的来上过这门课；
      2. 同一门课**其它时段行**上的关系 —— 同班同学一起选的课。

    两者都没有时返回 0（不会凭空给人挂课）。
    """
    from app.models import AttendanceEvent, StudentCourse

    found: set[str] = {
        r[0]
        for r in db.execute(
            select(AttendanceEvent.student_no)
            .join(Course, Course.schedule_guid == AttendanceEvent.schedule_guid)
            .where(Course.course_name == course_name)
            .distinct()
        ).all()
    }
    if not found:
        found = {
            r[0]
            for r in db.execute(
                select(StudentCourse.student_no)
                .join(Course, Course.schedule_guid == StudentCourse.schedule_guid)
                .where(Course.course_name == course_name)
                .distinct()
            ).all()
        }
    if not found:
        return 0

    existing = {
        r[0]
        for r in db.execute(
            select(StudentCourse.student_no).where(
                StudentCourse.schedule_guid == new_guid
            )
        ).all()
    }
    added = 0
    for student_no in sorted(found):
        if student_no in existing:
            continue
        db.add(
            StudentCourse(student_no=student_no, schedule_guid=new_guid,
                          class_date=None, source="roster")
        )
        added += 1
    db.flush()
    return added


def _relink_slots(db: Session, slots_by_name: dict[str, list[str]]) -> int:
    """把选课关系重挂到该课程的**全部时段行**上。

    为什么必须做：一门课有多个时段行之后，学生只挂在其中一行上，
    周课表就只有那一天有课 —— 又回到"一周只显示一次"。
    """
    from app.models import StudentCourse

    changed = 0
    for name, guids in slots_by_name.items():
        if not guids:
            continue
        rows = list(
            db.execute(
                select(
                    StudentCourse.id,
                    StudentCourse.student_no,
                    StudentCourse.schedule_guid,
                    StudentCourse.class_date,
                )
                .join(Course, Course.schedule_guid == StudentCourse.schedule_guid)
                .where(Course.course_name == name)
            ).all()
        )
        if not rows:
            continue
        # 该课程原本挂着关系的学号集合
        students = {r.student_no for r in rows}
        existing = {
            (r[0], r[1])
            for r in db.execute(
                select(StudentCourse.student_no, StudentCourse.schedule_guid).where(
                    StudentCourse.schedule_guid.in_(guids)
                )
            ).all()
        }
        target = set(guids)
        for student_no in sorted(students):
            for guid in sorted(target):
                if (student_no, guid) in existing:
                    continue
                db.add(
                    StudentCourse(student_no=student_no, schedule_guid=guid,
                                  class_date=None, source="roster")
                )
                existing.add((student_no, guid))
                changed += 1
        # 指向该课程「非时段行」的旧关系（主键已换名或已废弃）删掉
        for sid, _no, guid, _d in rows:
            if guid not in target:
                db.delete(db.get(StudentCourse, sid))
    db.flush()
    return changed


def _prune_stale_rows(db: Session, wanted: set[str]) -> int:
    """删掉早期导入方案留下的「一门课一行」残留。

    只删 ``tch-`` 开头、且**既不在本轮 wanted 里、也没有选课关系**的行。
    有关系的行会在 :func:`_relink_slots` 里先被挂到时段行上，
    再变成空行由下一轮清掉 —— 所以这里删不到它们是正常的，不会漏数据。
    """
    from app.models import StudentCourse

    linked = {
        r[0]
        for r in db.execute(select(StudentCourse.schedule_guid).distinct()).all()
    }
    stale = [
        c.schedule_guid
        for c in db.execute(
            select(Course).where(Course.schedule_guid.like("tch-%"))
        ).scalars()
        if c.schedule_guid not in wanted and c.schedule_guid not in linked
    ]
    for guid in stale:
        row = db.get(Course, guid)
        if row is not None:
            db.delete(row)
    db.flush()
    return len(stale)
