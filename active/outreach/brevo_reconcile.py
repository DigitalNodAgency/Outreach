"""
brevo_reconcile.py — Brevo API reconciliation.
Pulls full send history from Brevo, backfills rows missed by the script,
and deduplicates outreach_log. Idempotent — safe to run multiple times.
"""

import logging
import os
import requests
from datetime import datetime, timedelta, timezone

from config import BREVO_API_KEY, MAX_FOLLOWUPS

logger = logging.getLogger(__name__)

BREVO_BASE = "https://api.brevo.com/v3"
REQUEST_TIMEOUT = 30


def _headers() -> dict:
    return {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
    }


def _get_brevo_sent_emails(limit: int = 500, days_back: int = 90) -> list[dict]:
    """
    Pull recent sent email activity from Brevo /smtp/statistics/events.
    /smtp/emails requires email/messageId/templateId filter (unusable for bulk fetch).
    /smtp/statistics/events accepts just a startDate — returns event records.
    """
    url = f"{BREVO_BASE}/smtp/statistics/events"
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    params = {"limit": limit, "sort": "desc", "startDate": start_date, "endDate": end_date, "event": "requests"}
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            logger.error(
                f"[BREVO RECONCILE] GET /smtp/statistics/events returned {resp.status_code}: {resp.text[:500]}"
            )
            return []
        data = resp.json()
        return data.get("events", [])
    except Exception as e:
        logger.error(f"[BREVO RECONCILE] Failed to fetch Brevo email events: {e}")
        return []


def _classify_block_reason(code: str) -> str:
    """Map a Brevo blockedContacts reason.code to a terminal lead status.

    Brevo codes include unsubscribedViaEmail / unsubscribedViaMA / adminBlocked /
    hardBounce / contactFlaggedAsSpam / blockedByRecipient. Bounces → 'bounced';
    everything else (unsubscribe, spam complaint, admin block) → 'unsubscribed'.
    Unknown codes default to do-not-contact ('unsubscribed') — never re-contact."""
    c = (code or "").lower()
    if "bounce" in c:
        return "bounced"
    return "unsubscribed"


def fetch_blocked_contacts(page_limit: int = 100, max_pages: int = 50) -> list[dict]:
    """Pull Brevo's transactional do-not-contact list from GET /smtp/blockedContacts.

    Returns the CURRENT blocklist across every reason in one paginated sweep —
    unsubscribes, hard bounces, spam complaints, admin blocks — so a single call
    covers all suppression reasons. Each item: {"email", "status", "reason", "blocked_at"}
    where status is the terminal lead status ('unsubscribed' | 'bounced'). Read-only GET;
    returns [] on any error (non-fatal — suppression must never block a run)."""
    url = f"{BREVO_BASE}/smtp/blockedContacts"
    out: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        params = {"limit": page_limit, "offset": offset}
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
            if not resp.ok:
                logger.error(
                    f"[BREVO SUPPRESSION] GET /smtp/blockedContacts returned "
                    f"{resp.status_code}: {resp.text[:500]}"
                )
                break
            contacts = resp.json().get("contacts", [])
        except Exception as e:
            logger.error(f"[BREVO SUPPRESSION] Failed to fetch blocked contacts: {e}")
            break
        if not contacts:
            break
        for c in contacts:
            email = (c.get("email") or "").lower().strip()
            if not email:
                continue
            reason = c.get("reason") or {}
            code = reason.get("code", "") if isinstance(reason, dict) else str(reason)
            out.append({
                "email": email,
                "status": _classify_block_reason(code),
                "reason": code or (reason.get("message", "") if isinstance(reason, dict) else ""),
                "blocked_at": c.get("blockedAt", ""),
            })
        if len(contacts) < page_limit:
            break
        offset += page_limit
    logger.info(f"[BREVO SUPPRESSION] Fetched {len(out)} blocked/unsubscribed contact(s) from Brevo.")
    return out


