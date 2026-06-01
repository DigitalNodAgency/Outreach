"""
sheets_client.py — Google Sheets read/write client.
All Sheets I/O goes through this module. Batch-first, quota-safe.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import (
    GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID,
    LEADS_HEADERS, OUTREACH_LOG_HEADERS, REPLY_LOG_HEADERS, SOCIAL_LOG_HEADERS,
    COL_EMAIL, COL_STATUS, COL_LAST_CONTACTED, COL_FOLLOWUP_COUNT,
    COL_NAME, COL_COMPANY, COL_REGION, COL_FACEBOOK_URL, COL_LINKEDIN_URL,
    OLOG_LEAD_EMAIL, OLOG_STAGE_NUMBER,
    STATUS_NEW, STATUS_FAILED,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_client() -> gspread.Client:
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    logger.info(f"[SHEETS] Auth as: {creds_dict.get('client_email', '???')}")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_sheet(tab_name: str) -> gspread.Worksheet:
    client = _get_client()
    try:
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
    except PermissionError as e:
        raise PermissionError(
            f"403: service account has no access to sheet id={SPREADSHEET_ID!r}. "
            f"Share the sheet with the client_email logged above as Editor."
        ) from e
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=20)
        return ws


def ensure_headers(tab_name: str, headers: list[str]) -> None:
    """Write headers to row 1 if the sheet is empty."""
    ws = _get_sheet(tab_name)
    existing = ws.row_values(1)
    if not existing or existing[0] != headers[0]:
        ws.insert_row(headers, index=1)
        logger.info(f"[SHEETS] Headers written to {tab_name}")


# ── Leads tab ──────────────────────────────────────────────────────────────────

def get_all_leads() -> list[dict]:
    """Return all lead rows as dicts. Skips header row."""
    ws = _get_sheet("Leads")
    existing = ws.row_values(1)
    if existing != LEADS_HEADERS:
        if existing and existing[0].lower() == LEADS_HEADERS[0]:
            ws.update("A1", [LEADS_HEADERS])  # overwrite partial/wrong header row
        else:
            ws.insert_row(LEADS_HEADERS, index=1)  # empty sheet or data in row 1
        logger.info("[SHEETS] Headers corrected on Leads tab.")
        return []
    return ws.get_all_records(expected_headers=LEADS_HEADERS)


def get_existing_emails() -> set[str]:
    """Flat set of all emails already in Leads tab (lowercase). Fast dedup check."""
    ws = _get_sheet("Leads")
    all_values = ws.col_values(COL_EMAIL + 1)  # gspread is 1-indexed
    return {e.strip().lower() for e in all_values[1:] if e.strip()}


def get_leads_by_status(status: str) -> list[dict]:
    """Return leads matching a specific status value."""
    all_leads = get_all_leads()
    return [r for r in all_leads if r.get("status", "").strip().lower() == status.lower()]


def append_leads_batch(leads: list[dict]) -> int:
    """
    Batch-write new leads to Leads tab. Single API call.
    Falls back to per-row if batch fails.
    Returns count of rows written.
    """
    if not leads:
        return 0

    ws = _get_sheet("Leads")
    ensure_headers("Leads", LEADS_HEADERS)

    rows = []
    for lead in leads:
        row = [
            lead.get("name", ""),
            lead.get("email", ""),
            lead.get("company", ""),
            lead.get("region", ""),
            lead.get("warmth_score", ""),
            lead.get("status", STATUS_NEW),
            lead.get("last_contacted", ""),
            lead.get("followup_count", 0),
            lead.get("notes", ""),
            lead.get("facebook_url", ""),
            lead.get("linkedin_url", ""),
        ]
        rows.append(row)

    try:
        ws.append_rows(rows, value_input_option="RAW")
        logger.info(f"[SHEETS] Batch wrote {len(rows)} leads.")
        return len(rows)
    except Exception as e:
        logger.warning(f"[SHEETS] Batch write failed ({e}), falling back to per-row.")
        written = 0
        for row in rows:
            try:
                ws.append_row(row, value_input_option="RAW")
                written += 1
            except Exception as row_err:
                logger.error(f"[SHEETS] Per-row write failed: {row_err}")
        return written


def update_lead_status(email: str, status: str, last_contacted: Optional[str] = None,
                        followup_count: Optional[int] = None) -> bool:
    """Find lead by email and update status, last_contacted, followup_count."""
    ws = _get_sheet("Leads")
    emails = ws.col_values(COL_EMAIL + 1)
    for i, e in enumerate(emails[1:], start=2):
        if e.strip().lower() == email.strip().lower():
            ws.update_cell(i, COL_STATUS + 1, status)
            if last_contacted:
                ws.update_cell(i, COL_LAST_CONTACTED + 1, last_contacted)
            if followup_count is not None:
                ws.update_cell(i, COL_FOLLOWUP_COUNT + 1, followup_count)
            return True
    logger.warning(f"[SHEETS] Lead not found for status update: {email}")
    return False


def update_lead_email(email_key: str, new_email: str) -> bool:
    """Update email field for a lead matched by current email."""
    ws = _get_sheet("Leads")
    emails = ws.col_values(COL_EMAIL + 1)
    for i, e in enumerate(emails[1:], start=2):
        if e.strip().lower() == email_key.strip().lower():
            ws.update_cell(i, COL_EMAIL + 1, new_email)
            return True
    return False


def delete_lead_by_email(email: str) -> bool:
    """Delete a lead row matched by email. Used for auto-delete on enrichment failure."""
    ws = _get_sheet("Leads")
    emails = ws.col_values(COL_EMAIL + 1)
    for i, e in enumerate(emails[1:], start=2):
        if e.strip().lower() == email.strip().lower():
            ws.delete_rows(i)
            logger.info(f"[SHEETS] Deleted lead row: {email}")
            return True
    return False


def reset_smtp_failures() -> int:
    """Reset leads stuck at status=failed back to status=new for retry."""
    ws = _get_sheet("Leads")
    rows = ws.get_all_values()
    reset_count = 0
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > COL_STATUS and row[COL_STATUS].strip().lower() == STATUS_FAILED:
            ws.update_cell(i, COL_STATUS + 1, STATUS_NEW)
            reset_count += 1
    if reset_count:
        logger.info(f"[SHEETS] Reset {reset_count} failed leads to new.")
    return reset_count


def get_leads_for_enrichment() -> list[dict]:
    """Return leads with status=new AND empty email."""
    all_leads = get_all_leads()
    return [
        r for r in all_leads
        if r.get("status", "").lower() == STATUS_NEW
        and not r.get("email", "").strip()
    ]


def advance_followup_staging(delay_days: int) -> list[dict]:
    """
    Advance followup_count and status for leads eligible for follow-up.
    Pure date logic. No emails sent.
    Returns list of staged leads.
    """
    from datetime import timedelta
    all_leads = get_all_leads()
    staged = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=delay_days)

    ws = _get_sheet("Leads")
    emails = ws.col_values(COL_EMAIL + 1)

    for lead in all_leads:
        status = lead.get("status", "").lower()
        last_contacted_str = lead.get("last_contacted", "")
        followup_count = int(lead.get("followup_count", 0) or 0)

        if status not in ("outreach_sent", "followup_sent"):
            continue
        if followup_count >= 3:
            continue
        if not last_contacted_str:
            continue

        try:
            last_contacted = datetime.fromisoformat(last_contacted_str).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if last_contacted <= cutoff:
            email = lead.get("email", "").strip().lower()
            new_count = followup_count + 1
            new_status = "followup_sent" if new_count < 3 else "closed"

            for i, e in enumerate(emails[1:], start=2):
                if e.strip().lower() == email:
                    ws.update_cell(i, COL_STATUS + 1, new_status)
                    ws.update_cell(i, COL_FOLLOWUP_COUNT + 1, new_count)
                    break

            lead["followup_count"] = new_count
            lead["status"] = new_status
            staged.append(lead)

    logger.info(f"[SHEETS] Staged {len(staged)} leads for follow-up.")
    return staged


# ── outreach_log tab ───────────────────────────────────────────────────────────

def append_outreach_log(entry: dict, cache: set) -> bool:
    """
    Append a row to outreach_log tab. Idempotent — checks (email, stage_number) cache.
    Cache is a set of (email, stage_number) tuples maintained by caller per run.
    """
    key = (entry.get("lead_email", "").lower(), str(entry.get("stage_number", "")))
    if key in cache:
        logger.debug(f"[SHEETS] Skipping duplicate outreach_log entry: {key}")
        return False

    ws = _get_sheet("outreach_log")
    ensure_headers("outreach_log", OUTREACH_LOG_HEADERS)

    row = [
        entry.get("lead_email", ""),
        entry.get("lead_name", ""),
        entry.get("sequence_type", ""),
        entry.get("stage_number", ""),
        entry.get("email_subject", ""),
        entry.get("sent_date", ""),
        entry.get("status", "sent"),
    ]
    ws.append_row(row, value_input_option="RAW")
    cache.add(key)
    return True


def dedup_outreach_log() -> int:
    """Remove duplicate (lead_email, stage_number) rows from outreach_log. Idempotent."""
    ws = _get_sheet("outreach_log")
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return 0

    seen = set()
    rows_to_delete = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 4:
            continue
        key = (row[OLOG_LEAD_EMAIL].lower(), row[OLOG_STAGE_NUMBER])
        if key in seen:
            rows_to_delete.append(i)
        else:
            seen.add(key)

    for row_idx in reversed(rows_to_delete):
        ws.delete_rows(row_idx)

    if rows_to_delete:
        logger.info(f"[SHEETS] Removed {len(rows_to_delete)} duplicate outreach_log rows.")
    return len(rows_to_delete)


def get_outreach_log_cache() -> set:
    """Load existing (email, stage_number) pairs from outreach_log for dedup."""
    ws = _get_sheet("outreach_log")
    rows = ws.get_all_values()
    cache = set()
    for row in rows[1:]:
        if len(row) >= 4:
            cache.add((row[OLOG_LEAD_EMAIL].lower(), row[OLOG_STAGE_NUMBER]))
    return cache


# ── Outreach Reply Log tab ─────────────────────────────────────────────────────

def append_reply_log(entry: dict) -> None:
    """Append a reply entry to the Outreach Reply Log tab."""
    ws = _get_sheet("Outreach Reply Log")
    ensure_headers("Outreach Reply Log", REPLY_LOG_HEADERS)
    row = [
        entry.get("lead_email", ""),
        entry.get("lead_name", ""),
        entry.get("reply_date", ""),
        entry.get("subject", ""),
        entry.get("snippet", ""),
    ]
    ws.append_row(row, value_input_option="RAW")


def get_all_lead_emails_from_log() -> set[str]:
    """Return all lead emails from outreach_log. Used by reply logger for matching."""
    ws = _get_sheet("outreach_log")
    rows = ws.get_all_values()
    return {row[OLOG_LEAD_EMAIL].lower() for row in rows[1:] if row}


# ── Social outreach ────────────────────────────────────────────────────────────

def get_social_log_rows(platform: str) -> dict[str, dict]:
    """
    Return a map of {email: {"max_touch": int, "last_sent": str}} for all sent rows
    on the given platform. Used to determine eligibility for each touch number.
    Old rows without touch_number default to touch 1.
    """
    ws = _get_sheet("social_log")
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return {}
    result: dict[str, dict] = {}
    for row in rows[1:]:
        if len(row) < 6:
            continue
        if row[2].lower() != platform.lower():
            continue
        if row[5].lower() != "sent":
            continue
        email = row[0].lower()
        touch = int(row[7]) if len(row) >= 8 and row[7].strip().isdigit() else 1
        sent_date = row[4] if len(row) >= 5 else ""
        if email not in result or touch > result[email]["max_touch"]:
            result[email] = {"max_touch": touch, "last_sent": sent_date}
    return result


def get_leads_for_social_outreach(platform: str, touch_number: int) -> list[dict]:
    """
    Return leads eligible for the given touch number on this platform.
    Touch 1: has linkedin_url, not in social_log at all.
    Touch 2/3: received previous touch at least FOLLOWUP_DELAY_DAYS ago, not yet received this touch.
    """
    from config import FOLLOWUP_DELAY_DAYS
    from datetime import datetime, timezone, timedelta

    excluded = {"replied", "closed"}
    log = get_social_log_rows(platform)
    all_leads = [
        r for r in get_all_leads()
        if r.get("linkedin_url", "").strip()
        and r.get("status", "").lower() not in excluded
    ]

    eligible = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=FOLLOWUP_DELAY_DAYS)
    for lead in all_leads:
        email = lead.get("email", "").lower()
        entry = log.get(email)
        if touch_number == 1:
            if entry is None:
                eligible.append(lead)
        else:
            if entry is None:
                continue
            if entry["max_touch"] != touch_number - 1:
                continue
            try:
                last = datetime.fromisoformat(entry["last_sent"].replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            if last <= cutoff:
                eligible.append(lead)
    return eligible


def append_social_log(entry: dict) -> None:
    """Append a row to the social_log tab."""
    ws = _get_sheet("social_log")
    ensure_headers("social_log", SOCIAL_LOG_HEADERS)
    row = [
        entry.get("lead_email", ""),
        entry.get("lead_name", ""),
        entry.get("platform", ""),
        entry.get("profile_url", ""),
        entry.get("sent_date", ""),
        entry.get("status", ""),
        entry.get("notes", ""),
        str(entry.get("touch_number", 1)),
    ]
    ws.append_row(row, value_input_option="RAW")
