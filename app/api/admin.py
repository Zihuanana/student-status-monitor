"""运维接口：采集状态、手动触发、告警列表、规则设置、邮件自检。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.logging_conf import get_logger
from app.core.security import require_session
from app.models import Student
from app.schemas import (
    AlertOut,
    CollectStatusOut,
    CounselorClassIn,
    CounselorClassOut,
    NotifyResult,
    SettingsIn,
    SettingsOut,
    SimpleResult,
)
from app.services.alerts import list_alerts, today_alert_counts
from app.services.ingest import collect_stats

router = APIRouter(tags=["admin"], dependencies=[Depends(require_session)])
log = get_logger("api.admin")

#: 轮询循环是否在跑（由 main 在启动时置位）
POLLING_ACTIVE = {"value": False}


@router.get("/collect/status", response_model=CollectStatusOut)
def collect_status(db: Session = Depends(get_db)) -> CollectStatusOut:
    stats = collect_stats(db)
    today = today_alert_counts(db)
    return CollectStatusOut(**stats, **today, polling=POLLING_ACTIVE["value"])


@router.get("/collect/health")
def collect_health(db: Session = Depends(get_db)) -> dict:
    """采集体检：连续失败轮数、距上次成功采集多久、是否已失明。"""
    from app.services.health import health_report
    from app.services.scope import describe_scope, scope_keywords

    report = health_report(db)
    report["campus_keywords"] = list(scope_keywords())
    report["campus_scope"] = describe_scope()
    return report


@router.get("/collect/runs")
def collect_runs(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
) -> list[dict]:
    """最近的采集记录，用于排障（真实接口接入后主要靠这个看问题）。"""
    from app.services.health import recent_runs

    return [
        {
            "id": r.id,
            "job": r.job,
            "started_at": r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": r.finished_at.strftime("%Y-%m-%d %H:%M:%S") if r.finished_at else None,
            "ok": r.ok,
            "courses_seen": r.courses_seen,
            "events_upserted": r.events_upserted,
            "message": r.message,
        }
        for r in recent_runs(db, limit=limit)
    ]


@router.post("/collect/run", response_model=SimpleResult)
async def collect_now(db: Session = Depends(get_db)) -> SimpleResult:
    """立即执行一轮采集（用于调试与手动补数据）。"""
    from app.services.collect import Collector

    # force=True：页面上的人工触发不该被 COLLECT_ENABLED 挡住
    result = await Collector().run_once(db, job="manual", force=True)
    return SimpleResult(ok=result["ok"], message=result["message"])


@router.post("/rebuild", response_model=SimpleResult)
def rebuild(
    db: Session = Depends(get_db),
    days: int = Query(60, ge=1, le=365),
) -> SimpleResult:
    """不抓接口，仅用库内事件重算全部判定结果（规则改动或排障后使用）。"""
    from app.services.collect import Collector

    n = Collector.rebuild_all(db, days=days)
    return SimpleResult(ok=True, message=f"已重算 {n} 条天记录")


@router.get("/alerts", response_model=list[AlertOut])
def alerts(
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    only_unsent: bool = Query(False),
) -> list[AlertOut]:
    return [AlertOut(**a) for a in list_alerts(db, limit=limit, only_unsent=only_unsent)]


@router.get("/settings", response_model=SettingsOut)
def read_settings() -> SettingsOut:
    return SettingsOut(
        severe_consecutive_days=settings.severe_consecutive_days,
        count_in_progress_day=settings.count_in_progress_day,
        leave_counts_as_present=settings.leave_counts_as_present,
        poll_interval_seconds=settings.poll_interval_seconds,
        alert_email_enabled=settings.alert_email_enabled,
        alert_recipients=settings.recipient_list,
        data_mode=settings.data_mode,
        collect_enabled=settings.collect_enabled,
        run_background_jobs=settings.run_background_jobs,
        retention_days=settings.retention_days,
        digest_hour=settings.digest_hour,
        alert_severe_immediate=settings.alert_severe_immediate,
    )


@router.put("/settings", response_model=SettingsOut)
def update_settings(payload: SettingsIn) -> SettingsOut:
    """运行期热更新判定规则（不落盘，重启后回到 .env 的值）。"""
    if payload.severe_consecutive_days is not None:
        settings.severe_consecutive_days = payload.severe_consecutive_days
    if payload.count_in_progress_day is not None:
        settings.count_in_progress_day = payload.count_in_progress_day
    if payload.leave_counts_as_present is not None:
        settings.leave_counts_as_present = payload.leave_counts_as_present
    log.warning("判定规则已热更新：%s", payload.model_dump(exclude_none=True))
    return read_settings()


@router.post("/email/test", response_model=SimpleResult)
def email_test(
    background: BackgroundTasks,
    to: str | None = Query(None, description="不填则发给 ALERT_RECIPIENTS"),
) -> SimpleResult:
    from app.services.notify import send_test_email

    target = to or (settings.recipient_list[0] if settings.recipient_list else "")
    if not target:
        return SimpleResult(ok=False, message="未配置收件人")
    ok, err = send_test_email(target)
    _ = background
    return SimpleResult(ok=ok, message=err or f"已发送至 {target}")


@router.post("/email/digest", response_model=SimpleResult)
def email_digest(
    db: Session = Depends(get_db),
    force: bool = Query(True, description="忽略当日已发送标记，强制重发"),
) -> SimpleResult:
    """立即发送「当前异常学生」汇总邮件。"""
    from app.services.alerts import daily_digest

    ok, msg = daily_digest(db, now=datetime.now(), force=force)
    return SimpleResult(ok=ok, message=msg or "汇总邮件已发送")


@router.get("/alerts/current")
def current_anomalies(db: Session = Depends(get_db)) -> dict:
    """当前异常学生名单（与每日汇总邮件内容一致），供页面「今日待处理」使用。"""
    from app.services.alerts import current_anomalies as _current

    rows = _current(db)
    return {
        "count": len(rows),
        "severe": sum(1 for r in rows if r["state"] == "严重异常"),
        "abnormal": sum(1 for r in rows if r["state"] == "异常"),
        "students": rows,
    }


# ── 辅导员联系人（预警发给谁） ──────────────────────────────


@router.get("/counselors", response_model=list[CounselorClassOut])
def list_counselors(db: Session = Depends(get_db)) -> list[CounselorClassOut]:
    """**所有班**（不只是配过的）+ 各自的收件位置，给后台配置用。

    已配过的班也要出现 —— 后台要能改；没配的班更要出现 —— 那正是要填的。
    """
    from sqlalchemy import func, select

    from app.models import CounselorContact
    from app.services.routing import normalize_class

    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for name, n in db.execute(
        select(Student.class_name, func.count())
        .where(Student.active.is_(True))
        .group_by(Student.class_name)
    ).all():
        key = normalize_class(name or "")
        if not key:
            continue
        counts[key] = counts.get(key, 0) + int(n)
        # 记下**原始班名**：没配示示例辅导员的班也显示人话，而不是 "2024|数字媒体技术|1"
        labels.setdefault(key, str(name or "").strip() or key)
    contacts = {
        c.class_key: c for c in db.execute(select(CounselorContact)).scalars().all()
    }

    out: list[CounselorClassOut] = []
    seen: set[str] = set()
    # 先出有学生的班（按人数倒序），再出"配了但名册里没有"的班（改错了也得能看见）
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        seen.add(key)
        c = contacts.get(key)
        out.append(
            CounselorClassOut(
                class_key=key,
                # 配过就用配置里的显示名（老师自己填的），否则用名册里的原始班名
                class_name=(c.class_label if c and c.class_label else labels.get(key, key)),
                students=n,
                slot1=c.slot1 if c else "",
                slot2=c.slot2 if c else "",
                slot3=c.slot3 if c else "",
                note=c.note if c else "",
                updated_at=c.updated_at.strftime("%Y-%m-%d %H:%M") if c and c.updated_at else None,
            )
        )
    for key, c in sorted(contacts.items()):
        if key in seen:
            continue
        out.append(
            CounselorClassOut(
                class_key=key, class_name=c.class_label or key, students=0,
                slot1=c.slot1, slot2=c.slot2, slot3=c.slot3, note=c.note,
                updated_at=c.updated_at.strftime("%Y-%m-%d %H:%M") if c.updated_at else None,
            )
        )
    return out


@router.post("/counselors", response_model=CounselorClassOut)
def save_counselor(
    payload: CounselorClassIn, db: Session = Depends(get_db)
) -> CounselorClassOut:
    """保存一个班示示例辅导员邮箱（最多 3 个位置）。三个都空 = 等于没配。"""
    from app.models import CounselorContact
    from app.services.routing import normalize_class

    key = normalize_class(payload.class_name)
    if not key:
        raise HTTPException(status_code=400, detail="班级名不能为空")
    row = db.get(CounselorContact, key)
    if row is None:
        row = CounselorContact(class_key=key)
        db.add(row)
    row.class_label = payload.class_name.strip()
    row.slot1 = payload.slot1.strip()
    row.slot2 = payload.slot2.strip()
    row.slot3 = payload.slot3.strip()
    row.note = payload.note.strip()
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    log.warning(
        "辅导员联系人已保存：%s → %s", row.class_label, "、".join(row.emails) or "（清空）"
    )
    return CounselorClassOut(
        class_key=row.class_key, class_name=row.class_label, slot1=row.slot1,
        slot2=row.slot2, slot3=row.slot3, note=row.note,
        updated_at=row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else None,
    )


@router.delete("/counselors/{class_key}", response_model=SimpleResult)
def delete_counselor(class_key: str, db: Session = Depends(get_db)) -> SimpleResult:
    """删掉一个班示示例辅导员配置（该班预警会回落到管理员）。"""
    from app.models import CounselorContact

    row = db.get(CounselorContact, class_key)
    if row is None:
        return SimpleResult(ok=False, message="没有这个班的配置")
    db.delete(row)
    db.commit()
    return SimpleResult(ok=True, message=f"已删除 {row.class_label or class_key} 示示例辅导员配置")


@router.get("/counselors/missing")
def missing_counselors(db: Session = Depends(get_db)) -> dict:
    """还没示示例辅导员的班 —— 这些班的学生预警目前只能发给管理员。"""
    from app.services.routing import classes_without_counselor

    rows = classes_without_counselor(db)
    return {"count": len(rows), "classes": rows}


@router.post("/notify/warnings", response_model=NotifyResult)
def notify_warnings(
    db: Session = Depends(get_db),
    force: bool = Query(True, description="忽略当日已发送标记"),
    dry_run: bool = Query(False, description="只算收件人和条数，不发信"),
) -> NotifyResult:
    """立即把当前预警按收件人分发（不等到每日定时）。

    ``dry_run=true`` 只回「会发给谁、各多少条」，方便配置完先核对名单 ——
    配错邮箱时最怕的是"发出去了才发现发错人"，所以留了这个先看后发的口子。
    """
    from app.services.alerts import current_warnings
    from app.services.routing import route_warnings

    if not settings.alert_email_enabled and not dry_run:
        return NotifyResult(
            ok=False, message="邮件告警未启用（ALERT_EMAIL_ENABLED=false）"
        )

    warnings = current_warnings(db)
    if not warnings:
        return NotifyResult(ok=True, message="当前无预警，无需发送")

    grouped = route_warnings(db, warnings)
    recipients = [r.email for r in grouped.values()]
    unassigned = sorted(
        {c for r in grouped.values() for c in r.unassigned_classes}
    )
    if dry_run:
        detail = "；".join(f"{r.email}（{len(r.warnings)} 条）" for r in grouped.values())
        return NotifyResult(
            ok=True,
            message=f"预演：{len(warnings)} 条预警将发给 {len(grouped)} 人 —— {detail}",
            sent=0,
            recipients=recipients,
            unassigned_classes=unassigned,
        )

    from app.services.alerts import daily_digest

    ok, msg = daily_digest(db, now=datetime.now(), force=force)
    return NotifyResult(
        ok=ok,
        message=msg or "预警通知已发送",
        sent=len(recipients) if ok else 0,
        recipients=recipients,
        unassigned_classes=unassigned,
    )

