"""Transactional email (verification, password reset, invitations).

DUOSIM_SMTP_URL=smtp://user:pass@host:587 (STARTTLS) or smtps://user:pass@host:465 sends real mail;
without it, mail is written to the log (development) and kept in `Mailer.outbox` (tests).
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit

log = logging.getLogger("duosim.mail")


@dataclass
class Mail:
    to: str
    subject: str
    text: str


class Mailer:
    def __init__(self):
        self.outbox: list[Mail] = []
        self.sender = os.environ.get("DUOSIM_MAIL_FROM", "DuoSim <no-reply@duosim.local>")
        self.url = os.environ.get("DUOSIM_SMTP_URL", "").strip()

    @property
    def live(self) -> bool:
        return bool(self.url)

    def send(self, to: str, subject: str, text: str) -> None:
        m = Mail(to, subject, text)
        self.outbox.append(m)
        del self.outbox[:-50]
        if not self.url:
            log.warning("mail (no DUOSIM_SMTP_URL, not sent) to=%s subject=%s\n%s", to, subject, text)
            return
        threading.Thread(target=self._smtp, args=(m,), daemon=True).start()  # never block a request on SMTP

    def _smtp(self, m: Mail) -> None:
        u = urlsplit(self.url)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, m.to, m.subject
        msg.set_content(m.text)
        try:
            ctx = ssl.create_default_context()
            if u.scheme == "smtps":
                s = smtplib.SMTP_SSL(u.hostname or "", u.port or 465, context=ctx, timeout=20)
            else:
                s = smtplib.SMTP(u.hostname or "", u.port or 587, timeout=20)
                s.starttls(context=ctx)
            with s:
                if u.username:
                    s.login(unquote(u.username), unquote(u.password or ""))
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            log.error("sending mail to %s failed: %s", m.to, type(e).__name__)
