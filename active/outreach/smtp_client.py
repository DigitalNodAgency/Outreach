"""
smtp_client.py — Brevo SMTP email sender.
Handles daily cap enforcement, per-session health tracking, and send logging.
"""

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field

from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM,
    DAILY_EMAIL_CAP, SEND_DELAY_SECONDS,
    SMTP_HEALTH_MIN_SENDS, SMTP_HEALTH_FAIL_THRESHOLD,
)

logger = logging.getLogger(__name__)


@dataclass
class SMTPSession:
    sends_today: int = 0
    success_count: int = 0
    fail_count: int = 0
    cap_hit: bool = False
    health_degraded: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total_attempts(self) -> int:
        return self.success_count + self.fail_count

    @property
    def fail_rate(self) -> float:
        if self.total_attempts < SMTP_HEALTH_MIN_SENDS:
            return 0.0
        return self.fail_count / self.total_attempts

    def check_health(self) -> bool:
        """Returns True if health is degraded (>50% fail after 5+ sends)."""
        if self.total_attempts >= SMTP_HEALTH_MIN_SENDS:
            if self.fail_rate > SMTP_HEALTH_FAIL_THRESHOLD:
                self.health_degraded = True
        return self.health_degraded


_session = SMTPSession()


def get_session() -> SMTPSession:
    return _session


def reset_session() -> None:
    global _session
    _session = SMTPSession()


def cap_remaining() -> int:
    return max(0, DAILY_EMAIL_CAP - _session.sends_today)


def is_cap_hit() -> bool:
    return _session.sends_today >= DAILY_EMAIL_CAP


def send_email(to_email: str, subject: str, body: str, from_name: str = "") -> bool:
    """
    Send a single email via Brevo SMTP.
    Respects daily cap. Tracks session health.
    Returns True on success, False on failure.
    Applies SEND_DELAY_SECONDS after each send.
    """
    if is_cap_hit():
        logger.warning(f"[SMTP] Daily cap of {DAILY_EMAIL_CAP} hit. Skipping send to {to_email}.")
        _session.cap_hit = True
        return False

    if _session.health_degraded:
        logger.error(f"[SMTP] Health degraded. Refusing send to {to_email}.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{SMTP_FROM}>" if from_name else SMTP_FROM
    msg["To"] = to_email

    msg.attach(MIMEText(body, "plain"))

    if not SMTP_USER or not SMTP_PASS or not SMTP_FROM:
        err = f"SMTP_USER, SMTP_PASS, or SMTP_FROM is empty — set all three GitHub Secrets."
        logger.error(f"[SMTP] {err}")
        _session.fail_count += 1
        _session.errors.append(err)
        _session.health_degraded = True
        return False

    user_display = (
        f"{SMTP_USER[:4]}...@{SMTP_USER.split('@')[1]}" if "@" in SMTP_USER
        else f"{SMTP_USER[:8]}... (no @ found — invalid for Brevo)"
    )
    pass_len = len(SMTP_PASS)
    pass_prefix = SMTP_PASS[:8] if pass_len >= 8 else SMTP_PASS
    logger.info(f"[SMTP] Authenticating as: {user_display} | key_len={pass_len} key_prefix={pass_prefix}")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()  # re-announce after TLS upgrade
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())

        _session.sends_today += 1
        _session.success_count += 1
        logger.info(f"[SMTP] Sent to {to_email} | subject: {subject[:50]}")
        time.sleep(SEND_DELAY_SECONDS)
        return True

    except smtplib.SMTPAuthenticationError as e:
        err = f"Auth error sending to {to_email}: {e}"
        logger.error(f"[SMTP] {err}")
        _session.fail_count += 1
        _session.errors.append(err)
        # Auth errors are fatal — degrade health immediately
        _session.health_degraded = True
        return False

    except Exception as e:
        err = f"Send failed to {to_email}: {e}"
        logger.error(f"[SMTP] {err}")
        _session.fail_count += 1
        _session.errors.append(err)
        _session.check_health()
        time.sleep(SEND_DELAY_SECONDS)
        return False


def send_plain(to_email: str, subject: str, body: str) -> bool:
    """Alias for send_email without a from_name. Used for operator notifications."""
    return send_email(to_email=to_email, subject=subject, body=body)
