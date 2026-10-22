"""考勤判定状态机（纯函数，便于单测与回归）。

术语
----
- **一节课 (session)**：一条录播计划 `scheduleGuid`，有开始/结束时间。
- **有课日 (class day)**：某学生当天至少有一节课（按学生本人课表算，不是全校日历）。
- **整日无签到 (all missed)**：当天该生的**所有已结束**课程都没签到，且没有任何签到记录。
  若某节课根本没有考勤记录（可能延迟上传），该课不计入「已结束的判定依据」。

判定优先级（高 → 低）
--------------------
1. `严重异常`：最近连续 N（默认 3）个有课日均为「整日无签到」
2. `异常`：最近一个**已完成**的有课日为「整日无签到」
3. `今日未签到`：今日有课且尚未签到（但还不能定性为异常）
4. `活跃`：最近一个有课日至少签到过一次
5. `无课`：最近一个有课日之前没有任何课
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

from app.core.config import settings
from app.core.enums import AttendStatus, SessionState, StudentState


@dataclass(slots=True)
class SessionFact:
    """某学生某节课的签到事实（由 AttendanceEvent 聚合而来）。"""

    schedule_guid: str
    class_date: date
    start_time: datetime | None = None
    end_time: datetime | None = None
    present: bool = False
    leave: bool = False
    absent_recorded: bool = False
    sign_in_time: datetime | None = None
    course_name: str = ""
    node_name: str = ""
    classroom_name: str = ""
    #: 这节课**有没有考勤源覆盖**。``False`` = 我们根本看不到这间教室
    #: （录播系统只装了部分教室），此时"没有签到"不能当缺勤。
    #:
    #: 为什么是必备字段：教务选课名单录进来之后，名单里有、翰博尔看不到的课
    #: 会被 ``_load_session_facts`` 补成"无记录课时"，而「无记录」在状态机里
    #: 就是 ``MISSED`` —— 上千名学生会被集体误报成严重异常。
    #: 默认 ``True`` 是刻意的：老数据与按事件来的课都当作有覆盖，行为不变。
    covered: bool = True


@dataclass(slots=True)
class DaySummary:
    """「学生 × 日期」的判定结果。"""

    class_date: date
    session_count: int = 0
    present_count: int = 0
    missed_count: int = 0
    pending_count: int = 0
    leave_count: int = 0
    day_complete: bool = False
    all_missed: bool = False
    last_sign_in_time: datetime | None = None
    #: 当天有多少节课**没有考勤源覆盖**（看不到"来没来"）。
    #: 这些课不倒扣，但页面要能显示"这天有 N 节课我们看不到" ——
    #: 否示示例辅导员会以为学生那天只上了 2 节课。
    no_source_count: int = 0

    @property
    def has_class(self) -> bool:
        return self.session_count > 0

    @property
    def has_signed(self) -> bool:
        return self.present_count > 0


@dataclass(slots=True)
class DayEvaluation:
    """按「学生 × 日期」聚合后的判定输入（不需要逐节课明细）。

    为什么需要它：总览页面要一次算 1200 名学生，若按「每节课一行」装载，
    60 天窗口就是 15.8 万行对象，实测约 4.6 秒。按天聚合后降到约 3.6 万行，
    且这些字段全部可以在 SQL 里算出来。

    注意与 :class:`DaySummary` 的区别：``DaySummary`` 是 Python 侧逐节课判定
    的结果；本结构是**已在 SQL 侧聚合**的输入，``pending_count`` 表示尚未结束的
    课时数，``missed_count`` 表示已结束但未签到的课时数。

    ``no_source_count``：其中**没有考勤源覆盖**的课时数（看不到"来没来"）。
    与 :class:`DaySummary` 同名同义，供页面显示"这天有几节课我们看不到"。
    """

    class_date: date
    session_count: int = 0
    present_count: int = 0
    missed_count: int = 0
    pending_count: int = 0
    leave_count: int = 0
    day_complete: bool = False
    all_missed: bool = False
    last_sign_in_time: datetime | None = None
    no_source_count: int = 0

    @property
    def has_class(self) -> bool:
        return self.session_count > 0

    @property
    def has_signed(self) -> bool:
        return self.present_count > 0


@dataclass(slots=True)
class Verdict:
    """最终判定结论。"""

    state: StudentState
    reason: str
    consecutive_missed_days: int = 0
    last_class_date: date | None = None
    last_sign_in_time: datetime | None = None
    missed_dates: list[date] = field(default_factory=list)
    #: 该学生是否示示例辅导员的手动标记。与当前状态**无关**：标记之后重新异常时仍为 True，
    #: 页面要能同时显示「异常」和「已示示例辅导员标记过」。
    manual_marked: bool = False
    manual_marked_on: date | None = None


@dataclass(slots=True)
class ManualMark:
    """老师的手动标记。"""

    #: 标记发生在哪一天
    marked_on: date
    #: 老师断言的状态，目前只有「活跃」
    state: str = "活跃"
    reason: str = ""
    by: str = ""


def split_at_mark(
    class_days: Mapping[date, object],
    mark: ManualMark | None,
) -> dict[date, object]:
    """示示例辅导员的手动标记，切掉「标记日及以前」的有课日。

    规则（老师原话）：**「标记后，视作一次没有课程的签到」**。

    也就是这次标记相当于学生**刚刚签到过一次**（只不过那天没课），于是：

    - 它把标记之前的连续无签到**全部清零** —— 那示示例辅导员已经处理过了；
    - 它**不赠送任何豁免** —— 从第二天起照常判定：
      标记完第二天仍然没签到就继续报异常，连续三个有课日没签到就继续报严重异常。

    上一版是"豁免 3 个有课日"：标记后 3 天内一直显示活跃，
    即便学生连着旷课也不报 —— 老师明确要求改成现在这样。
    """
    if mark is None:
        return dict(class_days)
    return {d: s for d, s in class_days.items() if d > mark.marked_on}


# ─────────────────────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────────────────────
def _is_present(status: AttendStatus) -> bool:
    return status.value in settings.present_status_set


def _leave_is_present() -> bool:
    return bool(settings.leave_counts_as_present)


def classify_session(session: SessionFact, now: datetime) -> SessionState:
    """判定「某学生某节课」的签到结论。

    时间优先：课还没开/还在上，即使接口已经预置了「缺勤」字样，也不能
    定性为旷课 —— 否则「连续三天异常」会被尚未结束的课误触发。
    """
    if session.present or session.sign_in_time is not None:
        return SessionState.PRESENT
    if session.leave and _leave_is_present():
        return SessionState.LEAVE
    if session.end_time is not None and now < session.end_time:
        # 课尚未结束：未开始 / 进行中，都不能下结论
        return (
            SessionState.UPCOMING
            if session.start_time is not None and now < session.start_time
            else SessionState.PENDING
        )
    # 课已结束：有签到 -> 已签到；否则看是否有明确的缺勤记录
    if session.leave:
        return SessionState.LEAVE
    if session.absent_recorded:
        return SessionState.MISSED
    # 没有任何记录：按「已结束仍无签到」处理，但由调用方决定是否计入连续异常
    return SessionState.MISSED


def summarize_day(
    class_date: date,
    sessions: list[SessionFact],
    now: datetime,
) -> DaySummary:
    """把一天内的若干节课汇总成 DaySummary。

    没有考勤源覆盖的课（``covered=False``）单独计数，**不参与缺勤判定**：
    我们连这节课有没有人都看不到，"没人签到"无从谈起。但它仍然算
    ``session_count`` —— 「这天有课」是事实，只是我们答不了"来没来"。
    """
    summary = DaySummary(class_date=class_date, session_count=len(sessions))
    last_sign_in: datetime | None = None

    for s in sessions:
        state = classify_session(s, now)
        if not s.covered:
            # 看不到的课：签到/请假仍照实记（有数据就是有数据），
            # 但"没记录"绝不升级成缺勤。
            summary.no_source_count += 1
            if state is SessionState.PRESENT:
                summary.present_count += 1
                if s.sign_in_time is not None:
                    if last_sign_in is None or s.sign_in_time > last_sign_in:
                        last_sign_in = s.sign_in_time
            elif state is SessionState.LEAVE:
                summary.leave_count += 1
            continue
        if state is SessionState.PRESENT:
            summary.present_count += 1
            if s.sign_in_time is not None:
                if last_sign_in is None or s.sign_in_time > last_sign_in:
                    last_sign_in = s.sign_in_time
        elif state is SessionState.LEAVE:
            summary.leave_count += 1
        elif state is SessionState.PENDING:
            summary.pending_count += 1
        elif state is SessionState.UPCOMING:
            # 尚未开始的课同样代表「这一天还没结束」，必须计入 pending，
            # 否则「今天只有一节晚课」会被误判成整日无签到。
            summary.pending_count += 1
        elif state is SessionState.MISSED:
            summary.missed_count += 1

    # 该日是否已经可以下结论：不存在未开始的课，且不存在尚未结束的课
    concluded = summary.pending_count == 0
    summary.day_complete = concluded
    # 「整日无签到」：当天有课、课已全部结束、一次签到都没有、也没有请假。
    # 这里必须排除"只有看不到的课"的情况 —— 那天我们没有任何可用于判定的
    # 考勤数据，判成缺勤等于凭空指控。
    judged = summary.session_count - summary.no_source_count
    summary.all_missed = (
        judged > 0
        and concluded
        and summary.present_count == 0
        and summary.leave_count == 0
    )
    summary.last_sign_in_time = last_sign_in
    return summary


def summarize_student(
    sessions: list[SessionFact],
    now: datetime,
) -> dict[date, DaySummary]:
    """按日期分组汇总某学生的全部课程。"""
    by_date: dict[date, list[SessionFact]] = defaultdict(list)
    for s in sessions:
        by_date[s.class_date].append(s)
    return {d: summarize_day(d, ss, now) for d, ss in by_date.items()}


# ─────────────────────────────────────────────────────────────
# 主判定
# ─────────────────────────────────────────────────────────────
def _nothing_signed(summary: DaySummary) -> bool:
    """该日有课、且至今一次签到都没有（也没有请假）。

    与 ``DaySummary.all_missed`` 的区别：不要求当天课程已全部结束，
    因此「进行中且无人签到」的今天也能被识别（供 count_in_progress_day 使用）。
    """
    return (
        summary.session_count > 0
        and summary.present_count == 0
        and summary.leave_count == 0
    )


def _consecutive_all_missed_days(
    class_days: dict[date, DaySummary],
    today: date,
    now: datetime,
) -> tuple[int, list[date]]:
    """连续「整日无签到」的有课日天数。

    从**最近一个已结束的有课日**（锚点）往回数，遇到有签到/请假的日子即中断。

    两个要点：
    - 今日课程尚未全部结束时，今天既不能计入、也不应中断连续（它还没证明自己能打破连续）。
      由 ``settings.count_in_progress_day`` 决定是否强行把今天算作一天。
    - 中间若某天课程仍未结束（数据延迟），跳过该天、不中断计数。
    """
    ordered = sorted(class_days.keys(), reverse=True)
    if not ordered:
        return 0, []

    start_idx = 0
    today_summary = class_days.get(today)
    if today_summary is not None and not today_summary.day_complete:
        if settings.count_in_progress_day and _nothing_signed(today_summary):
            start_idx = -1  # 把今天算作连续的第 1 天，从今天开始数
        else:
            start_idx = 1  # 跳过今天，从上一个有课日锚定

    streak = 0
    involved: list[date] = []
    stopped = False

    if start_idx == -1:
        # 明确要求把「今天」计入：今天必须确实一次签到都没有，连续才成立
        if not _nothing_signed(today_summary):
            return 0, []
        streak = 1
        involved.append(today)
        streak = 1
        involved.append(today)

    idx = 1 if start_idx == -1 else start_idx
    while idx < len(ordered) and not stopped:
        d = ordered[idx]
        idx += 1
        summary = class_days[d]
        if not summary.day_complete:
            # 今天还没结束 / 数据未齐：跳过（不计入，也不打断既有的连续）
            continue
        if summary.all_missed:
            streak += 1
            involved.append(d)
        else:
            stopped = True

    # 锚点日（最近一个已结束的有课日）本身有签到 → 不存在任何连续异常
    if streak == 0:
        return 0, []

    return streak, involved


def _apply_mark(
    class_days: Mapping[date, object],
    mark: ManualMark | None,
) -> tuple[dict[date, object], bool]:
    """按手动标记裁剪有课日。

    返回 ``(裁剪后的有课日, 是否已无日子可判)``。

    「标记后视作一次没有课程的签到」：标记把之前的一切清零，
    但**不给任何豁免**。所以裁掉标记日及以前之后，直接按正常规则判。
    """
    if mark is None:
        return dict(class_days), False
    kept = split_at_mark(class_days, mark)
    return kept, not kept


def _mark_only_verdict(mark: ManualMark) -> Verdict:
    """标记之后一个可判定的有课日都没有 —— 视作刚签到过，判为活跃。

    两种情形都会走到这里：学生本来就没有任何课程记录，
    以及标记之后还没上过课。两种情况示示例辅导员的意思都是"这人没问题"。
    """
    return Verdict(
        state=StudentState.ACTIVE,
        reason=f"老师于 {mark.marked_on:%Y-%m-%d} 手动标记为活跃（视作一次签到）",
        manual_marked=True,
        manual_marked_on=mark.marked_on,
    )


def _evaluate_impl(
    sessions: list[SessionFact],
    now: datetime | None = None,
    *,
    today: date | None = None,
    mark: ManualMark | None = None,
) -> Verdict:
    """对某学生做出最终状态判定。"""
    now = now or datetime.now()
    today = today or now.date()

    if not sessions:
        if mark is not None:
            return _mark_only_verdict(mark)
        return Verdict(state=StudentState.NO_CLASS, reason="暂无任何课程记录")

    daily = summarize_student(sessions, now)
    class_days = {d: s for d, s in daily.items() if s.has_class}
    class_days, empty_after_mark = _apply_mark(class_days, mark)
    if empty_after_mark:
        return _mark_only_verdict(mark)
    if not class_days:
        return Verdict(state=StudentState.NO_CLASS, reason="暂无有课记录")

    ordered = sorted(class_days.keys(), reverse=True)
    last_class_date = ordered[0]
    last_day = class_days[last_class_date]

    # 「最近一个已下结论的有课日」：今天的课还在上时，它可能是昨天。
    # 异常判定必须看这一天，否则进行中的今日会掩盖昨天的整日无签到。
    anchor_date: date | None = None
    for d in ordered:
        if class_days[d].day_complete:
            anchor_date = d
            break
    anchor_day = class_days[anchor_date] if anchor_date is not None else None

    # 最近一次签到时间：取全局最晚
    last_sign_in: datetime | None = None
    for s in daily.values():
        if s.last_sign_in_time and (last_sign_in is None or s.last_sign_in_time > last_sign_in):
            last_sign_in = s.last_sign_in_time

    streak, involved = _consecutive_all_missed_days(class_days, today, now)

    # ── 1. 严重异常：连续 N 个有课日整日无签到 ──
    threshold = max(2, int(settings.severe_consecutive_days))
    if streak >= threshold:
        return Verdict(
            state=StudentState.SEVERE,
            reason=f"最近连续 {streak} 个有课日均整日无签到",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
            missed_dates=involved,
        )

    # ── 2. 异常：最近一个已下结论的有课日整日无签到 ──
    if anchor_day is not None and anchor_day.all_missed:
        return Verdict(
            state=StudentState.ABNORMAL,
            reason=f"{anchor_date:%Y-%m-%d} 有课但整日无签到",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
            missed_dates=involved or [anchor_date],
        )

    # ── 3. 今日未签到：今日有课、尚无签到，但还没结束（不能算异常） ──
    # 顺序很关键：必须排在「异常」之后 —— 今日课程尚在进行中，
    # 不能掩盖昨天/前几天的整日无签到。
    today_summary = class_days.get(today)
    if today_summary is not None and today_summary.present_count == 0:
        if today_summary.pending_count > 0 or not today_summary.day_complete:
            return Verdict(
                state=StudentState.TODAY_NOT_SIGNED,
                reason="今日有课，尚未产生签到",
                consecutive_missed_days=streak,
                last_class_date=last_class_date,
                last_sign_in_time=last_sign_in,
            )

    # ── 4. 活跃：最近一个有课日签到过 ──
    if last_day.present_count > 0:
        return Verdict(
            state=StudentState.ACTIVE,
            reason=f"{last_class_date:%Y-%m-%d} 有课且已签到",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
        )

    # ── 5. 兜底：最近一个有课日请假等 ──
    if last_day.leave_count > 0:
        return Verdict(
            state=StudentState.ACTIVE,
            reason=f"{last_class_date:%Y-%m-%d} 有课（请假）",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
        )

    return Verdict(
        state=StudentState.NO_CLASS,
        reason="最近无有效课堂记录",
        consecutive_missed_days=streak,
        last_class_date=last_class_date,
        last_sign_in_time=last_sign_in,
    )


def _evaluate_from_days_impl(
    days: list["DayEvaluation"],
    now: datetime | None = None,
    *,
    today: date | None = None,
    mark: ManualMark | None = None,
) -> Verdict:
    """与 :func:`evaluate` 同规则，但输入是**已按天聚合**的数据。

    仅供总览等"大批量只读展示"场景使用；逐节课明细走 :func:`evaluate`。
    两条路径共用 `_consecutive_all_missed_days`，规则不会漂移。
    """
    now = now or datetime.now()
    today = today or now.date()

    if not days:
        if mark is not None:
            return _mark_only_verdict(mark)
        return Verdict(state=StudentState.NO_CLASS, reason="暂无任何课程记录")

    class_days = {d.class_date: d for d in days if d.has_class}
    class_days, empty_after_mark = _apply_mark(class_days, mark)
    if empty_after_mark:
        return _mark_only_verdict(mark)
    if not class_days:
        return Verdict(state=StudentState.NO_CLASS, reason="暂无有课记录")

    ordered = sorted(class_days.keys(), reverse=True)
    last_class_date = ordered[0]
    last_day = class_days[last_class_date]

    anchor_date: date | None = None
    for d in ordered:
        if class_days[d].day_complete:
            anchor_date = d
            break
    anchor_day = class_days[anchor_date] if anchor_date is not None else None

    last_sign_in: datetime | None = None
    for d in days:
        if d.last_sign_in_time and (
            last_sign_in is None or d.last_sign_in_time > last_sign_in
        ):
            last_sign_in = d.last_sign_in_time

    streak, involved = _consecutive_all_missed_days(class_days, today, now)

    threshold = max(2, int(settings.severe_consecutive_days))
    if streak >= threshold:
        return Verdict(
            state=StudentState.SEVERE,
            reason=f"最近连续 {streak} 个有课日均整日无签到",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
            missed_dates=involved,
        )

    if anchor_day is not None and anchor_day.all_missed:
        return Verdict(
            state=StudentState.ABNORMAL,
            reason=f"{anchor_date:%Y-%m-%d} 有课但整日无签到",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
            missed_dates=involved or [anchor_date],
        )

    today_summary = class_days.get(today)
    if today_summary is not None and today_summary.present_count == 0:
        if today_summary.pending_count > 0 or not today_summary.day_complete:
            return Verdict(
                state=StudentState.TODAY_NOT_SIGNED,
                reason="今日有课，尚未产生签到",
                consecutive_missed_days=streak,
                last_class_date=last_class_date,
                last_sign_in_time=last_sign_in,
            )

    if last_day.present_count > 0:
        return Verdict(
            state=StudentState.ACTIVE,
            reason=f"{last_class_date:%Y-%m-%d} 有课且已签到",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
        )

    if last_day.leave_count > 0:
        return Verdict(
            state=StudentState.ACTIVE,
            reason=f"{last_class_date:%Y-%m-%d} 有课（请假）",
            consecutive_missed_days=streak,
            last_class_date=last_class_date,
            last_sign_in_time=last_sign_in,
        )

    return Verdict(
        state=StudentState.NO_CLASS,
        reason="最近无有效课堂记录",
        consecutive_missed_days=streak,
        last_class_date=last_class_date,
        last_sign_in_time=last_sign_in,
    )


def week_range(day: date, week_start_monday: bool = True) -> tuple[date, date]:
    """返回 day 所在自然周的 (周一, 周日)。"""
    weekday = day.weekday()  # 周一=0
    start = day - timedelta(days=weekday if week_start_monday else (weekday + 1) % 7)
    return start, start + timedelta(days=6)

# ─────────────────────────────────────────────────────────────
# 对外入口：内部实现 + 「手动标记」盖章
# ─────────────────────────────────────────────────────────────
def _stamp_mark(verdict: Verdict, mark: ManualMark | None) -> Verdict:
    """把「这名学生示示例辅导员标记过」盖到结论上。

    只补充标记信息，**不改状态** —— 状态由正常规则算出来。
    这样页面可以同时显示「异常」和「已示示例辅导员标记过」，
    老师一眼看出"这个我标过，但之后又没签到了"。
    """
    if mark is not None:
        verdict.manual_marked = True
        verdict.manual_marked_on = mark.marked_on
    return verdict


def evaluate(
    sessions: list[SessionFact],
    now: datetime | None = None,
    *,
    today: date | None = None,
    mark: ManualMark | None = None,
) -> Verdict:
    """对某学生做出最终状态判定（逐节课明细路径）。"""
    return _stamp_mark(
        _evaluate_impl(sessions, now, today=today, mark=mark), mark
    )


def evaluate_from_days(
    days: list["DayEvaluation"],
    now: datetime | None = None,
    *,
    today: date | None = None,
    mark: ManualMark | None = None,
) -> Verdict:
    """对某学生做出最终状态判定（按天聚合路径）。"""
    return _stamp_mark(
        _evaluate_from_days_impl(days, now, today=today, mark=mark), mark
    )
