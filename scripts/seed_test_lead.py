"""
seed_test_lead.py — One-shot: reset the live lead DB to a single Rizan QA lead.

Context (v13 niche pivot): the pipeline moved from HVAC → US marketing/social agencies.
Before pointing outreach at real agency leads, we wipe the old HVAC data and leave one
test lead (Rizan) so the next Phase 2 run sends the new 4-touch sequence to Rizan's inbox.

What it does:
  1. Backs up every tab it will clear to active/leads/sheet_backup_<ts>.json (always).
  2. With --apply: clears Leads + outreach_log + Removed Emails + Outreach Reply Log +
     social_log, re-writes their header rows, and seeds one Rizan lead (status=new).
  Without --apply it is a DRY RUN: writes the backup and prints what it would clear.

Destructive on the client's live sheet — run the dry run first, then `--apply`.
Delete this script after use (one-shot, like reset_sequence_corrupt.py).

Usage:
  python scripts/seed_test_lead.py            # dry run (backup + report only)
  python scripts/seed_test_lead.py --apply    # perform the wipe + seed
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "active" / "outreach"))

from config import (
    LEADS_HEADERS, OUTREACH_LOG_HEADERS, REMOVED_EMAILS_HEADERS,
    REPLY_LOG_HEADERS, SOCIAL_LOG_HEADERS,
)
from sheets_client import (
    _get_sheet, _with_backoff, ensure_headers,
    append_leads_batch, get_all_leads,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# (tab name, header constant) — every tab cleared by this reset.
TABS = [
    ("Leads", LEADS_HEADERS),
    ("outreach_log", OUTREACH_LOG_HEADERS),
    ("Removed Emails", REMOVED_EMAILS_HEADERS),
    ("Outreach Reply Log", REPLY_LOG_HEADERS),
    ("social_log", SOCIAL_LOG_HEADERS),
]

RIZAN_LEAD = {
    "name": "Rizan",
    "email": "himurakenshin096@gmail.com",
    "company": "Rizan Test Agency",
    "region": "USA",
    "warmth_score": 10,
    "status": "new",
    "last_contacted": "",
    "followup_count": 0,
    "notes": "test lead — makeover QA (v13)",
    "facebook_url": "",
    "linkedin_url": "",
}


def _backup() -> Path:
    """Dump every tab's current values to a timestamped JSON file. Returns the path."""
    snapshot = {}
    for tab, _ in TABS:
        ws = _get_sheet(tab)
        rows = _with_backoff(ws.get_all_values)
        snapshot[tab] = rows
        logger.info(f"  backup: {tab} — {max(len(rows) - 1, 0)} data row(s)")

    backup_dir = _ROOT / "active" / "leads"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    path = backup_dir / f"sheet_backup_{ts}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    logger.info(f"Backup written: {path}")
    return path


def main() -> None:
    apply = "--apply" in sys.argv

    logger.info("=== seed_test_lead — %s ===", "APPLY" if apply else "DRY RUN")

    # 1) Always back up before touching anything.
    _backup()

    # 2) Report current state.
    for tab, _ in TABS:
        ws = _get_sheet(tab)
        n = max(len(_with_backoff(ws.get_all_values)) - 1, 0)
        logger.info(f"  current: {tab} = {n} data row(s)")

    if not apply:
        logger.info("DRY RUN — no changes made. Re-run with --apply to wipe + seed.")
        return

    # 3) Wipe every tab (values only), then restore its header row.
    for tab, headers in TABS:
        ws = _get_sheet(tab)
        _with_backoff(ws.clear)
        ensure_headers(tab, headers)
        logger.info(f"  cleared + header restored: {tab}")

    # 4) Seed the single Rizan test lead (append_leads_batch ensures the Leads header too).
    written = append_leads_batch([RIZAN_LEAD])
    logger.info(f"Seeded {written} test lead (Rizan, status=new).")

    # 5) Confirm.
    after = get_all_leads()
    logger.info(f"Leads tab now holds {len(after)} row(s): {[l.get('email') for l in after]}")
    logger.info("Done. Next Phase 2 run will send Touch 1 to %s.", RIZAN_LEAD["email"])


if __name__ == "__main__":
    main()
