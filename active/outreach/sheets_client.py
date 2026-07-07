"""
sheets_client.py — Google Sheets read/write client.
All Sheets I/O goes through this module. Batch-first, quota-safe.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

from config import (
    GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID,
    LEADS_HEADERS, OUTREACH_LOG_HEADERS, REPLY_LOG_HEADERS, SOCIAL_LOG_HEADERS,
    REMOVED_EMAILS_HEADERS, DISCOVERY_STATE_HEADERS,
    COL_EMAIL, COL_STATUS, COL_LAST_CONTACTED, COL_FOLLOWUP_COUNT,
    COL_NAME, COL_COMPANY, COL_REGION, COL_FACEBOOK_URL, COL_LINKEDIN_URL,
    OLOG_LEAD_EMAIL, OLOG_STAGE_NUMBER,
    STATUS_NEW, STATUS_FAILED, STATUS_OUTREACH_SENT, STATUS_FOLLOWUP_SENT,
    MAX_FOLLOWUPS,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Per-process caches (quota-safe) ──────────────────────────────────────────────
# A run is a single short-lived process, so caching the authorized client, the
# spreadsheet handle, and worksheet handles is safe and removes the per-call
# re-auth + open_by_key reads that previously blew the Sheets read quota.
_client: Optional[gspread.Client] = None
_spreadsheet = None
_ws_cache: dict[str, gspread.Worksheet] = {}
_headers_ensured: set[str] = set()
# Cached email column for the Leads tab — row positions are stable within a run
# (Touch 1 only updates cells, never inserts/deletes rows). Invalidated on append/delete.
_leads_email_col: Optional[list[str]] = None

# Sheets default read quota is 60 requests/min/user; retry transient quota/5xx errors.
_RETRYABLE_STATUS = (429, 500, 503)
_BACKOFF_BASE = 1.0
_MAX_RETRIES = 3


def _with_backoff(fn, *args, **kwargs):
    """Call a gspread API method, retrying 429/5xx with exponential backoff (1s base, 3 retries)."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(f"[SHEETS] {status} quota/transient error — backoff {wait}s (retry {attempt + 1}/{_MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise


def _invalidate_leads_email_col() -> None:
    """Drop the cached Leads email column after a row insert/delete shifts positions."""
    global _leads_email_col
    _leads_email_col = None


def _get_leads_email_col() -> list[str]:
    """Cached Leads email column (1-indexed col), read once per run and reused.
    Safe because Touch 1 only updates cells; appends/deletes invalidate the cache."""
    global _leads_email_col
    if _leads_email_col is None:
        ws = _get_sheet("Leads")
        _leads_email_col = _with_backoff(ws.col_values, COL_EMAIL + 1)
    return _leads_email_col


def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        logger.info(f"[SHEETS] Auth as: {creds_dict.get('client_email', '???')}")
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        try:
            _spreadsheet = _with_backoff(_get_client().open_by_key, SPREADSHEET_ID)
        except PermissionError as e:
            raise PermissionError(
                f"403: service account has no access to sheet id={SPREADSHEET_ID!r}. "
                f"Share the sheet with the client_email logged above as Editor."
            ) from e
    return _spreadsheet


def _get_sheet(tab_name: str) -> gspread.Worksheet:
    if tab_name not in _ws_cache:
        spreadsheet = _get_spreadsheet()
        try:
            _ws_cache[tab_name] = _with_backoff(spreadsheet.worksheet, tab_name)
        except gspread.WorksheetNotFound:
            _ws_cache[tab_name] = _with_backoff(
                spreadsheet.add_worksheet, title=tab_name, rows=1000, cols=20
            )
    return _ws_cache[tab_name]


def ensure_headers(tab_name: str, headers: list[str]) -> None:
    """Write headers to row 1 if the sheet is empty. Reads at most once per tab per run."""
    if tab_name in _headers_ensured:
        return
    ws = _get_sheet(tab_name)
    existing = _with_backoff(ws.row_values, 1)
    if not existing or existing[0] != headers[0]:
        _with_backoff(ws.insert_row, headers, index=1)
        _invalidate_leads_email_col()
        logger.info(f"[SHEETS] Headers written to {tab_name}")
    _headers_ensured.add(tab_name)


# ── Leads tab ──────────────────────────────────────────────────────────────────

def get_all_leads() -> list[dict]:
    """Return all lead rows as dicts. Skips header row."""
    ws = _get_sheet("Leads")
    existing = _with_backoff(ws.row_values, 1)
    if existing != LEADS_HEADERS:
        if existing and existing[0].lower() == LEADS_HEADERS[0]:
            _with_backoff(ws.update, "A1", [LEADS_HEADERS])  # overwrite partial/wrong header row
        else:
            _with_backoff(ws.insert_row, LEADS_HEADERS, index=1)  # empty sheet or data in row 1
            _invalidate_leads_email_col()
        logger.info("[SHEETS] Headers corrected on Leads tab.")
        return []
    return _with_backoff(ws.get_all_records, expected_headers=LEADS_HEADERS)


def get_existing_emails() -> set[str]:
    """Flat set of all emails already in Leads tab (lowercase). Fast dedup check."""
    all_values = _get_leads_email_col()
    return {e.strip().lower() for e in all_values[1:] if e.strip()}


def get_existing_name_company_pairs() -> set[tuple[str, str]]:
    """(name_lower, company_lower) pairs for leads with no email. Fallback dedup key."""
    ws = _get_sheet("Leads")
    all_rows = _with_backoff(ws.get_all_values)
    pairs = set()
    for row in all_rows[1:]:
        email = row[COL_EMAIL].strip() if len(row) > COL_EMAIL else ""
        name = row[COL_NAME].strip().lower() if len(row) > COL_NAME else ""
        company = row[COL_COMPANY].strip().lower() if len(row) > COL_COMPANY else ""
        if not email and name:
            pairs.add((name, company))
    return pairs


def get_all_name_company_pairs() -> set[tuple[str, str]]:
    """(name_lower, company_lower) pairs for EVERY lead row with a name and company,
    regardless of email. Used by discovery to skip already-scraped prospects at
    fetch time (before enrichment), when no email is available to match on."""
    ws = _get_sheet("Leads")
    all_rows = _with_backoff(ws.get_all_values)
    pairs = set()
    for row in all_rows[1:]:
        name = row[COL_NAME].strip().lower() if len(row) > COL_NAME else ""
        company = row[COL_COMPANY].strip().lower() if len(row) > COL_COMPANY else ""
        if name and company:
            pairs.add((name, company))
    return pairs


def get_leads_by_status(status: str) -> list[dict]:
    """Return leads matching a specific status value."""
    all_leads = get_all_leads()
    return [r for r in all_leads if r.get("status", "").strip().lower() == status.lower()]


def get_suppression_set() -> tuple[set[str], set[str]]:
    """Read the do-not-contact list from a 'Suppression' tab.

    Column A = email (exact lowercase match); Column B = domain (optional, matches the
    part after @ for whole-company suppression). A header row (first cell 'email'/'address')
    is skipped if present. Returns (emails, domains). If the tab does not exist, returns
    empty sets — suppression is an optional safety layer, never a hard dependency, so a
    missing tab must not block a run."""
    spreadsheet = _get_spreadsheet()
    try:
        ws = _with_backoff(spreadsheet.worksheet, "Suppression")
    except gspread.WorksheetNotFound:
        logger.info("[SHEETS] No 'Suppression' tab — do-not-contact cross-check skipped.")
        return set(), set()
    rows = _with_backoff(ws.get_all_values)
    if not rows:
        return set(), set()
    start = 1 if rows[0] and rows[0][0].strip().lower() in ("email", "emails", "address") else 0
    emails: set[str] = set()
    domains: set[str] = set()
    for row in rows[start:]:
        if row and len(row) > 0 and "@" in row[0]:
            emails.add(row[0].strip().lower())
        if len(row) > 1 and row[1].strip():
            domains.add(row[1].strip().lower().lstrip("@"))
    logger.info(f"[SHEETS] Suppression list loaded: {len(emails)} emails, {len(domains)} domains.")
    return emails, domains


SUPPRESSION_HEADERS = ["email", "domain", "reason", "date"]


def append_suppression(entries: list[dict]) -> int:
    """Append do-not-contact addresses to the 'Suppression' tab (auto-created if absent).

    entries: list of {"email": str, "reason": str, "blocked_at": str}. Emails already on
    the list (via get_suppression_set) are skipped, so this is idempotent — safe to run
    every Phase 2 run. Column A = email (the exact-match key the send gate reads); column B
    (domain) is left blank on purpose so we suppress the individual address, not the whole
    company. Columns C/D (reason, date) are audit-only — get_suppression_set ignores them.
    Written in a single append_rows batch. Returns the number of NEW rows appended."""
    cleaned = []
    seen_this_call: set[str] = set()
    for e in entries:
        email = (e.get("email") or "").strip().lower()
        if not email or "@" not in email or email in seen_this_call:
            continue
        seen_this_call.add(email)
        cleaned.append(e)
    if not cleaned:
        return 0

    ensure_headers("Suppression", SUPPRESSION_HEADERS)
    existing_emails, _ = get_suppression_set()
    new_rows = [
        [(e.get("email") or "").strip().lower(), "",
         (e.get("reason") or "").strip(), (e.get("blocked_at") or "").strip()]
        for e in cleaned
        if (e.get("email") or "").strip().lower() not in existing_emails
    ]
    if not new_rows:
        logger.info("[SHEETS] Suppression sync: no new addresses to append.")
        return 0

    ws = _get_sheet("Suppression")
    _with_backoff(ws.append_rows, new_rows, value_input_option="RAW")
    logger.info(f"[SHEETS] Suppression sync: appended {len(new_rows)} new address(es).")
    return len(new_rows)


def mark_leads_suppressed(email_to_status: dict[str, str]) -> int:
    """Flip Leads rows for suppressed addresses to a terminal status in one batched write.

    email_to_status maps a lowercase email → terminal status (unsubscribed / bounced).
    Only rows currently in an ACTIVE state (new / outreach_sent / followup_sent) are
    touched — a lead that already replied/closed is never overwritten. Mirrors the single
    batch_update pattern in reset_smtp_failures. Returns the number of rows updated."""
    if not email_to_status:
        return 0
    active = {STATUS_NEW, STATUS_OUTREACH_SENT, STATUS_FOLLOWUP_SENT}
    lookup = {k.strip().lower(): v for k, v in email_to_status.items()}
    ws = _get_sheet("Leads")
    rows = _with_backoff(ws.get_all_values)
    updates = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) <= COL_STATUS:
            continue
        email = row[COL_EMAIL].strip().lower() if len(row) > COL_EMAIL else ""
        if email in lookup and row[COL_STATUS].strip().lower() in active:
            updates.append({"range": gspread.utils.rowcol_to_a1(i, COL_STATUS + 1),
                            "values": [[lookup[email]]]})
    if updates:
        _with_backoff(ws.batch_update, updates, value_input_option="RAW")
        logger.info(f"[SHEETS] Marked {len(updates)} lead(s) as suppressed (unsubscribed/bounced).")
    return len(updates)


def get_leads_raw_values() -> list[list[str]]:
    """All Leads rows as raw value lists (cached client + 429 backoff).
    For callers that write back by row index (e.g. social URL enrichment)."""
    ws = _get_sheet("Leads")
    return _with_backoff(ws.get_all_values)


# ── Discovery State tab (durable pagination cursor) ────────────────────────────

def get_discovery_cursor() -> dict:
    """Return the per-ICP discovery pagination cursor from the 'Discovery State' tab
    as {filter_key: {"offset": int, "total_results": int}}.

    Stored in the Sheet (not a local file) so it survives GitHub Actions' ephemeral
    filesystem. Returns {} if the tab is absent/empty so discovery degrades safely to
    offset 0 rather than failing."""
    spreadsheet = _get_spreadsheet()
    try:
        ws = _with_backoff(spreadsheet.worksheet, "Discovery State")
    except gspread.WorksheetNotFound:
        return {}
    rows = _with_backoff(ws.get_all_values)
    if not rows:
        return {}
    start = 1 if rows[0] and rows[0][0].strip().lower() == "filter_key" else 0
    cursor: dict[str, dict] = {}
    for row in rows[start:]:
        if not row or not row[0].strip():
            continue
        key = row[0].strip()
        def _int(idx: int) -> int:
            try:
                return int(row[idx]) if len(row) > idx and str(row[idx]).strip() else 0
            except (ValueError, TypeError):
                return 0
        cursor[key] = {"offset": _int(1), "total_results": _int(2)}
    return cursor


def set_discovery_cursor(filter_key: str, offset: int, total_results: int) -> None:
    """Upsert the pagination offset for one ICP filter set in the 'Discovery State'
    tab (one row per filter_key; updated in place or appended). Best-effort: a Sheets
    error is logged, not raised, so persistence never aborts a discovery run."""
    try:
        ws = _get_sheet("Discovery State")
        ensure_headers("Discovery State", DISCOVERY_STATE_HEADERS)
        new_values = [
            filter_key, str(offset), str(total_results),
            datetime.now(timezone.utc).isoformat(),
        ]
        rows = _with_backoff(ws.get_all_values)
        target_row = None
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == filter_key:
                target_row = i
                break
        if target_row:
            _with_backoff(ws.update, f"A{target_row}:D{target_row}", [new_values],
                          value_input_option="RAW")
        else:
            _with_backoff(ws.append_row, new_values, value_input_option="RAW")
        logger.info(f"[SHEETS] Discovery cursor saved: offset={offset} total={total_results}")
    except Exception as e:
        logger.warning(f"[SHEETS] Could not persist discovery cursor: {e}")


def update_lead_cell(row_idx: int, col_zero_based: int, value: str) -> None:
    """Update a single Leads cell by 1-based row and 0-based column.
    Routes through the cached client with 429 backoff. Invalidates the email
    cache if the email column itself is written."""
    ws = _get_sheet("Leads")
    _with_backoff(ws.update_cell, row_idx, col_zero_based + 1, value)
    if col_zero_based == COL_EMAIL:
        _invalidate_leads_email_col()


def batch_update_cells(cells: list[tuple]) -> int:
    """Apply many single-cell writes to the Leads tab in ONE batched API call.
    `cells` = list of (row_1based, col_0based, value). Quota-safe alternative to
    a loop of update_cell. Invalidates the email cache if any write targets the
    email column. Returns the number of cells written."""
    if not cells:
        return 0
    ws = _get_sheet("Leads")
    updates = [
        {"range": gspread.utils.rowcol_to_a1(r, c + 1), "values": [[v]]}
        for r, c, v in cells
    ]
    _with_backoff(ws.batch_update, updates, value_input_option="RAW")
    if any(c == COL_EMAIL for _, c, _ in cells):
        _invalidate_leads_email_col()
    logger.info(f"[SHEETS] Batch updated {len(cells)} cells on Leads.")
    return len(cells)


def append_removed_emails(entries: list[dict]) -> int:
    """Append BillionVerify-rejected addresses to the 'Removed Emails' audit tab.
    Single batched write. Returns rows written."""
    if not entries:
        return 0
    ws = _get_sheet("Removed Emails")
    ensure_headers("Removed Emails", REMOVED_EMAILS_HEADERS)
    rows = [[
        e.get("email", ""), e.get("name", ""), e.get("company", ""),
        e.get("bv_status", ""), e.get("bv_reason", ""), e.get("removed_date", ""),
    ] for e in entries]
    _with_backoff(ws.append_rows, rows, value_input_option="RAW")
    logger.info(f"[SHEETS] Logged {len(rows)} removed emails to audit tab.")
    return len(rows)


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
        _with_backoff(ws.append_rows, rows, value_input_option="RAW")
        _invalidate_leads_email_col()
        logger.info(f"[SHEETS] Batch wrote {len(rows)} leads.")
        return len(rows)
    except Exception as e:
        logger.warning(f"[SHEETS] Batch write failed ({e}), falling back to per-row.")
        written = 0
        for row in rows:
            try:
                _with_backoff(ws.append_row, row, value_input_option="RAW")
                written += 1
            except Exception as row_err:
                logger.error(f"[SHEETS] Per-row write failed: {row_err}")
        _invalidate_leads_email_col()
        return written


def update_lead_status(email: str, status: str, last_contacted: Optional[str] = None,
                        followup_count: Optional[int] = None) -> bool:
    """Find lead by email and update status, last_contacted, followup_count.
    Uses the cached email column (no per-lead read) and a single batch_update write."""
    ws = _get_sheet("Leads")
    emails = _get_leads_email_col()
    for i, e in enumerate(emails[1:], start=2):
        if e.strip().lower() == email.strip().lower():
            updates = [{"range": gspread.utils.rowcol_to_a1(i, COL_STATUS + 1), "values": [[status]]}]
            if last_contacted:
                updates.append({"range": gspread.utils.rowcol_to_a1(i, COL_LAST_CONTACTED + 1),
                                "values": [[last_contacted]]})
            if followup_count is not None:
                updates.append({"range": gspread.utils.rowcol_to_a1(i, COL_FOLLOWUP_COUNT + 1),
                                "values": [[followup_count]]})
            _with_backoff(ws.batch_update, updates, value_input_option="RAW")
            return True
    logger.warning(f"[SHEETS] Lead not found for status update: {email}")
    return False


def update_lead_email(email_key: str, new_email: str) -> bool:
    """Update email field for a lead matched by current email."""
    ws = _get_sheet("Leads")
    emails = _get_leads_email_col()
    for i, e in enumerate(emails[1:], start=2):
        if e.strip().lower() == email_key.strip().lower():
            _with_backoff(ws.update_cell, i, COL_EMAIL + 1, new_email)
            _invalidate_leads_email_col()
            return True
    return False


def delete_lead_by_email(email: str) -> bool:
    """Delete a lead row matched by email. Used for auto-delete on enrichment failure."""
    ws = _get_sheet("Leads")
    emails = _get_leads_email_col()
    for i, e in enumerate(emails[1:], start=2):
        if e.strip().lower() == email.strip().lower():
            _with_backoff(ws.delete_rows, i)
            _invalidate_leads_email_col()
            logger.info(f"[SHEETS] Deleted lead row: {email}")
            return True
    return False


def reset_smtp_failures() -> int:
    """Reset leads stuck at status=failed back to status=new for retry. Single batched write."""
    ws = _get_sheet("Leads")
    rows = _with_backoff(ws.get_all_values)
    updates = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > COL_STATUS and row[COL_STATUS].strip().lower() == STATUS_FAILED:
            updates.append({"range": gspread.utils.rowcol_to_a1(i, COL_STATUS + 1),
                            "values": [[STATUS_NEW]]})
    if updates:
        _with_backoff(ws.batch_update, updates, value_input_option="RAW")
        logger.info(f"[SHEETS] Reset {len(updates)} failed leads to new.")
    return len(updates)


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
    emails = _get_leads_email_col()

    for lead in all_leads:
        status = lead.get("status", "").lower()
        last_contacted_str = lead.get("last_contacted", "")
        followup_count = int(lead.get("followup_count", 0) or 0)

        if status not in ("outreach_sent", "followup_sent"):
            continue
        if followup_count >= MAX_FOLLOWUPS:
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
            new_status = "followup_sent" if new_count < MAX_FOLLOWUPS else "closed"

            for i, e in enumerate(emails[1:], start=2):
                if e.strip().lower() == email:
                    _with_backoff(ws.batch_update, [
                        {"range": gspread.utils.rowcol_to_a1(i, COL_STATUS + 1), "values": [[new_status]]},
                        {"range": gspread.utils.rowcol_to_a1(i, COL_FOLLOWUP_COUNT + 1), "values": [[new_count]]},
                    ], value_input_option="RAW")
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
    _with_backoff(ws.append_row, row, value_input_option="RAW")
    cache.add(key)
    return True


def dedup_outreach_log() -> int:
    """Remove duplicate (lead_email, stage_number) rows from outreach_log. Idempotent."""
    ws = _get_sheet("outreach_log")
    rows = _with_backoff(ws.get_all_values)
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
        _with_backoff(ws.delete_rows, row_idx)

    if rows_to_delete:
        logger.info(f"[SHEETS] Removed {len(rows_to_delete)} duplicate outreach_log rows.")
    return len(rows_to_delete)


def get_outreach_log_cache() -> set:
    """Load existing (email, stage_number) pairs from outreach_log for dedup."""
    ws = _get_sheet("outreach_log")
    rows = _with_backoff(ws.get_all_values)
    cache = set()
    for row in rows[1:]:
        if len(row) >= 4:
            cache.add((row[OLOG_LEAD_EMAIL].lower(), row[OLOG_STAGE_NUMBER]))
    return cache


def get_stage_subjects(stage_number: int = 1) -> dict[str, str]:
    """Return {lead_email_lower: email_subject} for a given outreach stage from
    outreach_log. Lets follow-ups thread off the EXACT Touch-1 subject each lead
    received (the variation engine varies Touch-1 subjects per lead). Last write wins."""
    from config import OLOG_EMAIL_SUBJECT
    ws = _get_sheet("outreach_log")
    rows = _with_backoff(ws.get_all_values)
    out: dict[str, str] = {}
    target = str(stage_number)
    for row in rows[1:]:
        if len(row) <= OLOG_EMAIL_SUBJECT:
            continue
        if str(row[OLOG_STAGE_NUMBER]).strip() != target:
            continue
        email = row[OLOG_LEAD_EMAIL].strip().lower()
        subj = row[OLOG_EMAIL_SUBJECT].strip()
        if email and subj:
            out[email] = subj
    return out


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
    _with_backoff(ws.append_row, row, value_input_option="RAW")


def get_all_lead_emails_from_log() -> set[str]:
    """Return all lead emails from outreach_log. Used by reply logger for matching."""
    ws = _get_sheet("outreach_log")
    rows = _with_backoff(ws.get_all_values)
    return {row[OLOG_LEAD_EMAIL].lower() for row in rows[1:] if row}


# ── Social outreach ────────────────────────────────────────────────────────────

def get_social_log_rows(platform: str) -> dict[str, dict]:
    """
    Return a map of {email: {"max_touch": int, "last_sent": str}} for all sent rows
    on the given platform. Used to determine eligibility for each touch number.
    Old rows without touch_number default to touch 1.
    """
    ws = _get_sheet("social_log")
    rows = _with_backoff(ws.get_all_values)
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
    _with_backoff(ws.append_row, row, value_input_option="RAW")