def sync_brevo_suppression() -> dict:
    """Pull Brevo's do-not-contact list and reflect it in the Sheet.

    (1) append new addresses to the 'Suppression' tab (durable, cross-run send gate);
    (2) flip any active Leads row (new/outreach_sent/followup_sent) to its terminal
    status (unsubscribed/bounced). Idempotent — dedup happens in both writers.
    Returns {"fetched", "suppressed_new", "leads_marked", "unsub", "bounced"}."""
    from sheets_client import append_suppression, mark_leads_suppressed

    stats = {"fetched": 0, "suppressed_new": 0, "leads_marked": 0, "unsub": 0, "bounced": 0}

    contacts = fetch_blocked_contacts()
    stats["fetched"] = len(contacts)
    if not contacts:
        return stats

    stats["unsub"] = sum(1 for c in contacts if c["status"] == "unsubscribed")
    stats["bounced"] = sum(1 for c in contacts if c["status"] == "bounced")

    stats["suppressed_new"] = append_suppression(contacts)
    stats["leads_marked"] = mark_leads_suppressed({c["email"]: c["status"] for c in contacts})

    logger.info(f"[BREVO SUPPRESSION] Sync done: {stats}")
    return stats


def _get_brevo_contact_count() -> int:
    """Return total Brevo contact count for pre-sync comparison."""
    url = f"{BREVO_BASE}/contacts"
    try:
        resp = requests.get(url, headers=_headers(), params={"limit": 1}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("count", 0)
    except Exception as e:
        logger.error(f"[BREVO RECONCILE] Failed to get contact count: {e}")
        return 0


def run_reconcile(pre_sync: bool = False) -> dict:
    """
    Main reconciliation entry point.
    pre_sync=True: only run if Sheets lead count < Brevo contact count.
    pre_sync=False: always run (post-outreach).
    Returns stats dict.
    """
    from sheets_client import (
        get_all_leads, get_outreach_log_cache,
        append_outreach_log, dedup_outreach_log,
    )

    stats = {"backfilled": 0, "dupes_removed": 0, "skipped": False}

    if pre_sync:
        sheets_count = len(get_all_leads())
        brevo_count = _get_brevo_contact_count()
        if sheets_count >= brevo_count:
            logger.info(f"[BREVO RECONCILE] Pre-sync skipped: Sheets={sheets_count} >= Brevo={brevo_count}")
            stats["skipped"] = True
            return stats

    brevo_emails = _get_brevo_sent_emails()
    if not brevo_emails:
        logger.info("[BREVO RECONCILE] No Brevo send history returned.")
        dupes = dedup_outreach_log()
        stats["dupes_removed"] = dupes
        return stats

    existing_cache = get_outreach_log_cache()
    backfilled = 0

    for record in brevo_emails:
        to_email = record.get("email", "").lower().strip()
        subject = record.get("subject", "")
        sent_at = record.get("date", record.get("sentAt", ""))

        if not to_email:
            continue

        # Best-effort stage inference from subject. Touches that share the same "Re:"
        # subject can't be told apart here — this is reconciliation backfill only.
        # The breakup (final touch) maps to MAX_FOLLOWUPS so there's no hardcoded ceiling.
        stage_number = "1"
        subject_lower = subject.lower()
        if "closing the loop" in subject_lower or "breakup" in subject_lower:
            stage_number = str(MAX_FOLLOWUPS)
        elif "touch 3" in subject_lower or "last" in subject_lower:
            stage_number = "3"
        elif "re:" in subject_lower or "follow" in subject_lower or "touch 2" in subject_lower:
            stage_number = "2"

        key = (to_email, stage_number)
        if key in existing_cache:
            continue

        # Backfill missing row
        entry = {
            "lead_email": to_email,
            "lead_name": "",
            "sequence_type": "brevo_reconcile",
            "stage_number": stage_number,
            "email_subject": subject,
            "sent_date": sent_at,
            "status": "sent_brevo",
        }
        appended = append_outreach_log(entry, existing_cache)
        if appended:
            backfilled += 1

    stats["backfilled"] = backfilled

    # Always dedup at the end
    dupes = dedup_outreach_log()
    stats["dupes_removed"] = dupes

    logger.info(f"[BREVO RECONCILE] Done. Backfilled: {backfilled}, Dupes removed: {dupes}")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_reconcile(pre_sync=False)
    print(result)
