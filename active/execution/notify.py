"""
notify.py — Operator notification emails.
Sends phase summaries and alert emails via Gmail App Password (not Brevo).
Brevo is reserved for prospect outreach only.
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

from config import GMAIL_SENDER, GMAIL_APP_PASSWORD, NOTIFY_EMAIL

logger = logging.getLogger(__name__)

GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 587


def _send_via_gmail(subject: str, body: str) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(GMAIL_HOST, GMAIL_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, [NOTIFY_EMAIL], msg.as_string())
        logger.info(f"[NOTIFY] Sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"[NOTIFY] Gmail send failed: {e}")
        return False


def send_run_summary(
    new_leads: int = 0,
    dupes_skipped: int = 0,
    icp_rejected: int = 0,
    enrichment_results: dict = None,
    verify_results: dict = None,
    social_results: dict = None,
    followup_staged: int = 0,
    errors: list[str] = None,
) -> None:
    """Phase 1 summary email to operator."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    enrichment_results = enrichment_results or {}
    verify_results = verify_results or {}
    social_results = social_results or {}
    errors = errors or []

    lines = [
        f"Lead Manager — Phase 1 Summary",
        f"Run time: {now}",
        "",
        f"New leads written:     {new_leads}",
        f"Duplicates skipped:    {dupes_skipped}",
        f"Off-ICP rejected:      {icp_rejected}",
        f"Follow-ups staged:     {followup_staged}",
        "",
        "Enrichment results:",
        f"  Discovery + email:       Vibe-only (Prospeo/Apify/Serper disabled)",
        f"  Email-less leads:        kept for social outreach (not deleted)",
        f"  LinkedIn URLs found:     {social_results.get('li_found', 0)}",
        f"  LinkedIn not found:      {social_results.get('li_not_found', 0)}",
        f"  Facebook URLs found:     {social_results.get('fb_found', 0)}",
        f"  Facebook not found:      {social_results.get('fb_not_found', 0)}",
    ]

    # Email verification block (BillionVerify) — only shown when the step ran.
    lines += ["", "Email verification (BillionVerify):"]
    if verify_results.get("skipped"):
        lines.append(f"  Skipped: {verify_results.get('reason', 'n/a')}")
    elif not verify_results:
        lines.append("  Not run.")
    else:
        by_status = verify_results.get("by_status", {})
        status_str = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "—"
        lines += [
            f"  Verified:                {verify_results.get('verified', 0)}",
            f"  Kept (valid/catchall/role): {verify_results.get('kept', 0)}",
            f"  Removed (email blanked):  {verify_results.get('removed', 0)}",
            f"  Breakdown:               {status_str}",
            f"  Credits before run:      {verify_results.get('credits_before', 'unknown')}",
        ]

    if errors:
        lines += ["", f"Errors ({len(errors)}):"]
        for err in errors[:20]:
            lines.append(f"  - {err}")
        if len(errors) > 20:
            lines.append(f"  ... and {len(errors) - 20} more. Check pipeline_errors.jsonl.")
    else:
        lines += ["", "No errors."]

    _send_via_gmail(
        subject=f"[Lead Manager] Phase 1 Summary — {now}",
        body="\n".join(lines),
    )


def send_outreach_summary(
    initial_sent: int = 0,
    followup_sent: int = 0,
    failed: int = 0,
    cap_hit: bool = False,
    health_degraded: bool = False,
    errors: list[str] = None,
    suppressed_unsub: int = 0,
    suppressed_bounced: int = 0,
    suppressed_new: int = 0,
) -> None:
    """Phase 2 summary email to operator."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    errors = errors or []

    lines = [
        f"Lead Manager — Phase 2 Outreach Summary",
        f"Run time: {now}",
        "",
        f"Touch 1 (initial) sent:   {initial_sent}",
        f"Follow-up (Touch 2+) sent: {followup_sent}",
        f"Failed sends:             {failed}",
        f"Daily cap hit:            {'YES' if cap_hit else 'no'}",
        f"SMTP health degraded:     {'YES — CHECK IMMEDIATELY' if health_degraded else 'no'}",
        f"Brevo suppressions:       {suppressed_unsub} unsub, {suppressed_bounced} bounced "
        f"({suppressed_new} newly added to Suppression tab)",
    ]

    if errors:
        lines += ["", f"Errors ({len(errors)}):"]
        for err in errors[:20]:
            lines.append(f"  - {err}")
    else:
        lines += ["", "No errors."]

    subject = f"[Lead Manager] Phase 2 Summary — {now}"
    if health_degraded:
        subject = f"[ALERT] SMTP Health Degraded — {now}"

    _send_via_gmail(subject=subject, body="\n".join(lines))


def alert_scrape_zero_results(source: str) -> None:
    """Alert operator when a discovery source returns zero leads."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _send_via_gmail(
        subject=f"[Lead Manager] Zero Results — {source} — {now}",
        body=(
            f"Source '{source}' returned zero leads at {now}.\n\n"
            f"Pipeline continued with remaining sources.\n"
            f"Check source health or ICP filters if this repeats."
        ),
    )


def alert_smtp_degraded(fail_rate: float, errors: list[str]) -> None:
    """Alert operator when SMTP health threshold is breached."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    error_lines = "\n".join(f"  - {e}" for e in errors[:10])
    _send_via_gmail(
        subject=f"[ALERT] SMTP Health Degraded — {now}",
        body=(
            f"SMTP failure rate: {fail_rate:.0%}\n"
            f"Threshold: >50% after 5+ sends.\n\n"
            f"Outreach halted. Check Brevo credentials and sending domain.\n\n"
            f"Recent errors:\n{error_lines}"
        ),
    )


def alert_pipeline_error(stage: str, message: str) -> None:
    """Generic pipeline error alert."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _send_via_gmail(
        subject=f"[Lead Manager] Error in {stage} — {now}",
        body=f"Stage: {stage}\nTime: {now}\n\nError:\n{message}",
    )


def send_social_summary(touch_stats: list[dict], dry_run: bool = False) -> None:
    """Social outreach summary email to operator."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prefix = "[DRY RUN] " if dry_run else ""

    lines = [
        f"Lead Manager — {prefix}LinkedIn Social Outreach Summary",
        f"Run time: {now}",
        "",
    ]
    for s in touch_stats:
        lines.append(
            f"  Touch {s['touch_number']}: targeted={s['targeted']}, "
            f"failed={s['failed']}, launched={s.get('launched')}, succeeded={s.get('succeeded')}"
        )

    total_failed = sum(s["failed"] for s in touch_stats)
    lines += ["", "No errors." if total_failed == 0 else f"Total failed sends: {total_failed}"]

    _send_via_gmail(
        subject=f"[Lead Manager] {prefix}Social Outreach Summary — {now}",
        body="\n".join(lines),
    )


def alert_token_exhausted(source: str, details: str = "") -> None:
    """Alert operator when an API source runs out of credits/quota."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body_lines = [
        f"Source: {source}",
        f"Time:   {now}",
        "",
        "The API returned a credit/quota exhaustion error.",
        "Pipeline continued but this source produced no leads.",
        "",
        "Action required: log in and recharge your credits, then re-run Phase 1.",
    ]
    if details:
        body_lines += ["", f"Details: {details}"]
    _send_via_gmail(
        subject=f"[ALERT] Token Exhausted — {source} — {now}",
        body="\n".join(body_lines),
    )
