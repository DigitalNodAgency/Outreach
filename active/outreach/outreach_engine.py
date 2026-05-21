"""
outreach_engine.py — Core outreach send logic.
Handles initial outreach (Touch 1) and follow-up outreach (Touch 2/3).
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from config import (
    FOLLOWUP_DELAY_DAYS, MAX_FOLLOWUPS, TEMPLATES_DIR,
    STATUS_NEW, STATUS_OUTREACH_SENT, STATUS_FOLLOWUP_SENT,
    STATUS_CLOSED, STATUS_FAILED, REGION_TEMPLATE_MAP, DEFAULT_TEMPLATE_PREFIX,
    DAILY_EMAIL_CAP, CALENDLY_URL,
)
from smtp_client import send_email, is_cap_hit, get_session

logger = logging.getLogger(__name__)


def _load_template(prefix: str, touch_number: int) -> tuple[str, str]:
    """
    Load subject and body from template file.
    File format: Line 1 = subject | Lines 2+ = body.
    Returns (subject, body). Raises FileNotFoundError if missing.
    """
    filename = f"{prefix}-{touch_number}.txt"
    path = os.path.join(TEMPLATES_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    subject = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    return subject, body


def _render_template(subject: str, body: str, name: str, company: str) -> tuple[str, str]:
    """Replace {{name}}, {{company}}, and {{calendly_url}} placeholders."""
    first_name = name.split()[0] if name.strip() else "there"
    replacements = [("{{name}}", first_name), ("{{company}}", company), ("{{calendly_url}}", CALENDLY_URL)]
    for placeholder, value in replacements:
        subject = subject.replace(placeholder, value)
        body = body.replace(placeholder, value)
    return subject, body


def _resolve_template_prefix(region: str) -> str:
    key = region.strip().lower()
    return REGION_TEMPLATE_MAP.get(key, DEFAULT_TEMPLATE_PREFIX)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_initial_outreach(outreach_log_cache: set) -> dict:
    """
    Send Touch 1 to all leads with status=new.
    Updates Sheets status → outreach_sent on success.
    Halts if daily cap hit or SMTP health degrades.
    """
    from sheets_client import get_leads_by_status, update_lead_status, append_outreach_log

    stats = {"sent": 0, "failed": 0, "skipped": 0, "cap_hit": False}
    leads = get_leads_by_status(STATUS_NEW)

    if not leads:
        logger.info("[OUTREACH] No new leads for Touch 1.")
        return stats

    logger.info(f"[OUTREACH] Touch 1 — {len(leads)} leads queued.")

    for lead in leads:
        if is_cap_hit():
            logger.warning(f"[OUTREACH] Daily cap hit. Stopping Touch 1.")
            stats["cap_hit"] = True
            break

        session = get_session()
        if session.health_degraded:
            logger.error("[OUTREACH] SMTP health degraded. Halting Touch 1.")
            break

        email = lead.get("email", "").strip()
        name = lead.get("name", "").strip()
        company = lead.get("company", "").strip()
        region = lead.get("region", "").strip()

        if not email:
            logger.warning(f"[OUTREACH] Skipping lead with no email: {name}")
            stats["skipped"] += 1
            continue

        prefix = _resolve_template_prefix(region)
        try:
            subject_tpl, body_tpl = _load_template(prefix, 1)
        except FileNotFoundError as e:
            logger.error(f"[OUTREACH] {e}")
            stats["failed"] += 1
            continue

        subject, body = _render_template(subject_tpl, body_tpl, name, company)
        success = send_email(to_email=email, subject=subject, body=body)

        if success:
            now = _now_iso()
            update_lead_status(email, STATUS_OUTREACH_SENT, last_contacted=now, followup_count=1)
            append_outreach_log({
                "lead_email": email,
                "lead_name": name,
                "sequence_type": prefix,
                "stage_number": 1,
                "email_subject": subject,
                "sent_date": now,
                "status": "sent",
            }, outreach_log_cache)
            stats["sent"] += 1
        else:
            update_lead_status(email, STATUS_FAILED)
            stats["failed"] += 1

    logger.info(f"[OUTREACH] Touch 1 done. Sent: {stats['sent']}, Failed: {stats['failed']}, Skipped: {stats['skipped']}")
    return stats


def run_followup_outreach(outreach_log_cache: set) -> dict:
    """
    Send Touch 2 or Touch 3 to eligible leads.
    Eligibility: status=outreach_sent or followup_sent AND last_contacted >= FOLLOWUP_DELAY_DAYS ago.
    Skips if initial outreach hit the daily cap.
    """
    from sheets_client import get_leads_by_status, update_lead_status, append_outreach_log

    stats = {"sent": 0, "failed": 0, "skipped": 0}

    if is_cap_hit():
        logger.info("[FOLLOWUP] Daily cap already hit. Skipping follow-up run.")
        return stats

    eligible_statuses = [STATUS_OUTREACH_SENT, STATUS_FOLLOWUP_SENT]
    leads = []
    for s in eligible_statuses:
        leads.extend(get_leads_by_status(s))

    if not leads:
        logger.info("[FOLLOWUP] No leads eligible for follow-up.")
        return stats

    cutoff = datetime.now(timezone.utc) - timedelta(days=FOLLOWUP_DELAY_DAYS)
    logger.info(f"[FOLLOWUP] {len(leads)} candidates. Cutoff: {cutoff.date()}")

    for lead in leads:
        if is_cap_hit():
            logger.warning("[FOLLOWUP] Daily cap hit mid-run. Stopping.")
            break

        session = get_session()
        if session.health_degraded:
            logger.error("[FOLLOWUP] SMTP health degraded. Halting follow-up.")
            break

        email = lead.get("email", "").strip()
        name = lead.get("name", "").strip()
        company = lead.get("company", "").strip()
        region = lead.get("region", "").strip()
        followup_count = int(lead.get("followup_count", 0) or 0)
        last_contacted_str = lead.get("last_contacted", "")

        if not email:
            stats["skipped"] += 1
            continue

        if followup_count >= MAX_FOLLOWUPS:
            update_lead_status(email, STATUS_CLOSED)
            stats["skipped"] += 1
            continue

        if not last_contacted_str:
            stats["skipped"] += 1
            continue

        try:
            last_contacted = datetime.fromisoformat(last_contacted_str).replace(tzinfo=timezone.utc)
        except ValueError:
            stats["skipped"] += 1
            continue

        if last_contacted > cutoff:
            stats["skipped"] += 1
            continue

        touch_number = followup_count + 1
        prefix = _resolve_template_prefix(region)

        try:
            subject_tpl, body_tpl = _load_template(prefix, touch_number)
        except FileNotFoundError as e:
            logger.error(f"[FOLLOWUP] {e}")
            stats["failed"] += 1
            continue

        subject, body = _render_template(subject_tpl, body_tpl, name, company)
        success = send_email(to_email=email, subject=subject, body=body)

        new_count = followup_count + 1
        new_status = STATUS_CLOSED if new_count >= MAX_FOLLOWUPS else STATUS_FOLLOWUP_SENT

        if success:
            now = _now_iso()
            update_lead_status(email, new_status, last_contacted=now, followup_count=new_count)
            append_outreach_log({
                "lead_email": email,
                "lead_name": name,
                "sequence_type": prefix,
                "stage_number": touch_number,
                "email_subject": subject,
                "sent_date": now,
                "status": "sent",
            }, outreach_log_cache)
            stats["sent"] += 1
        else:
            update_lead_status(email, STATUS_FAILED)
            stats["failed"] += 1

    logger.info(f"[FOLLOWUP] Done. Sent: {stats['sent']}, Failed: {stats['failed']}, Skipped: {stats['skipped']}")
    return stats
