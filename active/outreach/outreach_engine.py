"""
outreach_engine.py — Core outreach send logic.
Handles initial outreach (Touch 1) and follow-up outreach (Touch 2..MAX_FOLLOWUPS).
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import variation_engine
from config import (
    FOLLOWUP_DELAY_DAYS, MAX_FOLLOWUPS, TEMPLATES_DIR,
    STATUS_NEW, STATUS_OUTREACH_SENT, STATUS_FOLLOWUP_SENT,
    STATUS_REPLIED, STATUS_CLOSED, STATUS_FAILED,
    REGION_TEMPLATE_MAP, DEFAULT_TEMPLATE_PREFIX,
    DAILY_EMAIL_CAP, CALENDLY_URL, SENDER_NAME,
)
from smtp_client import send_email, is_cap_hit, get_session

logger = logging.getLogger(__name__)

# Never send a follow-up to a lead in one of these states (mirrors the social path).
# Belt-and-suspenders: get_leads_by_status already filters, but this protects against
# a reply that landed after the status fetch and against future query broadening.
FOLLOWUP_EXCLUDED_STATUSES = {STATUS_REPLIED, STATUS_CLOSED, STATUS_FAILED}


def _norm_suppression(suppression) -> tuple[set, set]:
    """Coerce a possibly-None suppression arg into (emails, domains) sets."""
    if not suppression:
        return set(), set()
    return suppression


def _is_suppressed(email: str, suppression: tuple[set, set]) -> bool:
    """True if the email — or its domain — is on the do-not-contact list."""
    emails, domains = suppression
    e = email.strip().lower()
    if e in emails:
        return True
    domain = e.split("@")[-1] if "@" in e else ""
    return bool(domain) and domain in domains


def _build_followup_plans(leads: list[dict]) -> dict:
    """Variation plans keyed by (prefix, touch_number) for a follow-up batch, so copy is
    unique within each touch group. touch_number = followup_count + 1."""
    groups: dict = defaultdict(list)
    for lead in leads:
        em = lead.get("email", "").strip().lower()
        if not em:
            continue
        touch = int(lead.get("followup_count", 0) or 0) + 1
        pfx = _resolve_template_prefix(lead.get("region", "").strip())
        groups[(pfx, touch)].append(em)
    return {key: variation_engine.build_plan(key[0], key[1], ems) for key, ems in groups.items()}


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
    replacements = [
        ("{{name}}", first_name),
        ("{{company}}", company),
        ("{{calendly_url}}", CALENDLY_URL),
        ("{{sender_name}}", SENDER_NAME),
    ]
    for placeholder, value in replacements:
        subject = subject.replace(placeholder, value)
        body = body.replace(placeholder, value)
    return subject, body


def _resolve_template_prefix(region: str) -> str:
    key = region.strip().lower()
    prefix = REGION_TEMPLATE_MAP.get(key, DEFAULT_TEMPLATE_PREFIX)
    # Fall back to the default series if the mapped templates don't exist on disk
    # (e.g. AU/NZ maps to touch-aunz but no touch-aunz-*.txt files were ever created).
    if not os.path.exists(os.path.join(TEMPLATES_DIR, f"{prefix}-1.txt")):
        if prefix != DEFAULT_TEMPLATE_PREFIX:
            logger.warning(
                f"[OUTREACH] Template series '{prefix}' missing; "
                f"falling back to '{DEFAULT_TEMPLATE_PREFIX}' for region '{region}'."
            )
        return DEFAULT_TEMPLATE_PREFIX
    return prefix


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warmth_of(lead: dict) -> float:
    """Parse a lead's warmth_score, treating blank/garbage as 0."""
    try:
        return float(lead.get("warmth_score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def run_initial_outreach(outreach_log_cache: set, suppression=None) -> dict:
    """
    Send Touch 1 to all leads with status=new.
    Updates Sheets status → outreach_sent on success.
    Halts if daily cap hit or SMTP health degrades.
    Skips do-not-contact (suppression) leads and varies copy per lead so no two leads
    in the batch receive identical text.
    """
    from sheets_client import get_leads_by_status, update_lead_status, append_outreach_log

    suppression = _norm_suppression(suppression)
    stats = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0, "cap_hit": False}
    leads = get_leads_by_status(STATUS_NEW)

    if not leads:
        logger.info("[OUTREACH] No new leads for Touch 1.")
        return stats

    # Highest-warmth leads first, so the best-fit prospects go out before the
    # daily (possibly warm-up-throttled) cap is reached.
    leads.sort(key=_warmth_of, reverse=True)

    logger.info(f"[OUTREACH] Touch 1 — {len(leads)} leads queued.")

    # Variation plans per resolved prefix → unique copy for every lead in the batch.
    plans: dict = defaultdict(dict)
    by_prefix: dict = defaultdict(list)
    for lead in leads:
        em = lead.get("email", "").strip().lower()
        if em:
            by_prefix[_resolve_template_prefix(lead.get("region", "").strip())].append(em)
    for pfx, ems in by_prefix.items():
        plans[pfx] = variation_engine.build_plan(pfx, 1, ems)

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

        # Do-not-contact / suppression cross-check — hard gate before the send queue.
        if _is_suppressed(email, suppression):
            logger.info(f"[OUTREACH] Suppressed (do-not-contact): {email}. Closing lead.")
            update_lead_status(email, STATUS_CLOSED)
            stats["suppressed"] += 1
            continue

        prefix = _resolve_template_prefix(region)
        variant = plans.get(prefix, {}).get(email.lower())
        if variant:
            subject_tpl, body_tpl = variant["subject"], variant["body"]
        else:
            try:
                subject_tpl, body_tpl = _load_template(prefix, 1)
            except FileNotFoundError as e:
                logger.error(f"[OUTREACH] {e}")
                stats["failed"] += 1
                continue

        subject, body = _render_template(subject_tpl, body_tpl, name, company)
        success = send_email(to_email=email, subject=subject, body=body, from_name=SENDER_NAME)

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
            if variant:
                variation_engine.log_variant(email, 1, variant)
            stats["sent"] += 1
        else:
            update_lead_status(email, STATUS_FAILED)
            stats["failed"] += 1

    logger.info(
        f"[OUTREACH] Touch 1 done. Sent: {stats['sent']}, Failed: {stats['failed']}, "
        f"Skipped: {stats['skipped']}, Suppressed: {stats['suppressed']}"
    )
    return stats


def run_followup_outreach(outreach_log_cache: set, suppression=None) -> dict:
    """
    Send the next follow-up touch (Touch 2..MAX_FOLLOWUPS) to eligible leads.
    Eligibility: status=outreach_sent or followup_sent AND last_contacted >= FOLLOWUP_DELAY_DAYS ago.
    Touch number = followup_count + 1; loads touch-standard-{N}.txt. The lead is closed
    once followup_count reaches MAX_FOLLOWUPS or no template exists for the next touch.
    Skips if initial outreach hit the daily cap. Honors the do-not-contact list and
    varies copy per lead. Re: threaded touches reuse the lead's exact Touch-1 subject.
    """
    from sheets_client import (
        get_leads_by_status, update_lead_status, append_outreach_log, get_stage_subjects,
    )

    suppression = _norm_suppression(suppression)
    stats = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0}

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

    # Per-(prefix, touch) variation plans + Touch-1 subjects for proper Re: threading.
    plans = _build_followup_plans(leads)
    stage1_subjects = get_stage_subjects(1)

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
        status = lead.get("status", "").strip().lower()
        followup_count = int(lead.get("followup_count", 0) or 0)
        last_contacted_str = lead.get("last_contacted", "")

        if not email:
            stats["skipped"] += 1
            continue

        # Never chase a lead who already replied / is closed / failed.
        if status in FOLLOWUP_EXCLUDED_STATUSES:
            logger.info(f"[FOLLOWUP] Skipping {email} (status={status}).")
            stats["skipped"] += 1
            continue

        # Do-not-contact / suppression cross-check.
        if _is_suppressed(email, suppression):
            logger.info(f"[FOLLOWUP] Suppressed (do-not-contact): {email}. Closing lead.")
            update_lead_status(email, STATUS_CLOSED)
            stats["suppressed"] += 1
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
            flat_subject, flat_body = _load_template(prefix, touch_number)
        except FileNotFoundError:
            # No template for this touch = end of the configured sequence. Close the
            # lead gracefully instead of failing, so MAX_FOLLOWUPS can be raised beyond
            # the number of touch-standard-*.txt files without stranding leads at
            # status=failed. Effective ceiling = min(MAX_FOLLOWUPS, templates on disk).
            logger.warning(
                f"[FOLLOWUP] No template for touch {touch_number} (prefix '{prefix}'); "
                f"closing {email} at end of available sequence."
            )
            update_lead_status(email, STATUS_CLOSED)
            stats["skipped"] += 1
            continue

        variant = plans.get((prefix, touch_number), {}).get(email.lower())
        body_tpl = variant["body"] if variant else flat_body
        # Subject: thread off the lead's own Touch-1 subject when this touch is a Re:
        # thread; otherwise use the variant's fresh subject (e.g. breakup) or the flat one.
        recall = stage1_subjects.get(email.lower())
        if flat_subject.lower().startswith("re:") and recall:
            subject_tpl = f"Re: {recall}"
        elif variant and variant.get("subject"):
            subject_tpl = variant["subject"]
        else:
            subject_tpl = flat_subject

        subject, body = _render_template(subject_tpl, body_tpl, name, company)
        success = send_email(to_email=email, subject=subject, body=body, from_name=SENDER_NAME)

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
            if variant:
                variation_engine.log_variant(email, touch_number, variant)
            stats["sent"] += 1
        else:
            update_lead_status(email, STATUS_FAILED)
            stats["failed"] += 1

    logger.info(
        f"[FOLLOWUP] Done. Sent: {stats['sent']}, Failed: {stats['failed']}, "
        f"Skipped: {stats['skipped']}, Suppressed: {stats['suppressed']}"
    )
    return stats
