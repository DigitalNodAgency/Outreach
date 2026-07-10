"""
smtp_client.py — Brevo SMTP email sender.
Handles daily cap enforcement, per-session health tracking, and send logging.
"""

import logging
import random
import smtplib
import time
from datetime import date
from email.mime.text import MIMEText
from dataclasses import dataclass, field

from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM,
    DAILY_EMAIL_CAP, SEND_DELAY_SECONDS,
    MIN_SEND_GAP_SECONDS, MAX_SEND_GAP_SECONDS,
    SMTP_HEALTH_MIN_SENDS, SMTP_HEALTH_FAIL_THRESHOLD,
    WARMUP_START_DATE, WARMUP_STEP_DAYS, WARMUP_SCHEDULE,
    RUN_TIME_BUDGET_SECONDS,
)

logger = logging.getLogger(__name__)


def _pacing_gap() -> float:
    """Randomized inter-send delay (seconds) for cold lead outreach. Uniform over
    [MIN_SEND_GAP_SECONDS, MAX_SEND_GAP_SECONDS] so sends are never a fixed metronome,
    with a hard 5-min-class minimum. Falls back to the min if the range is misconfigured."""
    lo, hi = MIN_SEND_GAP_SECONDS, MAX_SEND_GAP_SECONDS
    if hi < lo:
        return float(lo)
    return random.uniform(lo, hi)


def pace_sleep(deadline: float | None = None) -> None:
    """Randomized inter-send gap for cold lead outreach. The engine calls this
    AFTER a send is fully recorded in the Sheet (status + outreach_log) — never
    between the SMTP send and its writes: a hard kill during a sleep in that
    window strands a sent-but-unrecorded lead, which the next run would re-send
    (duplicate email). `deadline` is a time.monotonic() timestamp; the sleep is
    skipped when the gap would cross it, letting the loop's deadline check end
    the run gracefully instead of oversleeping into the workflow's hard kill."""
    gap = _pacing_gap()
    if deadline is not None and time.monotonic() + gap >= deadline:
        logger.info("[SMTP] Pacing skipped: gap would cross the run time budget.")
        return
    logger.info(f"[SMTP] Pacing: sleeping {gap/60:.1f} min before next send.")
    time.sleep(gap)


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


def seed_sends_today(count: int) -> None:
    """Pre-load sends_today from sends a PRIOR run already logged today (UTC), so
    DAILY_EMAIL_CAP is a true per-calendar-day ceiling shared across multiple daily
    firings — not a per-run cap that resets every time the workflow fires (redundant
    cron is a reliability safety net, not a second daily budget). Call once, before
    any sends this run. Never lowers an already-higher in-process count."""
    if count > _session.sends_today:
        _session.sends_today = count


def _ramp_cap() -> int:
    """Warm-up-ramp cap. During the warm-up window the cap follows WARMUP_SCHEDULE
    (one rung per WARMUP_STEP_DAYS days), never exceeding DAILY_EMAIL_CAP. Once the
    schedule is exhausted — or no warm-up is configured — it's just DAILY_EMAIL_CAP."""
    if not WARMUP_START_DATE or not WARMUP_SCHEDULE:
        return DAILY_EMAIL_CAP
    try:
        start = date.fromisoformat(WARMUP_START_DATE)
    except ValueError:
        logger.warning(f"[SMTP] Invalid WARMUP_START_DATE '{WARMUP_START_DATE}'; warm-up ramp ignored.")
        return DAILY_EMAIL_CAP
    days_in = (date.today() - start).days
    idx = days_in // max(1, WARMUP_STEP_DAYS)
    if idx < 0:
        idx = 0  # start date in the future — stay on the first (most conservative) rung
    if idx >= len(WARMUP_SCHEDULE):
        return DAILY_EMAIL_CAP
    return min(WARMUP_SCHEDULE[idx], DAILY_EMAIL_CAP)


def _budget_send_ceiling() -> int:
    """Max sends that fit the run's time budget at worst-case pacing
    (RUN_TIME_BUDGET_SECONDS ÷ MAX_SEND_GAP_SECONDS). Self-governs the daily cap so
    the warm-up ramp / DAILY_EMAIL_CAP can never outgrow the workflow timeout: the
    run always ends gracefully instead of being hard-killed mid-send. Floor of 1 so
    a misconfigured (tiny) budget can't silently zero outreach — the engine's
    in-loop deadline check is the hard stop."""
    return max(1, RUN_TIME_BUDGET_SECONDS // max(MAX_SEND_GAP_SECONDS, 1))


def effective_daily_cap() -> int:
    """Today's send cap: the warm-up-ramp cap, additionally bounded by how many
    paced sends physically fit inside the run's time budget."""
    return min(_ramp_cap(), _budget_send_ceiling())


def cap_remaining() -> int:
    return max(0, effective_daily_cap() - _session.sends_today)


def is_cap_hit() -> bool:
    return _session.sends_today >= effective_daily_cap()


def send_email(to_email: str, subject: str, body: str, from_name: str = "", pace: bool = True) -> bool:
    """
    Send a single email via Brevo SMTP.
    Respects daily cap. Tracks session health.
    Returns True on success, False on failure.
    Pacing: the outreach engine passes pace=False and calls pace_sleep() itself
    AFTER the send is recorded in the Sheet — sleeping in here (between the SMTP
    send and the caller's status/log writes) is the duplicate-send window a hard
    kill exploits. pace=True (legacy default) still paces in-call for any other
    caller; operator notifications pass pace=False and only wait SEND_DELAY_SECONDS.
    """
    if is_cap_hit():
        logger.warning(f"[SMTP] Daily cap of {effective_daily_cap()} hit. Skipping send to {to_email}.")
        _session.cap_hit = True
        return False

    if _session.health_degraded:
        logger.error(f"[SMTP] Health degraded. Refusing send to {to_email}.")
        return False

    # Plain-text only: with no HTML part Brevo cannot inject an open-tracking pixel,
    # and there are no HTML links to rewrite for click tracking — both hurt cold-email
    # inbox placement. (Also disable account-level link/open tracking in the Brevo
    # dashboard for the fullest effect.)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{SMTP_FROM}>" if from_name else SMTP_FROM
    msg["To"] = to_email

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
    # Never log any of the secret itself (not even its length or prefix) — this line
    # lands in logs/phase2_run.log, which is uploaded as a 30-day GitHub artifact.
    logger.info(f"[SMTP] Authenticating as: {user_display} | key_present={bool(SMTP_PASS)}")

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
        if pace:
            pace_sleep()
        else:
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
    """Alias for send_email without a from_name. Used for operator notifications —
    skips the long cold-send pacing gap (pace=False)."""
    return send_email(to_email=to_email, subject=subject, body=body, pace=False)
