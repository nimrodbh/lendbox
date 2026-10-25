"""Mail delivery. `sender()` picks how the outbox is drained:

- SMTP when LENDBOX_SMTP_HOST is set (with LENDBOX_SMTP_PORT, _USER, _PASSWORD, LENDBOX_MAIL_FROM), over TLS;
- the console in development (LENDBOX_DEV_MAIL=1), so the confirmation link can be clicked locally;
- otherwise nothing: rows stay unsent and are delivered once SMTP is configured. Links are never logged
  in production.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from lendbox.db.accounts import Send

log = logging.getLogger("lendbox.mail")


def _smtp_send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = os.environ["LENDBOX_MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ["LENDBOX_SMTP_HOST"], int(os.environ.get("LENDBOX_SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        if os.environ.get("LENDBOX_SMTP_USER"):
            smtp.login(os.environ["LENDBOX_SMTP_USER"], os.environ["LENDBOX_SMTP_PASSWORD"])
        smtp.send_message(msg)


def _console_send(to: str, subject: str, body: str) -> None:
    log.warning("DEV MAIL to %s | %s | %s", to, subject, body)


def sender() -> Send | None:
    if os.environ.get("LENDBOX_SMTP_HOST"):
        return _smtp_send
    if os.environ.get("LENDBOX_DEV_MAIL") == "1":
        return _console_send
    return None
