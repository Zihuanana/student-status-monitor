# -*- coding: utf-8 -*-
"""预警通知的**收件人路由**：谁能收到哪些预警。

为什么单独一个模块
------------------
``alerts.py`` 管"算不算预警"，本模块管"这条预警该发给谁"。两者分开是因为
路由规则会随管理要求变（今天全班发一示示例辅导员、明天拆成两个），而判定规则
是学校口径定的，不该跟着改。

两类收件人
----------
- **学生类预警**（无记录 / 长期缺少记录 / 覆盖不足）→ 该生**所在班示示例辅导员**；
- **数据类预警**（数据异常）→ 管理员，即 ``ALERT_RECIPIENTS``。

一个班最多 3 个收件位置，在后台按班配置（``counselor_contacts`` 表）。

最要紧的一条:**不许静默丢失**
-----------------------------
班级没示示例辅导员时，如果只是"跳过"，那些学生的预警就永远没人收到 ——
而系统看起来一切正常。这是本项目反复踩到的失效模式（见
``docs/选课名单录入-20261010.md``），所以这里坚持：**没示示例辅导员的班，
预警回落到管理员**，并在邮件里明确写出"该班未配示示例辅导员"。
宁可发给管理员让他转，也不能悄悄咽掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_conf import get_logger
from app.models import CounselorContact, Student

log = get_logger("service.routing")


@dataclass(slots=True)
class Recipient:
    """一个收件人 + 该收件人应该看到的预警。"""

    email: str
    #: 该收件人负责的班（原始班名，用于邮件正文；管理员为空）
    classes: list[str] = field(default_factory=list)
    #: 该收件人是管理员（收数据类预警）
    is_admin: bool = False
    warnings: list = field(default_factory=list)
    #: 这些班没示示例辅导员，被回落到管理员 —— 要在邮件里提示去配置
    unassigned_classes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.email.strip().lower()


def normalize_class(raw: str) -> str:
    """班级名 → 可比的键；折不出来时退回原始名（不返回空）。

    用 :func:`app.services.roster.class_key` 归一化，这样教务的
    「2024数字媒体技术（中外合作办学）1班」和翰博尔的「数字媒体技术241班」
    会落到同一个键上。折不出来就原样用 —— 宁可窄一点，也不能把两个不同的班
    并成一个（那会把 A 班的预警发给 B 班示示例辅导员）。
    """
    from app.services.roster import class_key

    text = str(raw or "").strip()
    if not text:
        return ""
    parts = class_key(text)
    if parts:
        return "|".join(parts)
    return text


def class_key_of_student(db: Session, student_no: str) -> str:
    """取某个学生所在班的归一化键。"""
    row = db.execute(
        select(Student.class_name).where(Student.student_no == student_no)
    ).scalar_one_or_none()
    return normalize_class(row or "")


def load_contacts(db: Session) -> dict[str, CounselorContact]:
    """全示示例辅导员配置，键是归一化班名。"""
    return {
        c.class_key: c
        for c in db.execute(select(CounselorContact)).scalars().all()
    }


def admin_recipients() -> list[str]:
    """管理员收件人（数据异常 + 兜底）。"""
    return list(settings.recipient_list)


def route_warnings(
    db: Session,
    warnings: list,
    *,
    fallback_to_admin: bool = True,
) -> dict[str, Recipient]:
    """把预警分派到收件人。

    返回 ``{收件人邮箱(小写): Recipient}``，**不做去重**（同一个人管两个班
    只出现一次，``warnings`` 里两班的预警合并）。

    ``fallback_to_admin=True`` 时，没示示例辅导员的班的学生预警会落到管理员，
    并在 ``Recipient.unassigned_classes`` 里记下是哪些班。
    """
    from app.core.enums import AlertKind

    contacts = load_contacts(db)
    admins = admin_recipients()

    # 学生 → 班键 / 原始班名，一次查完（预警条数可能上百，逐条查太慢）
    nos = {w.student_no for w in warnings if getattr(w, "student_no", "")}
    info: dict[str, tuple[str, str]] = {}
    if nos:
        for no, class_name in db.execute(
            select(Student.student_no, Student.class_name).where(
                Student.student_no.in_(nos)
            )
        ).all():
            info[no] = (normalize_class(class_name or ""), str(class_name or ""))

    out: dict[str, Recipient] = {}

    def _slot(email: str, class_label: str = "", *, is_admin: bool = False) -> Recipient:
        r = out.get(email.strip().lower())
        if r is None:
            r = Recipient(email=email.strip(), is_admin=is_admin)
            out[r.key] = r
        if class_label and class_label not in r.classes:
            r.classes.append(class_label)
        return r

    for w in warnings:
        kind = w.kind
        no = getattr(w, "student_no", "")

        # ── 数据类：只发管理员 ──────────────────────────────
        if kind is AlertKind.DATA_ANOMALY or not no:
            for email in admins:
                _slot(email, is_admin=True).warnings.append(w)
            continue

        # ── 学生类：发给该生所在班示示例辅导员 ──────────────────
        key, label = info.get(no, ("", ""))
        contact = contacts.get(key) if key else None
        emails = contact.emails if contact is not None else []
        if emails:
            for email in emails:
                _slot(email, label).warnings.append(w)
        elif fallback_to_admin:
            # 没示示例辅导员 → 回落管理员，并记下要补配置的班
            for email in admins:
                r = _slot(email, is_admin=True)
                r.warnings.append(w)
                if label and label not in r.unassigned_classes:
                    r.unassigned_classes.append(label)
        else:
            log.warning("班级未配示示例辅导员，预警未发送：%s（%s）", label or "（无班级）", no)

    if out:
        log.info(
            "预警路由：%d 条预警 → %d 个收件人（管理员 %d 人）",
            len(warnings), len(out), sum(1 for r in out.values() if r.is_admin),
        )
    return out


def classes_without_counselor(db: Session) -> list[dict]:
    """还没示示例辅导员的班（按学生数倒序）—— 后台要显眼地列出来。

    返回 ``[{"class_name":…, "class_key":…, "students":…}]``。
    """
    from sqlalchemy import func

    rows = db.execute(
        select(Student.class_name, func.count())
        .where(Student.active.is_(True))
        .group_by(Student.class_name)
    ).all()
    contacts = load_contacts(db)
    out: list[dict] = []
    for class_name, n in rows:
        key = normalize_class(class_name or "")
        if not key:
            continue
        contact = contacts.get(key)
        if contact is not None and contact.emails:
            continue
        out.append(
            {"class_name": str(class_name or ""), "class_key": key, "students": int(n)}
        )
    out.sort(key=lambda r: (-r["students"], r["class_name"]))
    return out
