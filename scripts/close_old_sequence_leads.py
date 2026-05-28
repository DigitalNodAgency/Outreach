"""
close_old_sequence_leads.py — One-shot fix.
Closes out leads that received Touch 1 with the old template (May 25-26)
so they don't receive a Touch 3 that continues the wrong sequence.
Identifies them by last_contacted date (2026-05-25 or 2026-05-26).
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "active" / "outreach"))

from config import (
    GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID,
    COL_EMAIL, COL_STATUS, COL_LAST_CONTACTED,
    STATUS_CLOSED,
)
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

OLD_SEQUENCE_DATES = ("2026-05-25", "2026-05-26")


def _get_client() -> gspread.Client:
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def main() -> None:
    client = _get_client()
    ss = client.open_by_key(SPREADSHEET_ID)
    ws = ss.worksheet("Leads")

    all_values = ws.get_all_values()
    closed_count = 0

    for i, row in enumerate(all_values[1:], start=2):
        if len(row) <= max(COL_STATUS, COL_LAST_CONTACTED):
            continue

        last_contacted = row[COL_LAST_CONTACTED].strip()
        status = row[COL_STATUS].strip().lower()

        if status == STATUS_CLOSED:
            continue

        if any(last_contacted.startswith(d) for d in OLD_SEQUENCE_DATES):
            email = row[COL_EMAIL].strip()
            ws.update_cell(i, COL_STATUS + 1, STATUS_CLOSED)
            logger.info(f"  Closed: {email} (last_contacted: {last_contacted})")
            closed_count += 1

    logger.info(f"Done. Closed {closed_count} old-sequence lead(s).")


if __name__ == "__main__":
    main()
