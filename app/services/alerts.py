"""告警生成与去重。

规则
----
- 学生状态为「异常」→ 产生 kind=异常 的告警记录
- 学生状态为「严重异常」→ 产生 kind=严重异常 的告警记录（连续 ≥3 个有课日无签到）
- 同一 (学生, 类型, 起始日, 结束日) 只记一条；连续天数增加时窗口滚动，生成新记录
- 邮件发送受 ALERT_COOLDOWN_HOURS 限制，避免反复轰示示例辅导员
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AlertKind, StudentState
from app.core.logging_conf import get_logger
from app.models import Alert, Student

log = get_logger("service.alerts")


def _build_alert_context(db: Session, student_no: str) -> str:
    """把「涉及课程 + 最近缺勤明细」序列化成 JSON，供邮件渲染。

    在告警层做这层查询，而不是让邮件渲染函数去查库 —— 渲染函数保持纯函数，
    才好测试（`tests/test_health.py` 直接断言渲染结果）。
    """
    import json

    try:
        from app.services.ingest import student_status_detail

        rows = student_status_detail(db, {student_no})
        row = rows.get(student_no) or {}
        recent = [
            {"date": d, "courses": (row.get("recent_missed_courses") or {}).get(d, [])}
            for d in (row.get("recent_missed_dates") or [])
        ]
        payload = {"courses": row.get("courses") or [], "recent_missed": recent}
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:  # 上下文缺失不应阻断告警本身
        log.warning("构建告警上下文失败（%s）：%s", student_no, exc)
        return json.dumps({"courses": [], "recent_missed": []}, ensure_ascii=False)


def _recent_alert_sent(db: Session, student_no: str, kind: str, now: datetime) -> bool:
    """冷却期内是否已给该学生发过同类告警邮件。"""
    since = now - timedelta(hours=max(1, settings.alert_cooldown_hours))
    row = db.execute(
        select(Alert)
        .where(
            Alert.student_no == student_no,
            Alert.kind == kind,
            Alert.email_sent.is_(True),
            Alert.email_sent_at >= since,
        )
        .limit(1)
    ).scalar_one_or_none()
    return row is not None


def evaluate_and_alert(
    db: Session,
    student_nos: set[str],
    now: datetime | None = None,
    commit: bool = True,
    facts: dict | None = None,
) -> int:
    """按四级预警规则生成/更新告警记录。返回本轮新产生的告警条数。

    四级预警（2026-10-13 按学校口径）：

    | 类型 | 含义 | 邮件 |
    |---|---|---|
    | 学生无记录预警 | 连续 3 个有效统计日无记录 | **即时**（示示例辅导员尽快联系） |
    | 长期缺少记录提醒 | 14 天内记录持续偏少 | 并入每日汇总 |
    | 监测覆盖不足提醒 | 系统看不到这个学生（课没考勤数据）| 并入每日汇总 |
    | 数据异常提醒 | 接口失败 / 课表缺失 / 课程对不上 | 并入每日汇总 |

    后两种是**数据侧**的：绝不能混进学生异常名单，否示示例辅导员会去追问一个
    根本没问题的学生。

    ``facts`` 可传入已装载的课时事实，避免重复装载 60 天窗口。
    """
    from app.services.ingest import _load_session_facts
    from app.services.warnings import data_anomaly_warnings, evaluate_warnings

    now = now or datetime.now()
    if not student_nos:
        return 0

    if facts is None:
        facts = _load_session_facts(
            db, student_nos, now.date() - timedelta(days=60), now.date()
        )
    students = {
        s.student_no: s
        for s in db.execute(select(Student).where(Student.student_no.in_(student_nos))).scalars()
    }

    created = 0
    for no, warnings in evaluate_warnings(db, student_nos, now=now).items():
        for w in warnings:
            if _upsert_alert(db, w, students.get(no), now):
                created += 1

    # 数据侧的提醒与具体学生无关，只在本批覆盖全体时生成一次
    for w in data_anomaly_warnings(db, now=now):
        if _upsert_alert(db, w, None, now, force_once=True):
            created += 1

    if commit:
        db.commit()
    else:
        db.flush()

    if created:
        log.warning("新增 %d 条预警", created)

    _ = facts
    return created


def _upsert_alert(db: Session, warning, student: Student | None,
                  now: datetime, force_once: bool = False) -> bool:
    """写入一条预警；已存在同 (学生, 类型, 起止日) 的则更新。返回是否新建。"""
    exists = db.execute(
        select(Alert).where(
            Alert.student_no == warning.student_no,
            Alert.kind == warning.kind.value,
            Alert.first_date == warning.first_date,
            Alert.last_date == warning.last_date,
        )
    ).scalar_one_or_none()
    if exists is not None:
        exists.detail = warning.reason
        exists.consecutive_days = warning.days
        return False

    alert = Alert(
        student_no=warning.student_no,
        kind=warning.kind.value,
        state=warning.kind.value,
        consecutive_days=warning.days,
        first_date=warning.first_date,
        last_date=warning.last_date,
        detail=warning.reason,
        detail_json=_build_warning_context(db, warning),
    )
    db.add(alert)
    db.flush()

    # 只有「学生确实缺勤」这一类才即时发信；数据侧问题并入每日汇总，
    # 免得凌晨因为一次接口抖动就给人发信。
    immediate = (
        warning.kind is AlertKind.NO_RECORD
        and settings.alert_severe_immediate
    )
    if settings.alert_email_enabled and immediate:
        if not _recent_alert_sent(db, warning.student_no, warning.kind.value, now):
            _queue_email(db, alert, student, now)
    _ = force_once
    return True


def _build_warning_context(db: Session, warning) -> str:
    """预警上下文（涉及课程等），供邮件渲染。"""
    import json

    payload: dict = {"courses": [], "recent_missed": []}
    try:
        if warning.detail:
            payload.update(warning.detail)
        if warning.student_no:
            from app.services.ingest import student_status_detail

            row = (student_status_detail(db, {warning.student_no}) or {}).get(
                warning.student_no
            ) or {}
            payload["courses"] = row.get("courses") or []
    except Exception as exc:
        log.warning("构建预警上下文失败（%s）：%s", warning.student_no, exc)
    return json.dumps(payload, ensure_ascii=False)


def _queue_email(db: Session, alert: Alert, student: Student | None, now: datetime) -> None:
    """发送告警邮件并回写结果（同步 SMTP，失败不影响主流程）。"""
    from app.services.notify import send_alert_email

    name = student.name if student else alert.student_no
    try:
        ok, err = send_alert_email(
            subject=f"【学生状态监测】{alert.state}：{name}（{alert.student_no}）连续 {alert.consecutive_days} 个有课日无签到",
            body=_render_body(alert, student),
        )
        alert.email_sent = bool(ok)
        alert.email_sent_at = now if ok else None
        alert.email_error = err or ""
        if ok:
            log.warning("已发送告警邮件：%s %s", alert.student_no, alert.kind)
        else:
            log.error("告警邮件发送失败：%s %s -> %s", alert.student_no, alert.kind, err)
    except Exception as exc:  # 邮件问题绝不能影响采集
        alert.email_sent = False
        alert.email_error = f"{type(exc).__name__}: {exc}"[:500]
        log.exception("告警邮件异常（学生 %s）：%s", alert.student_no, exc)
    _ = db


def _render_body(alert: Alert, student: Student | None) -> str:
    """渲染单条预警邮件。

    尽量写清楚「缺了哪几节课、哪几天」，示示例辅导员**不必打开页面**就能判断
    这个学生是真旷课还是有别的原因（生病、实习、转专业……）。
    """
    major = ""
    if student:
        major = f"{student.grade}级{student.major_name or student.major_code} {student.class_name}".strip()
    # created_at 是 Python 侧默认值，在 flush 之前仍为 None；
    # 而发信发生在 flush 之前，直接用会抛
    # "unsupported format string passed to NoneType.__format__"，
    # 结果是第一封严重异常告警静默发不出去。
    created = alert.created_at or datetime.now()

    #: 每种预警给一行"该怎么办"，避免收信人不知道这封邮件要他做什么。
    action = {
        AlertKind.NO_RECORD.value: "请尽快联系学生本人核实情况。",
        AlertKind.LONG_TERM_MISSING.value: "该生近期到课记录持续偏少，建议联系学生了解原因。",
        AlertKind.LOW_COVERAGE.value: (
            "这些课程在各考勤平台都没有可用数据，系统无法判断该生是否到课。"
            "请通过点名、班委反馈或其它方式进一步核实（此为数据覆盖问题，非学生缺勤）。"
        ),
        AlertKind.DATA_ANOMALY.value: "请相关管理员核查数据链路（课表、课程匹配或接口同步）。",
    }.get(alert.kind, "请及时联系学生了解情况。")

    if alert.kind == AlertKind.NO_RECORD.value:
        headline = "学生无记录预警（待核查）"
    else:
        headline = alert.kind

    lines = [
        "学生状态监测系统 — " + headline,
        "",
    ]
    if alert.student_no:
        lines += [
            f"姓名：{student.name if student else '未知'}",
            f"学号：{alert.student_no}",
            f"年级专业：{major or '—'}",
        ]
    lines += [
        f"类型：{alert.kind}",
        f"涉及天数：{alert.consecutive_days} 天",
        f"区间：{alert.first_date} ~ {alert.last_date}",
        f"判定说明：{alert.detail}",
    ]

    detail = {}
    if alert.detail_json:
        try:
            import json

            detail = json.loads(alert.detail_json) or {}
        except (ValueError, TypeError):
            detail = {}
    courses = detail.get("courses") or []
    if courses:
        lines.append("涉及课程：" + "、".join(courses))
    dates = detail.get("dates") or []
    if dates:
        lines.append("")
        lines.append("无记录的日期：")
        for d in dates[:15]:
            lines.append(f"  · {d}")
    recent = detail.get("recent_missed") or []
    if recent:
        lines.append("")
        lines.append("最近整日未签到的日期与课程：")
        for item in recent:
            label = "、".join(item.get("courses") or []) or "（无课程记录）"
            lines.append(f"  · {item.get('date')}　{label}")
    if detail.get("effective_days") is not None:
        lines.append("")
        lines.append(
            f"窗口内有效统计日 {detail.get('effective_days')} 天，"
            f"其中有记录 {detail.get('recorded_days')} 天"
            f"（出勤 {detail.get('present_days', 0)} 天、请假 {detail.get('leave_days', 0)} 天）"
        )

    if alert.student_no and not courses and not recent:
        lines.append("")
        lines.append("（未能取到课程明细，请登录系统查看课表）")

    lines += [
        "",
        f"生成时间：{created:%Y-%m-%d %H:%M:%S}",
        "",
        action,
    ]
    return "\n".join(lines)


def list_alerts(db: Session, limit: int = 100, only_unsent: bool = False) -> list[dict]:
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    if only_unsent:
        stmt = (
            select(Alert)
            .where(Alert.email_sent.is_(False))
            .order_by(Alert.created_at.desc())
            .limit(limit)
        )
    out = []
    for a in db.execute(stmt).scalars():
        out.append(
            {
                "id": a.id,
                "student_no": a.student_no,
                "kind": a.kind,
                "state": a.state,
                "consecutive_days": a.consecutive_days,
                "first_date": a.first_date.isoformat(),
                "last_date": a.last_date.isoformat(),
                "detail": a.detail,
                "email_sent": a.email_sent,
                "email_sent_at": a.email_sent_at.strftime("%Y-%m-%d %H:%M") if a.email_sent_at else None,
                "email_error": a.email_error,
                "created_at": a.created_at.strftime("%Y-%m-%d %H:%M"),
            }
        )
    return out


def current_anomalies(db: Session) -> list[dict]:
    """当前状态为「异常」或「严重异常」的学生（按严重程度排序）。

    这是示示例辅导员的"待处理名单"，与 `alerts` 表的区别在于它反映的是**此刻的状态**：
    学生今天来上课了就不再出现，不需示示例辅导员自己去核对。
    """
    from app.services.ingest import dashboard_rows

    rows = [
        r
        for r in dashboard_rows(db)
        if r["state"] in (StudentState.ABNORMAL.value, StudentState.SEVERE.value)
    ]
    rows.sort(key=lambda r: (-r["consecutive_missed_days"], r["student_no"]))
    return rows


def build_digest_body(
    warnings: list,
    day: date,
    names: dict | None = None,
    *,
    recipient: object | None = None,
) -> str:
    """把当前预警渲染成每日汇总邮件正文（四级预警分组）。

    ``warnings`` 是 :class:`app.services.warnings.Warning` 列表。
    学生侧（无记录 / 长期缺少记录）排前面，数据侧（覆盖不足 / 数据异常）
    排后面 —— 老师先看要联系的学生，管理员看要修的数据。

    ``names`` 是 ``{学号: 姓名}``：邮件里只写学示示例辅导员不好认人
    （实示示例辅导员拿到学号还得回系统查一遍），所以有姓名就带上。

    ``recipient`` 是 :class:`app.services.routing.Recipient`（可选）：
    辅导员只收到自己班的预警，所以正文要说清"这是哪个班的名单"；
    管理员若因班级没示示例辅导员而收到学生预警，也要在正文里点明，
    免得他以为这些学生归他管。
    """
    names = names or {}
    order = [
        (AlertKind.NO_RECORD,
         "【学生无记录预警】连续 3 个有效统计日无有效考勤记录（待核查）"),
        (AlertKind.LONG_TERM_MISSING,
         "【长期缺少记录提醒】14 天内有效考勤记录持续偏少"),
        (AlertKind.LOW_COVERAGE,
         "【监测覆盖不足提醒】课程在考勤平台无可用数据（系统看不到，非学生缺勤）"),
        (AlertKind.DATA_ANOMALY,
         "【数据异常提醒】请管理员核查数据链路"),
    ]

    student_side = sum(1 for w in warnings if w.kind.is_student_side)
    data_side = len(warnings) - student_side
    lines = [
        f"学生状态监测系统 — {day} 预警汇总",
        "",
    ]

    # 辅导员收到的邮件要写清是哪个班；管理员邮件写清是"全校 + 数据"
    classes = list(getattr(recipient, "classes", []) or [])
    unassigned = list(getattr(recipient, "unassigned_classes", []) or [])
    if classes:
        lines.append(f"负责班级：{'、'.join(classes)}")
    elif recipient is not None:
        lines.append("负责范围：全校（管理员）")
    lines += [
        f"当前共 {len(warnings)} 条预警：学生侧 {student_side} 条，数据侧 {data_side} 条。",
        "",
    ]

    # 管理员兜底：这些班没示示例辅导员，预警先发给管理员，请尽快补配置
    if unassigned:
        lines += [
            "⚠ 以下班级**尚未配示示例辅导员**，其学生预警暂由管理员代收，请尽快在"
            "后台「辅导员联系人」里补上：",
        ]
        lines += [f"  · {c}" for c in unassigned]
        lines.append("")

    for kind, title in order:
        items = [w for w in warnings if w.kind is kind]
        if not items:
            continue
        lines.append(f"{title}（{len(items)} 条）")
        for w in items[:60]:
            if w.student_no:
                name = names.get(w.student_no) or ""
                who = f"{name}（{w.student_no}）" if name else w.student_no
            else:
                who = "（系统）"
            lines.append(f"  · {who}　{w.reason}　[{w.first_date} ~ {w.last_date}]")
        if len(items) > 60:
            lines.append(f"  … 其余 {len(items) - 60} 条，请登录系统查看")
        lines.append("")

    lines += [
        "说明：",
        "· 「无记录预警」= 学生当日有课、课程也确实产生了考勤数据，但查不到他的记录；",
        "· 「覆盖不足」= 这些课在各平台都没有可用考勤数据，系统判断不了，不代表学生缺勤；",
        "· 本汇总反映的是当前仍未恢复正常的名单，已有签到的学生不会出现。",
        "请登录监测系统查看课表与每日签到详情。",
    ]
    return "\n".join(lines)


def current_warnings(db: Session, now: datetime | None = None) -> list:
    """当前所有预警（四级）。学生侧在前、数据侧在后，各自按天数倒序。"""
    from app.services.warnings import data_anomaly_warnings, evaluate_warnings

    now = now or datetime.now()
    nos = {
        r[0]
        for r in db.execute(
            select(Student.student_no).where(Student.active.is_(True))
        ).all()
    }
    out: list = []
    per_student = evaluate_warnings(db, nos, now=now)
    for no in sorted(per_student):
        out.extend(per_student[no])
    out.sort(key=lambda w: (not w.kind.is_student_side, -w.days, w.student_no))
    out.extend(data_anomaly_warnings(db, now=now))
    return out


def _notifiable(warnings: list, db: Session, now: datetime) -> list:
    """每日邮件里真正该发的预警。

    2026-10-13 的结论：**目前无需过滤**。用户选择的"方案 B"（只提醒
    一节考勤数据都没有的学生）与学校规则 ② 本来就是同一件事 ——
    规则 ② 的前置条件是"这段时间**所有**有课的日期都没有可用考勤数据"，
    只要有一天看得到，它就不成立。

    线上实测（2026-10-13）：规则 ② 报出 187 人，与"窗口内零考勤数据"
    严格口径**完全一致，差集为空**。所以这里不加过滤，避免多出一层
    "看起来能调、实际不改变任何结果"的配置。

    但页面与邮件的分工要保留：``warning_summary`` 始终按完整口径统计，
    邮件只是投递渠道。将来若真出现"部分课看不到"也要提醒的需求，
    在这里加过滤即可 —— 函数留着就是给那个位置。
    """
    return warnings



def daily_digest(
    db: Session, now: datetime | None = None, force: bool = False
) -> tuple[bool, str]:
    """把当前预警按收件人分派，各自收到**自己该看的那部分**。

    与 ``evaluate_and_alert`` 的分工：

    - **学生无记录预警**（连续 3 个有效统计日无记录）：出现时即时单独发信
      （受 ``ALERT_SEVERE_IMMEDIATE`` 控制）—— 这条要人尽快联系学生；
    - 其余三类（长期缺少记录 / 覆盖不足 / 数据异常）：不即时打扰，
      由本函数每天定时汇总一封。

    收件人怎么定（见 ``app/services/routing.py``）：

    - 学生类预警 → 该生**所在班示示例辅导员**（后台按班配，每班最多 3 个邮箱）；
    - 数据类预警 → **管理员**（``ALERT_RECIPIENTS``）；
    - 班级没示示例辅导员 → 回落给管理员，并在邮件里点明要补配置
      （**绝不静默丢掉** —— 那会让一批学生永远没人管，而系统看起来正常）。

    返回 ``(是否成功, 说明)``。没有预警时返回 ``(True, "当前无预警")``
    且不发信 —— 避免每天发一封"一切正常"的噪音邮件。
    """
    from app.services.notify import send_alert_email
    from app.services.routing import route_warnings

    now = now or datetime.now()
    today = now.date()

    if not settings.alert_email_enabled:
        return False, "邮件告警未启用（ALERT_EMAIL_ENABLED=false）"

    warnings = current_warnings(db, now=now)
    if not warnings:
        return True, "当前无预警，未发送汇总邮件"

    # 方案 B：只把"完全看不到"的覆盖不足发进邮件（见 _notifiable 的说明）
    before = len(warnings)
    warnings = _notifiable(warnings, db, now)
    if not warnings:
        return True, (
            f"当前 {before} 条预警都不属于要发邮件的类型"
            f"（覆盖不足中「部分课看不到」的按方案 B 不发）"
        )

    # 幂等：同一天只自动发一次，避免调度重复触发导致重复打扰
    if not force and _digest_already_sent(db, today):
        return True, f"{today} 的汇总邮件已发送过，跳过"

    grouped = route_warnings(db, warnings)
    if not grouped:
        return False, "预警无法分派给任何收件人（未配示示例辅导员，也没有管理员）"

    names = {
        no: nm
        for no, nm in db.execute(
            select(Student.student_no, Student.name).where(
                Student.student_no.in_([w.student_no for w in warnings if w.student_no])
            )
        ).all()
    }

    sent = 0
    errors: list[str] = []
    for r in grouped.values():
        mine = r.warnings
        student_side = sum(1 for w in mine if w.kind.is_student_side)
        # 收件人角色写进主题，老师一眼能分辨这封是不是给自己的
        scope = "、".join(r.classes) if r.classes else "全校数据"
        subject = (
            f"【学生状态监测】{today} 预警汇总"
            f"（{scope}，{len(mine)} 条：学生侧 {student_side}，"
            f"数据侧 {len(mine) - student_side}）"
        )
        ok, err = send_alert_email(
            subject=subject,
            body=build_digest_body(mine, today, names, recipient=r),
            recipients=[r.email],
        )
        if ok:
            sent += 1
        else:
            errors.append(f"{r.email}: {err}")
            log.error("汇总邮件发送失败 → %s：%s", r.email, err)

    if errors:
        # 部分成功也算失败：要让调用方（页面/日志）看见谁没收到
        return False, f"已发送 {sent}/{len(grouped)} 封；失败：{'；'.join(errors)}"

    _mark_digest_sent(db, today, now)
    log.warning(
        "已发送每日预警汇总：%d 条预警 → %d 个收件人（学生侧 %d 条）",
        len(warnings),
        sent,
        sum(1 for w in warnings if w.kind.is_student_side),
    )
    return True, f"已发送 {sent} 封汇总邮件（共 {len(warnings)} 条预警）"


#: 记录每日汇总发送时间的 AppSetting 键前缀
_DIGEST_KEY = "digest_sent_on"


def _digest_already_sent(db: Session, day: date) -> bool:
    from app.models import AppSetting

    return db.get(AppSetting, _DIGEST_KEY) is not None and (
        db.get(AppSetting, _DIGEST_KEY).value == day.isoformat()
    )


def _mark_digest_sent(db: Session, day: date, now: datetime) -> None:
    from app.models import AppSetting

    row = db.get(AppSetting, _DIGEST_KEY)
    if row is None:
        db.add(AppSetting(key=_DIGEST_KEY, value=day.isoformat(), updated_at=now))
    else:
        row.value = day.isoformat()
        row.updated_at = now
    db.commit()


def pending_digest_count(db: Session) -> int:
    """当前异常学生数（供页面提示"今日待处理"）。"""
    return len(current_anomalies(db))


def today_alert_counts(db: Session, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    rows = db.execute(select(Alert.kind, Alert.consecutive_days).where(Alert.created_at >= start)).all()
    _ = date  # 保留导入
    return {
        "today_alerts": len(rows),
        "today_severe": sum(1 for r in rows if r[0] == AlertKind.SEVERE.value),
    }
