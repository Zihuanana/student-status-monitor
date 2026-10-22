# -*- coding: utf-8 -*-
"""历史数据回补。

为什么必须做
------------
`_fetch_statistics()` 只拉**当天**的课程与明细。没有历史数据时：

- 每个有缺勤的学生都被算成"连续 1 天" → 全员「异常」，看起来满屏告警；
- 真正的「连续三天异常」永远无法识别，连续天数字段全是 1。

首次接入真实数据、或系统停机一段时间后，都需要用它把过去的课补齐。

    python -m app.cli backfill --days 30
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dtime, timedelta

from app.adapters.hanboer import HanboerClient
from app.core.config import settings
from app.core.logging_conf import get_logger
from app.services.scope import describe_scope, split_by_scope

log = get_logger("service.backfill")


async def backfill(db, days: int = 30, now: datetime | None = None) -> dict:
    """逐天回补课程与考勤明细。返回统计信息。"""
    from app.services.ingest import (
        rebuild_from_events,
        upsert_courses,
        upsert_events,
        upsert_students,
    )
    from app.services.ingest import (
        infer_grade_from_student_no,
        infer_major_code_from_student_no,
    )

    now = now or datetime.now()
    if not settings.hb_base_url:
        raise RuntimeError("未配置 HB_BASE_URL")

    totals = {"days": 0, "courses": 0, "events": 0, "failed_days": 0,
              "skipped_courses": 0}
    log.info("回补作用域关键词：%s", describe_scope())

    async with HanboerClient() as client:
        for offset in range(days, -1, -1):
            day = now.date() - timedelta(days=offset)
            day_start = datetime.combine(day, dtime(0, 0))
            # 最后一天（今天）只取到当前时刻，避免请求"未来"的课
            day_end = min(
                datetime.combine(day, dtime(23, 59, 59)),
                now,
            )
            try:
                courses = await client.iter_statistics(
                    start_time=day_start, end_time=day_end
                )
            except Exception as exc:
                totals["failed_days"] += 1
                log.warning("回补 %s 的课程列表失败：%s", day, exc)
                continue

            # 只回补本校区的课。翰博尔是多校区共用平台，接口没有校区参数，
            # 不切的话会把别的校区的学生一起灌进库里。
            courses, skipped = split_by_scope(courses)
            totals["skipped_courses"] = totals.get("skipped_courses", 0) + len(skipped)

            totals["days"] += 1
            totals["courses"] += len(courses)
            if courses:
                upsert_courses(db, courses)
                db.commit()

            events = []
            for c in courses:
                if not c.schedule_guid:
                    continue
                try:
                    events.extend(
                        await client.iter_details(schedule_guid=c.schedule_guid)
                    )
                except Exception as exc:
                    log.warning("回补 %s/%s 明细失败：%s", day, c.schedule_guid, exc)

            if events:
                nos = {e.student_no for e in events}
                names = {e.student_no: e.student_name for e in events if e.student_name}
                upsert_students(
                    db,
                    [
                        {
                            "student_no": no,
                            "name": names.get(no, ""),
                            "grade": infer_grade_from_student_no(no),
                            "major_code": infer_major_code_from_student_no(no),
                            "major_name": "",
                            "class_name": "",
                            "org_name": "",
                        }
                        for no in nos
                    ],
                )
                upsert_events(db, events)
                db.commit()
                totals["events"] += len(events)

            log.info(
                "回补 %s：课程 %d 门，事件 %d 条", day, len(courses), len(events)
            )

    # 全部回补完后，统一重算判定与告警（避免逐天重算，浪费且慢）
    if totals["events"]:
        from app.services.alerts import evaluate_and_alert
        from app.services.ingest import rebuild_daily

        student_nos = {r[0] for r in db.query(_student_no_col()).all()}
        start = now.date() - timedelta(days=days + 1)
        written = rebuild_daily(
            db, student_nos=student_nos, start=start, end=now.date(), now=now
        )
        alerts = evaluate_and_alert(db, student_nos, now=now)
        totals["daily"] = written
        totals["alerts"] = alerts
        log.info("重算完成：天记录 %d，告警 %d", written, alerts)

    return totals


def _student_no_col():
    from app.models import Student

    return Student.student_no


def run_backfill(db, days: int = 30, now: datetime | None = None) -> dict:
    return asyncio.run(backfill(db, days=days, now=now))


_ = date
