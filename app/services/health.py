"""采集健康监控：采集持续失败时主动告警。

为什么需要
----------
第 7.2 节那个缺陷修好之后，采集失败会正确记成 ``CollectRun.ok=False`` —— 但
**只有主动打开页面的人才看得到**。真实场景里更危险的是"静默失明"：
考勤接口挂了 / 凭据过期 / 网络不通，系统连续几小时没采到任何数据，
而页面上显示的仍是几小时前的旧状态（"全员正常"）。

因此需要对采集本身做体检，并在**连续失败达到阈值**时示示例辅导员发信，
让"监测系统自己坏了"这件事也能被监测到。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_conf import get_logger
from app.models import CollectRun

log = get_logger("service.health")


def recent_runs(db: Session, limit: int = 20) -> list[CollectRun]:
    return list(
        db.execute(
            select(CollectRun).order_by(CollectRun.started_at.desc()).limit(limit)
        ).scalars()
    )


def consecutive_failures(db: Session, limit: int = 20) -> int:
    """从最近一次往前数，连续失败的轮数（遇到成功即停止）。"""
    count = 0
    for run in recent_runs(db, limit=limit):
        if run.ok:
            break
        count += 1
    return count


def last_success(db: Session) -> CollectRun | None:
    return db.execute(
        select(CollectRun).where(CollectRun.ok.is_(True)).order_by(CollectRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()


def last_success_age_minutes(db: Session, now: datetime | None = None) -> float | None:
    """距上一次成功采集过了多少分钟；从未成功过返回 None。"""
    now = now or datetime.now()
    run = last_success(db)
    if run is None:
        return None
    return (now - run.started_at).total_seconds() / 60.0


def health_report(db: Session, now: datetime | None = None) -> dict:
    """供 API 与告警复用的体检结果。"""
    now = now or datetime.now()
    failures = consecutive_failures(db)
    age = last_success_age_minutes(db, now=now)
    # 轮询间隔的 3 倍仍未成功，或连续失败达 3 轮，即认为异常
    stale_after = max(15, (settings.poll_interval_seconds * 3) // 60)
    stale = age is None or age > stale_after
    unhealthy = failures >= 3 or stale

    runs = recent_runs(db, limit=1)
    return {
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "consecutive_failures": failures,
        "last_success_age_minutes": round(age, 1) if age is not None else None,
        "stale_after_minutes": stale_after,
        "stale": stale,
        "unhealthy": unhealthy,
        "last_message": runs[0].message if runs else "",
        "polling": settings.collect_enabled and settings.run_background_jobs,
        "data_mode": settings.data_mode,
    }


def alert_if_unhealthy(db: Session, now: datetime | None = None) -> bool:
    """采集不健康时示示例辅导员发信（每 ``ALERT_COOLDOWN_HOURS`` 最多一封）。

    返回是否真的发出。
    """
    from app.services.notify import send_alert_email

    now = now or datetime.now()
    report = health_report(db, now=now)
    if not report["unhealthy"]:
        return False
    if not settings.alert_email_enabled:
        log.error("采集不健康但邮件未启用，无法通知：%s", report)
        return False

    # 冷却：用 AppSetting 记住上次体检告警时间
    from app.models import AppSetting

    key = "health_alert_last_sent_at"
    row = db.get(AppSetting, key)
    cooldown = timedelta(hours=max(1, settings.alert_cooldown_hours))
    if row is not None and row.value:
        try:
            last = datetime.fromisoformat(row.value)
            if now - last < cooldown:
                return False
        except ValueError:
            pass

    failures = report["consecutive_failures"]
    age = report["last_success_age_minutes"]
    body = "\n".join(
        [
            "学生状态监测系统 — 采集异常告警",
            "",
            "系统连续未能从考勤接口取到数据，当前页面显示的状态可能已经过时。",
            "",
            f"连续失败轮数：{failures}",
            f"距上次成功采集：{age if age is not None else '从未成功'} 分钟",
            f"最近一次报错：{report['last_message'] or '（无）'}",
            f"采集模式：{report['data_mode']}",
            f"检查时间：{report['checked_at']}",
            "",
            "请检查考勤接口凭据是否过期、服务器网络是否正常。",
        ]
    )
    ok, err = send_alert_email(
        subject=f"【学生状态监测】采集异常：已连续 {failures} 轮未取到数据",
        body=body,
    )
    if ok:
        if row is None:
            db.add(AppSetting(key=key, value=now.isoformat()))
        else:
            row.value = now.isoformat()
            row.updated_at = now
        db.commit()
        log.error("已发送采集异常告警邮件（连续失败 %d 轮）", failures)
    else:
        log.error("采集异常告警邮件发送失败：%s", err)
    return ok


def should_check(now: datetime, last_checked: datetime | None) -> bool:
    """体检频率：每 30 分钟一次即可，不必每轮都查库。"""
    if last_checked is None:
        return True
    return (now - last_checked) >= timedelta(minutes=30)


def daily_task_due(
    now: datetime,
    hour: int,
    last_run: datetime | None,
) -> bool:
    """判断「每天某整点执行一次」的任务此刻是否该跑。

    ``hour`` 为负数表示关闭。同一自然日内只返回一次 True，
    因此调用方只要记录上次运行时间即可，无需自己判断日期。
    """
    if hour < 0:
        return False
    if now.hour < hour:
        return False
    if last_run is not None and last_run.date() == now.date():
        return False
    return True
