"""
ingest_vibe_export.py — Phase 1 Vibe Prospecting ingestion.
Loads vibe_export.csv, structures in batches of 10, deduplicates, writes to Sheets.
Entry point: run directly or imported by Phase 1 orchestrator.
"""

import csv
import logging
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from config import (
    VIBE_EXPORT_CSV, STRUCTURING_BATCH_SIZE, MAX_LEADS_PER_RUN,
    STATUS_NEW,
)
from pipeline_metrics import (
    should_skip_source, record_source_run, record_run_stats,
    log_pipeline_error, log_failed_record,
)

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
SOURCE_NAME = "vibe_prospecting"


def _source_tag() -> str:
    return f"vibe_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def _validate_email(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email.strip()))


def _is_person_name(name: str) -> bool:
    parts = name.strip().split()
    if not (2 <= len(parts) <= 5):
        return False
    return all(p.isalpha() for p in parts)


def _normalize_record(raw: dict, source_tag: str) -> dict | None:
    """
    Normalize a raw Vibe CSV row to Sheets schema.
    Returns None and logs to failed_records if required fields missing.
    """
    email = raw.get("email", "").strip()
    if not email or not _validate_email(email):
        log_failed_record(raw, reason="missing_or_invalid_email")
        return None

    name = raw.get("name", raw.get("full_name", "")).strip()
    company = raw.get("company", raw.get("company_name", "")).strip()
    region = raw.get("region", raw.get("country", raw.get("location", ""))).strip().upper()

    if not company:
        log_failed_record(raw, reason="missing_company")
        return None

    return {
        "name": name,
        "email": email.lower(),
        "company": company,
        "region": region,
        "warmth_score": raw.get("warmth_score", ""),
        "status": STATUS_NEW,
        "last_contacted": "",
        "followup_count": 0,
        "notes": f"source:{source_tag}",
    }


def _deduplicate(
    candidates: list[dict],
    existing_emails: set[str],
) -> tuple[list[dict], int]:
    """
    Two-level dedup:
    Level 1 — exact email match (case-insensitive)
    Level 2 — domain + name similarity >85% (fuzzy, keep most senior)
    Returns (clean_leads, dupe_count).
    """
    try:
        from rapidfuzz import fuzz
        has_fuzzy = True
    except ImportError:
        has_fuzzy = False
        logger.warning("[DEDUP] rapidfuzz not installed. Fuzzy dedup skipped.")

    SENIORITY = ["founder", "ceo", "owner", "director", "manager"]

    def seniority_score(title: str) -> int:
        t = title.lower()
        for i, keyword in enumerate(SENIORITY):
            if keyword in t:
                return i
        return len(SENIORITY)

    seen_emails: set[str] = set(existing_emails)
    seen_domains: dict[str, dict] = {}
    clean = []
    dupe_count = 0

    for lead in candidates:
        email = lead["email"].lower()
        domain = email.split("@")[-1] if "@" in email else ""

        # Level 1 — exact email
        if email in seen_emails:
            logger.debug(f"[DEDUP] Exact email match — skipping: {email}")
            dupe_count += 1
            continue

        # Level 2 — domain + name fuzzy
        if has_fuzzy and domain and domain in seen_domains:
            existing = seen_domains[domain]
            name_sim = fuzz.ratio(lead["name"].lower(), existing["name"].lower())
            if name_sim > 85:
                # Keep most senior
                if seniority_score(lead.get("notes", "")) < seniority_score(existing.get("notes", "")):
                    seen_domains[domain] = lead
                logger.debug(f"[DEDUP] Domain+name fuzzy match — skipping: {email}")
                dupe_count += 1
                continue

        seen_emails.add(email)
        seen_domains[domain] = lead
        clean.append(lead)

    return clean, dupe_count


def load_vibe_csv() -> list[dict]:
    """Load raw rows from vibe_export.csv."""
    if not os.path.exists(VIBE_EXPORT_CSV):
        log_pipeline_error("vibe_ingest", f"CSV not found: {VIBE_EXPORT_CSV}", source=SOURCE_NAME)
        return []
    rows = []
    with open(VIBE_EXPORT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    logger.info(f"[VIBE] Loaded {len(rows)} raw rows from CSV.")
    return rows


def run_vibe_ingest() -> dict:
    """
    Main entry point for Vibe ingestion.
    Returns stats dict: new_leads, dupes_skipped, failed.
    """
    from sheets_client import get_existing_emails, append_leads_batch

    stats = {"new_leads": 0, "dupes_skipped": 0, "failed": 0, "source": SOURCE_NAME}

    if should_skip_source(SOURCE_NAME):
        logger.info(f"[VIBE] Source skipped due to health check.")
        return stats

    raw_rows = load_vibe_csv()
    if not raw_rows:
        record_source_run(SOURCE_NAME, 0)
        return stats

    source_tag = _source_tag()
    normalized = []

    # Structure in batches of STRUCTURING_BATCH_SIZE (context window protection)
    for i in range(0, len(raw_rows), STRUCTURING_BATCH_SIZE):
        batch = raw_rows[i: i + STRUCTURING_BATCH_SIZE]
        for raw in batch:
            record = _normalize_record(raw, source_tag)
            if record:
                normalized.append(record)
            else:
                stats["failed"] += 1

        if len(normalized) >= MAX_LEADS_PER_RUN:
            normalized = normalized[:MAX_LEADS_PER_RUN]
            logger.info(f"[VIBE] MAX_LEADS_PER_RUN cap reached: {MAX_LEADS_PER_RUN}")
            break

    if not normalized:
        record_source_run(SOURCE_NAME, 0)
        return stats

    existing_emails = get_existing_emails()
    clean, dupe_count = _deduplicate(normalized, existing_emails)
    stats["dupes_skipped"] = dupe_count

    if clean:
        written = append_leads_batch(clean)
        stats["new_leads"] = written
        has_email_pct = sum(1 for l in clean if l.get("email")) / len(clean)
        record_run_stats(SOURCE_NAME, written, has_email_pct, dupe_count)

    record_source_run(SOURCE_NAME, len(clean))
    logger.info(f"[VIBE] Done. Written: {stats['new_leads']}, Dupes: {dupe_count}, Failed: {stats['failed']}")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_vibe_ingest()
    print(result)
