"""
brevo_reconcile.py — Brevo API reconciliation.
Pulls full send history from Brevo, backfills rows missed by the script,
and deduplicates outreach_log. Idempotent — safe to run multiple times.
"""

import logging
import os
import requests
from datetime import datetime, timedelta, timezone

from config import BREVO_API_KEY

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
    Pull recent sent email history from Brevo API.
    Returns list of send records with email, subject, sentAt.
    startDate is required by Brevo to avoid a 400 on accounts with send history.
    """
    url = f"{BREVO_BASE}/smtp/emails"
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = {"limit": limit, "sort": "desc", "startDate": start_date}
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            logger.error(
                f"[BREVO RECONCILE] GET /smtp/emails returned {resp.status_code}: {resp.text[:500]}"
            )
            return []
        data = resp.json()
        return data.get("transactionalEmails", [])
    except Exception as e:
        logger.error(f"[BREVO RECONCILE] Failed to fetch Brevo emails: {e}")
        return []


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

        # Try to infer stage number from subject (rough match — Touch 1/2/3)
        stage_number = "1"
        subject_lower = subject.lower()
        if "follow" in subject_lower or "touch 2" in subject_lower:
            stage_number = "2"
        elif "touch 3" in subject_lower or "last" in subject_lower:
            stage_number = "3"

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
