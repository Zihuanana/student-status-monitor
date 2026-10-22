"""数据保留与清理。

为什么需要
----------
采集是**持续轮询**的：真实环境下每天都会新增课程与签到记录；模拟环境下
每个轮询周期还会生成新的 `scheduleGuid`。若不清理，`attendance_events` /
`attendance_daily` / `courses` 会无限增长（服务器磁盘只剩约 10G）。

策略
----
- 按 ``RETENTION_DAYS``（默认 400 天，覆盖一整个学年）删除过期的签到事件与天快照；
- 删除**已无任何事件引用**且同样过期的课程行，避免 courses 表单独膨胀；
- 保留 `alerts`（告警是审计数据，体积很小，不清理）。

由 ``collect_loop`` 每天调用一次。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.logging_conf import get_logger
from app.models import AttendanceDaily, AttendanceEvent, Course, StudentCourse

log = get_logger("service.maintenance")


def prune(db: Session, retention_days: int = 400, now: datetime | None = None) -> dict:
    """删除早于保留窗口的数据。返回各表删除行数。"""
    now = now or datetime.now()
    cutoff: date = now.date() - timedelta(days=max(30, retention_days))

    events = db.execute(
        delete(AttendanceEvent).where(AttendanceEvent.class_date < cutoff)
    ).rowcount or 0

    daily = db.execute(
        delete(AttendanceDaily).where(AttendanceDaily.class_date < cutoff)
    ).rowcount or 0

    links = db.execute(
        delete(StudentCourse).where(
            StudentCourse.class_date.is_not(None), StudentCourse.class_date < cutoff
        )
    ).rowcount or 0

    # 只删除「没有任何事件引用」的过期课程，避免误删仍需展示的课表
    referenced = select(AttendanceEvent.schedule_guid).distinct()
    courses = db.execute(
        delete(Course).where(
            Course.class_date.is_not(None),
            Course.class_date < cutoff,
            Course.schedule_guid.not_in(referenced),
        )
    ).rowcount or 0

    db.commit()
    result = {
        "cutoff": cutoff.isoformat(),
        "events": events,
        "daily": daily,
        "student_courses": links,
        "courses": courses,
    }
    if any(v for k, v in result.items() if k != "cutoff"):
        log.warning(
            "数据清理：事件 %d，天快照 %d，选课关系 %d，课程 %d（早于 %s）",
            events, daily, links, courses, cutoff,
        )
    return result


def db_stats(db: Session) -> dict:
    """统计各表行数，便于观察增长。"""
    from app.models import Student

    return {
        "students": db.execute(select(func.count()).select_from(Student)).scalar_one(),
        "courses": db.execute(select(func.count()).select_from(Course)).scalar_one(),
        "events": db.execute(select(func.count()).select_from(AttendanceEvent)).scalar_one(),
        "daily": db.execute(select(func.count()).select_from(AttendanceDaily)).scalar_one(),
    }


def checkpoint(db) -> None:
    """合并 WAL（备份前或清理后调用，缩小 -wal 文件）。

    注意不要用 ``db.connection()`` —— 它返回的是与会话事务绑定的连接，
    事务结束即关闭，随后再提交会抛 ResourceClosedError。
    """
    from sqlalchemy import text

    try:
        engine = db.get_bind()
        if engine.dialect.name != "sqlite":
            return
        with engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            conn.commit()
    except Exception as exc:  # 清理失败不应影响采集主流程
        log.debug("wal_checkpoint 跳过：%s", exc)
