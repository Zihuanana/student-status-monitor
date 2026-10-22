# -*- coding: utf-8 -*-
"""状态机测试：五种状态的判定、优先级、连续异常升级。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta

from app.core.config import settings
from app.core.enums import SessionState, StudentState
from app.services.attendance import (
    DaySummary,
    SessionFact,
    Verdict,
    classify_session,
    evaluate,
    summarize_day,
    week_range,
)

# 固定「现在」：2026-03-11(周三) 15:00，正处于第三节进行中
NOW = datetime(2026, 3, 11, 15, 0, 0)
TODAY = NOW.date()
# 2026-03-09 周一 / 03-10 周二 / 03-11 周三 / 03-12 周四 / 03-13 周五


def sess(
    d: date,
    guid: str = "g",
    *,
    present: bool = False,
    leave: bool = False,
    absent: bool = False,
    hour: int = 8,
    duration: int = 2,
    sign_in: datetime | None = None,
) -> SessionFact:
    start = datetime.combine(d, time(hour, 0))
    return SessionFact(
        schedule_guid=f"{guid}-{d.isoformat()}-{hour}",
        class_date=d,
        start_time=start,
        end_time=start + timedelta(hours=duration),
        present=present,
        leave=leave,
        absent_recorded=absent,
        sign_in_time=sign_in,
    )


class TestSessionClassification(unittest.TestCase):
    def test_signed_session_is_present(self) -> None:
        s = sess(TODAY, present=True, sign_in=datetime.combine(TODAY, time(8, 3)))
        self.assertIs(classify_session(s, NOW), SessionState.PRESENT)

    def test_future_session_is_upcoming(self) -> None:
        s = sess(TODAY, hour=16)
        self.assertIs(classify_session(s, NOW), SessionState.UPCOMING)

    def test_in_progress_unsigned_is_pending(self) -> None:
        s = sess(TODAY, hour=14)
        self.assertIs(classify_session(s, NOW), SessionState.PENDING)

    def test_finished_unsigned_is_missed(self) -> None:
        s = sess(TODAY, hour=8, absent=True)
        self.assertIs(classify_session(s, NOW), SessionState.MISSED)

    def test_leave_is_leave_not_missed(self) -> None:
        s = sess(TODAY, hour=8, leave=True)
        self.assertIs(classify_session(s, NOW), SessionState.LEAVE)


class TestDaySummary(unittest.TestCase):
    def test_all_missed_requires_concluded_day(self) -> None:
        day = [
            sess(TODAY, "a", hour=8, absent=True),
            sess(TODAY, "b", hour=10, absent=True),
        ]
        summary = summarize_day(TODAY, day, NOW)
        self.assertEqual(summary.session_count, 2)
        self.assertEqual(summary.missed_count, 2)
        self.assertTrue(summary.day_complete)
        self.assertTrue(summary.all_missed)

    def test_one_present_prevents_all_missed(self) -> None:
        day = [
            sess(TODAY, "a", hour=8, present=True, sign_in=datetime.combine(TODAY, time(8, 1))),
            sess(TODAY, "b", hour=10, absent=True),
        ]
        summary = summarize_day(TODAY, day, NOW)
        self.assertFalse(summary.all_missed)
        self.assertEqual(summary.present_count, 1)

    def test_leave_prevents_all_missed(self) -> None:
        day = [sess(TODAY, "a", hour=8, leave=True)]
        summary = summarize_day(TODAY, day, NOW)
        self.assertFalse(summary.all_missed)
        self.assertEqual(summary.leave_count, 1)

    def test_pending_day_is_not_concluded(self) -> None:
        day = [sess(TODAY, "a", hour=14, absent=True)]
        summary = summarize_day(TODAY, day, NOW)
        self.assertFalse(summary.day_complete)
        self.assertFalse(summary.all_missed)

    def test_last_sign_in_is_latest(self) -> None:
        day = [
            sess(TODAY, "a", hour=8, present=True, sign_in=datetime.combine(TODAY, time(8, 5))),
            sess(TODAY, "b", hour=10, present=True, sign_in=datetime.combine(TODAY, time(10, 1))),
        ]
        summary = summarize_day(TODAY, day, NOW)
        self.assertEqual(summary.last_sign_in_time, datetime.combine(TODAY, time(10, 1)))


class TestEvaluate(unittest.TestCase):
    def test_no_sessions_is_no_class(self) -> None:
        self.assertIs(evaluate([]).state, StudentState.NO_CLASS)

    def test_signed_today_is_active(self) -> None:
        day = [sess(TODAY, hour=8, present=True, sign_in=datetime.combine(TODAY, time(8, 2)))]
        v = evaluate(day, now=NOW)
        self.assertIs(v.state, StudentState.ACTIVE)
        self.assertEqual(v.last_sign_in_time, datetime.combine(TODAY, time(8, 2)))

    def test_class_today_not_signed_yet_is_today_not_signed(self) -> None:
        day = [sess(TODAY, hour=14)]  # 进行中，无签到
        v = evaluate(day, now=NOW)
        self.assertIs(v.state, StudentState.TODAY_NOT_SIGNED)

    def test_upcoming_class_today_is_today_not_signed(self) -> None:
        day = [sess(TODAY, hour=16)]
        v = evaluate(day, now=NOW)
        self.assertIs(v.state, StudentState.TODAY_NOT_SIGNED)

    def test_no_class_today_but_signed_yesterday_is_active(self) -> None:
        """按「最近一个有课日」判定：昨天签到了 → 活跃。"""
        yesterday = TODAY - timedelta(days=1)
        day = [sess(yesterday, present=True, sign_in=datetime.combine(yesterday, time(8, 1)))]
        v = evaluate(day, now=NOW)
        self.assertIs(v.state, StudentState.ACTIVE)
        self.assertEqual(v.last_class_date, yesterday)

    def test_missed_yesterday_is_abnormal(self) -> None:
        yesterday = TODAY - timedelta(days=1)
        day = [sess(yesterday, absent=True)]
        v = evaluate(day, now=NOW)
        self.assertIs(v.state, StudentState.ABNORMAL)
        self.assertEqual(v.consecutive_missed_days, 1)
        self.assertIn(yesterday, v.missed_dates)

    def test_three_consecutive_missed_days_is_severe(self) -> None:
        days = []
        for offset in (1, 2, 3):
            d = TODAY - timedelta(days=offset)
            days.append(sess(d, absent=True))
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.SEVERE)
        self.assertEqual(v.consecutive_missed_days, 3)

    def test_two_missed_days_is_only_abnormal(self) -> None:
        days = []
        for offset in (1, 2):
            d = TODAY - timedelta(days=offset)
            days.append(sess(d, absent=True))
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.ABNORMAL)
        self.assertEqual(v.consecutive_missed_days, 2)

    def test_streak_breaks_on_attendance(self) -> None:
        yesterday = TODAY - timedelta(days=1)
        days = [
            sess(yesterday, "a", absent=True),
            sess(TODAY - timedelta(days=2), "b", present=True,
                 sign_in=datetime.combine(TODAY - timedelta(days=2), time(8, 1))),
            sess(TODAY - timedelta(days=3), "c", absent=True),
        ]
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.ABNORMAL)
        self.assertEqual(v.consecutive_missed_days, 1)

    def test_recent_attendance_wins_over_old_streak(self) -> None:
        """今天已签到 → 活跃。

        「优先级」说的是同一次判定里哪个结论更靠前，而不是让历史异常
        压过今天刚发生的签到：学生今天来了，就不该还挂红灯。
        """
        days = [
            sess(TODAY, "x", hour=8, present=True, sign_in=datetime.combine(TODAY, time(8, 1))),
        ]
        for offset in (1, 2, 3):
            days.append(sess(TODAY - timedelta(days=offset), "m", absent=True))
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.ACTIVE)
        self.assertEqual(v.consecutive_missed_days, 0)

    def test_severe_still_applies_when_no_class_today(self) -> None:
        """今天无课时，连续三天的历史异常必须报出来。"""
        days = []
        for offset in (1, 2, 3):
            days.append(sess(TODAY - timedelta(days=offset), "m", absent=True))
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.SEVERE)
        self.assertEqual(v.consecutive_missed_days, 3)

    def test_multi_session_day_all_missed_counts_as_one_day(self) -> None:
        yesterday = TODAY - timedelta(days=1)
        days = [
            sess(yesterday, "a", hour=8, absent=True),
            sess(yesterday, "b", hour=10, absent=True),
            sess(TODAY - timedelta(days=2), "c", absent=True),
            sess(TODAY - timedelta(days=3), "d", absent=True),
        ]
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.SEVERE)
        self.assertEqual(v.consecutive_missed_days, 3)
        self.assertEqual(len(v.missed_dates), 3)

    def test_leave_day_does_not_count_as_missed(self) -> None:
        yesterday = TODAY - timedelta(days=1)
        days = [sess(yesterday, leave=True)]
        for offset in (2, 3, 4):
            days.append(sess(TODAY - timedelta(days=offset), absent=True))
        v = evaluate(days, now=NOW)
        # 昨天请假 → 连续中断
        self.assertIsNot(v.state, StudentState.SEVERE)

    def test_in_progress_today_excluded_from_streak_by_default(self) -> None:
        """今日课程尚未结束 → 不计入连续异常天数。

        NOW=15:00，故用 14:00 开始的课来构造「正在进行中」。
        """
        self.assertFalse(settings.count_in_progress_day)
        days = [sess(TODAY, "t", hour=14, absent=True)]
        for offset in (1, 2):
            days.append(sess(TODAY - timedelta(days=offset), "m", absent=True))
        v = evaluate(days, now=NOW)
        self.assertIs(v.state, StudentState.ABNORMAL)
        self.assertEqual(v.consecutive_missed_days, 2)

    def test_in_progress_day_can_be_counted_when_enabled(self) -> None:
        """显式开启后，今天（进行中且无签到）也计入连续天数。"""
        settings.count_in_progress_day = True
        try:
            days = [sess(TODAY, "t", hour=14, absent=True)]
            for offset in (1, 2):
                days.append(sess(TODAY - timedelta(days=offset), "m", absent=True))
            v = evaluate(days, now=NOW)
            self.assertIs(v.state, StudentState.SEVERE)
            self.assertEqual(v.consecutive_missed_days, 3)
        finally:
            settings.count_in_progress_day = False

    def test_upcoming_class_today_is_not_missed(self) -> None:
        """今天只有一节还没开始的课 → 绝不能算「整日无签到」。"""
        v = evaluate([sess(TODAY, "u", hour=16)], now=NOW)
        self.assertIs(v.state, StudentState.TODAY_NOT_SIGNED)
        self.assertEqual(v.consecutive_missed_days, 0)

    def test_weekend_gap_does_not_break_streak(self) -> None:
        """周五、周四、周三连续无签到（周末无课不应打断）。"""
        friday = date(2026, 3, 6)
        now = datetime(2026, 3, 9, 10, 0)  # 下周一
        days = [
            sess(friday, "a", absent=True),
            sess(date(2026, 3, 5), "b", absent=True),
            sess(date(2026, 3, 4), "c", absent=True),
        ]
        v = evaluate(days, now=now)
        self.assertIs(v.state, StudentState.SEVERE)
        self.assertEqual(v.consecutive_missed_days, 3)

    def test_verdict_carries_last_class_date(self) -> None:
        yesterday = TODAY - timedelta(days=1)
        v = evaluate([sess(yesterday, absent=True)], now=NOW)
        self.assertEqual(v.last_class_date, yesterday)


class TestHelpers(unittest.TestCase):
    def test_week_range_monday_to_sunday(self) -> None:
        start, end = week_range(date(2026, 3, 11))  # 周三
        self.assertEqual(start, date(2026, 3, 9))
        self.assertEqual(end, date(2026, 3, 15))
        self.assertEqual(start.weekday(), 0)
        self.assertEqual(end.weekday(), 6)

    def test_week_range_on_sunday(self) -> None:
        start, end = week_range(date(2026, 3, 15))  # 周日
        self.assertEqual(start, date(2026, 3, 9))
        self.assertEqual(end, date(2026, 3, 15))

    def test_state_levels_and_priority(self) -> None:
        self.assertEqual(StudentState.SEVERE.level, "danger")
        self.assertEqual(StudentState.ABNORMAL.level, "warn")
        for s in (StudentState.ACTIVE, StudentState.NO_CLASS, StudentState.TODAY_NOT_SIGNED):
            self.assertEqual(s.level, "ok")
        self.assertGreater(StudentState.SEVERE.priority, StudentState.ABNORMAL.priority)
        self.assertGreater(StudentState.ABNORMAL.priority, StudentState.TODAY_NOT_SIGNED.priority)
        self.assertGreater(StudentState.TODAY_NOT_SIGNED.priority, StudentState.ACTIVE.priority)
        self.assertGreater(StudentState.ACTIVE.priority, StudentState.NO_CLASS.priority)


if __name__ == "__main__":
    unittest.main(verbosity=2)
