"""
reset_sequence_corrupt.py — One-shot fix for out-of-sequence sends.

Root cause: Brevo credential bug (SMTP_FROM misconfiguration) caused earlier
pipeline runs to advance lead statuses to outreach_sent / followup_count=1
even though emails weren't properly delivered. When Phase 2 ran, those leads
were treated as having received Touch 1 and got Touch 2 instead.

This script:
  1. Finds leads that have a Touch 2/3 entry in outreach_log but NO Touch 1
  2. Resets those leads to status=new, followup_count=0, last_contacted=''
  3. Removes their bad Touch 2/3 outreach_log entries so future sends aren't blocked
"""

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "active" / "outreach"))

from config import (
    GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID,
    OLOG_LEAD_EMAIL, OLOG_STAGE_NUMBER,
    COL_EMAIL, COL_STATUS, COL_LAST_CONTACTED, COL_FOLLOWUP_COUNT,
    STATUS_NEW,
)
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_client() -> gspread.Client:
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def main() -> None:
    client = _get_client()
    ss = client.open_by_key(SPREADSHEET_ID)

    # Step 1 — Analyse outreach_log
    log_ws = ss.worksheet("outreach_log")
    log_rows = log_ws.get_all_values()

    has_touch1: set[str] = set()
    has_touch2plus: set[str] = set()

    for row in log_rows[1:]:
        if len(row) < 4:
            continue
        email = row[OLOG_LEAD_EMAIL].strip().lower()
        try:
            stage = int(row[OLOG_STAGE_NUMBER])
        except (ValueError, IndexError):
            continue
        if stage == 1:
            has_touch1.add(email)
        elif stage >= 2:
            has_touch2plus.add(email)

    affected = has_touch2plus - has_touch1
    logger.info(f"Affected leads (Touch 2+ without Touch 1): {len(affected)}")
    for e in sorted(affected):
        logger.info(f"  → {e}")

    if not affected:
        logger.info("No affected leads found. Nothing to reset.")
        return

    # Step 2 — Reset affected leads in Leads sheet
    leads_ws = ss.worksheet("Leads")
    lead_emails = leads_ws.col_values(COL_EMAIL + 1)
    reset_count = 0

    for i, email in enumerate(lead_emails[1:], start=2):
        if email.strip().lower() in affected:
            leads_ws.update_cell(i, COL_STATUS + 1, STATUS_NEW)
            leads_ws.update_cell(i, COL_FOLLOWUP_COUNT + 1, 0)
            leads_ws.update_cell(i, COL_LAST_CONTACTED + 1, "")
            logger.info(f"  Reset row {i}: {email}")
            reset_count += 1

    logger.info(f"Reset {reset_count} lead(s) to status=new.")

    # Step 3 — Remove bad outreach_log entries for affected leads
    # Reload after lead updates (indices haven't shifted yet)
    log_rows = log_ws.get_all_values()
    rows_to_delete = []

    for i, row in enumerate(log_rows[1:], start=2):
        if len(row) < 4:
            continue
        email = row[OLOG_LEAD_EMAIL].strip().lower()
        try:
            stage = int(row[OLOG_STAGE_NUMBER])
        except (ValueError, IndexError):
            continue
        if email in affected and stage >= 2:
            rows_to_delete.append(i)

    for row_idx in reversed(rows_to_delete):
        log_ws.delete_rows(row_idx)
        logger.info(f"  Deleted outreach_log row {row_idx}")

    logger.info(f"Deleted {len(rows_to_delete)} bad outreach_log row(s).")
    logger.info("Done. Next Phase 2 run will send Touch 1 correctly to all affected leads.")


if __name__ == "__main__":
    main()
