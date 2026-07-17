"""
mark_lead_replied.py — Operator CLI: stop a lead's outreach sequence.

Marks a lead as replied (or closed) by email, and appends a matching row to the
'Outreach Reply Log' tab so the stop survives even a later status corruption
(the reconcile sweep in reply_logger re-flips any active lead listed there on
every run).

Usage:
    python scripts/mark_lead_replied.py lead@example.com                # dry run
    python scripts/mark_lead_replied.py lead@example.com --apply        # write
    python scripts/mark_lead_replied.py lead@example.com --status closed --apply
    python scripts/mark_lead_replied.py lead@example.com --note "replied via LinkedIn" --apply

Dry run (default) only prints the lead's current state. Exit codes: 0 = ok,
1 = lead not found / write failed.

NOTE for non-CLI use: pasting the lead's email into column A of the
'Outreach Reply Log' tab does the same job on the next scheduled run.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "active" / "outreach"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mark a lead replied/closed and stop their sequence.")
    parser.add_argument("email", help="Lead email (case-insensitive, as in the Leads tab)")
    parser.add_argument("--status", choices=["replied", "closed"], default="replied",
                        help="Terminal status to set (default: replied)")
    parser.add_argument("--note", default="", help="Optional note stored in the Reply Log snippet column")
    parser.add_argument("--apply", action="store_true", help="Write changes (omit for dry run)")
    args = parser.parse_args()

    from sheets_client import get_all_leads, update_lead_status, append_reply_log

    email = args.email.strip().lower()
    lead = next((r for r in get_all_leads() if (r.get("email") or "").strip().lower() == email), None)
    if lead is None:
        logger.error(f"No Leads row matches '{email}'. Nothing changed.")
        return 1

    logger.info(
        f"Lead found: {lead.get('name', '')} | {email} | status={lead.get('status', '')} | "
        f"last_contacted={lead.get('last_contacted', '')} | followup_count={lead.get('followup_count', '')}"
    )

    if not args.apply:
        logger.info(f"DRY RUN — would set status={args.status} and append a Reply Log row. "
                    f"Re-run with --apply to write.")
        return 0

    if not update_lead_status(email, args.status):
        logger.error(f"Status write failed for '{email}'.")
        return 1
    append_reply_log({
        "lead_email": email,
        "lead_name": lead.get("name", ""),
        "reply_date": datetime.now(timezone.utc).isoformat(),
        "subject": "manual",
        "snippet": args.note or f"manually marked {args.status} via mark_lead_replied.py",
    })
    logger.info(f"Done: status={args.status} set and Reply Log row appended for {email}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
