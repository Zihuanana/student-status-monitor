"""邮件通知（SMTP）。

服务器实测可连通 smtp.example.com:465（SSL），因此邮件告警链路是可行的。
换用企业邮箱/学校邮箱时只需改 .env 里的 SMTP_* 配置。
"""

from __future__ import annotations

import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from app.core.config import settings
from app.core.logging_conf import get_logger

log = get_logger("service.notify")


def send_alert_email(subject: str, body: str, recipients: list[str] | None = None) -> tuple[bool, str]:
    """发送告警邮件。返回 (是否成功, 错误信息)。"""
    if not settings.alert_email_enabled:
        return False, "邮件告警未启用（ALERT_EMAIL_ENABLED=false）"

    to = recipients if recipients is not None else settings.recipient_list
    if not to:
        return False, "未配置收件人（ALERT_RECIPIENTS）"
    if not settings.smtp_host or not settings.smtp_user:
        return False, "未配置 SMTP 账号"

    sender = settings.smtp_from or settings.smtp_user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("学生状态监测系统", "utf-8")), sender))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)

    try:
        if settings.smtp_use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ctx) as srv:
                srv.login(settings.smtp_user, settings.smtp_password)
                srv.sendmail(sender, to, msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as srv:
                srv.ehlo()
                try:
                    srv.starttls(context=ssl.create_default_context())
                    srv.ehlo()
                except smtplib.SMTPException:
                    log.warning("STARTTLS 不可用，使用明文发送")
                srv.login(settings.smtp_user, settings.smtp_password)
                srv.sendmail(sender, to, msg.as_string())
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def send_test_email(recipient: str) -> tuple[bool, str]:
    return send_alert_email(
        subject="【学生状态监测系统】邮件链路测试",
        body="这是一封测试邮件。收到即表示告警邮件链路正常。\n\n学生状态监测系统",
        recipients=[recipient],
    )
