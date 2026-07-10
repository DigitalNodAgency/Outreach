"""
outreach_engine.py — Core outreach send logic.
Handles initial outreach (Touch 1) and follow-up outreach (Touch 2..MAX_FOLLOWUPS).
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import variation_engine
from config import (
    FOLLOWUP_DELAY_DAYS, MAX_FOLLOWUPS, TEMPLATES_DIR,
    STATUS_NEW, STATUS_OUTREACH_SENT, STATUS_FOLLOWUP_SENT,
    STATUS_REPLIED, STATUS_CLOSED, STATUS_FAILED,
    STATUS_UNSUBSCRIBED, STATUS_BOUNCED,
    REGION_TEMPLATE_MAP, DEFAULT_TEMPLATE_PREFIX,
    DAILY_EMAIL_CAP, CALENDLY_URL, SENDER_NAME,
)
from smtp_client import send_email, is_cap_hit, get_session, pace_sleep

logger = logging.getLogger(__name__)

# Never send a follow-up to a lead in one of these states (mirrors the social path).
# Belt-and-suspenders: get_leads_by_status already filters, but this protects against
# a reply that landed after the status fetch and against future query broadening.
# unsubscribed/bounced are terminal do-not-contact states set by the Brevo suppression
# sync — excluded here so a re-classified lead can never slip into a follow-up batch.
FOLLOWUP_EXCLUDED_STATUSES = {
    STATUS_REPLIED, STATUS_CLOSED, STATUS_FAILED,
    STATUS_UNSUBSCRIBED, STATUS_BOUNCED,
}


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


def run_initial_outreach(outreach_log_cache: set, suppression=None, deadline: float | None = None) -> dict:
    """
    Send Touch 1 to all leads with status=new.
    Updates Sheets status → outreach_sent on success.
    Halts if daily cap hit, SMTP health degrades, or the run's time budget
    (`deadline`, a time.monotonic() timestamp) is reached — a budget stop is a
    normal outcome (leads defer to the next run), never an error.
    Skips do-not-contact (suppression) leads and varies copy per lead so no two leads
    in the batch receive identical text.
    """
    from sheets_client import get_leads_by_status, update_lead_status, append_outreach_log

    suppression = _norm_suppression(suppression)
    stats = {
        "sent": 0, "failed": 0, "skipped": 0, "suppressed": 0, "cap_hit": False,
        "time_budget_hit": False, "deferred": 0,
    }
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

    for i, lead in enumerate(leads):
        # Graceful time-budget stop: never START a send we may not have time to
        # record + pace. Deferred leads go out on the next daily run. Deliberately
        # does NOT touch session.health_degraded — exit code stays 0.
        if deadline is not None and time.monotonic() >= deadline:
            stats["time_budget_hit"] = True
            stats["deferred"] = len(leads) - i
            logger.info(
                f"[OUTREACH] Run time budget reached — stopping Touch 1, "
                f"{stats['deferred']} leads deferred to the next run."
            )
            break

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
        # pace=False: the pacing sleep must come AFTER the Sheet writes below, not
        # inside send_email — a hard kill during a sleep between the SMTP send and
        # update_lead_status/append_outreach_log strands a sent-but-unrecorded
        # lead, which the next run would re-send (duplicate email).
        success = send_email(to_email=email, subject=subject, body=body, from_name=SENDER_NAME, pace=False)

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
            if i < len(leads) - 1:  # no trailing sleep after the batch's last lead
                pace_sleep(deadline)
        else:
            update_lead_status(email, STATUS_FAILED)
            stats["failed"] += 1

    logger.info(
        f"[OUTREACH] Touch 1 done. Sent: {stats['sent']}, Failed: {stats['failed']}, "
        f"Skipped: {stats['skipped']}, Suppressed: {stats['suppressed']}"
    )
    return stats


def run_followup_outreach(outreach_log_cache: set, suppression=None, deadline: float | None = None) -> dict:
    """
    Send the next follow-up touch (Touch 2..MAX_FOLLOWUPS) to eligible leads.
    Eligibility: status=outreach_sent or followup_sent AND last_contacted >= FOLLOWUP_DELAY_DAYS ago.
    Touch number = followup_count + 1; loads touch-standard-{N}.txt. The lead is closed
    once followup_count reaches MAX_FOLLOWUPS or no template exists for the next touch.
    Skips if initial outreach hit the daily cap. Stops gracefully at the run's time
    budget (`deadline`, time.monotonic() timestamp) — deferred leads go out next run.
    Honors the do-not-contact list and varies copy per lead. Re: threaded touches
    reuse the lead's exact Touch-1 subject.
    """
    from sheets_client import (
        get_leads_by_status, update_lead_status, append_outreach_log, get_stage_subjects,
    )

    suppression = _norm_suppression(suppression)
    stats = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0, "time_budget_hit": False, "deferred": 0}

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

    for i, lead in enumerate(leads):
        # Graceful time-budget stop (mirrors Touch 1). Remaining count is the raw
        # candidate tail — not all of it is delay-eligible, hence "~" in the summary.
        if deadline is not None and time.monotonic() >= deadline:
            stats["time_budget_hit"] = True
            stats["deferred"] = len(leads) - i
            logger.info(
                f"[FOLLOWUP] Run time budget reached — stopping follow-ups, "
                f"~{stats['deferred']} candidates deferred to the next run."
            )
            break

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
        # Subject: thread EVERY follow-up (including the breakup) off the lead's own Touch-1
        # subject. A fresh-subject follow-up from a young sending domain reads to Gmail as a
        # brand-new cold email and gets filtered to Promotions / archived out of the inbox,
        # whereas a Re: reply rides the existing (engaged) conversation and lands in Primary.
        # This is exactly why Touches 2-3 (Re:) arrived but a fresh-subject breakup did not.
        # Falls back to the variant's / flat subject only when Touch 1 isn't on record.
        recall = stage1_subjects.get(email.lower())
        if recall:
            subject_tpl = recall if recall.lower().startswith("re:") else f"Re: {recall}"
        elif variant and variant.get("subject"):
            subject_tpl = variant["subject"]
        else:
            subject_tpl = flat_subject

        subject, body = _render_template(subject_tpl, body_tpl, name, company)
        # pace=False: pacing sleep happens AFTER the Sheet writes (see Touch 1 note —
        # sleeping before them is the duplicate-send window on a hard kill).
        success = send_email(to_email=email, subject=subject, body=body, from_name=SENDER_NAME, pace=False)

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
            if i < len(leads) - 1:  # no trailing sleep after the batch's last lead
                pace_sleep(deadline)
        else:
            update_lead_status(email, STATUS_FAILED)
            stats["failed"] += 1

    logger.info(
        f"[FOLLOWUP] Done. Sent: {stats['sent']}, Failed: {stats['failed']}, "
        f"Skipped: {stats['skipped']}, Suppressed: {stats['suppressed']}"
    )
    return stats
