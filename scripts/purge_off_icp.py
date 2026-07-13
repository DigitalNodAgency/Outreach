"""
purge_off_icp.py — One-shot: remove off-ICP leads that reached the Leads tab before the
strict-ICP post-dedup gate existed (e.g. the off-ICP consultant lead that slipped in).

What it does:
  1. Backs up the ENTIRE Leads tab to active/leads/leads_backup_officp_<ts>.json (always,
     both dry run and --apply).
  2. Screens every Leads row with the SAME credit-free deny screen discovery now uses
     (config.ICP_DENY_KEYWORDS via run_vibe_api_discovery._icp_deny_reason), matched on
     the row's COMPANY NAME. A row is a purge CANDIDATE only when it ALSO passes the hard
     safety gate below.
     (Geo is deliberately NOT auto-screened here: the Leads 'region' cell usually holds a
     US/CA state name, not a country, so a geo screen on it would false-drop valid leads.
     Out-of-region leads are prevented at discovery and can be targeted with --also-email.)
  3. Dry run (default): prints the exact candidate rows and stops. Nothing is deleted.
  4. --apply: re-prints the candidate rows, then requires you to type DELETE to confirm,
     then deletes ONLY those rows (by row index, descending so indices stay valid).

HARD SAFETY GATE — a row is eligible ONLY if ALL hold (never touches a contacted lead):
  * status is exactly "new" (case-insensitive)
  * last_contacted is blank
  * followup_count is 0 / blank
  * the email is NOT present anywhere in the outreach_log tab (no send history)

LIMITATION: the Leads schema stores no job-title column, so the automatic deny screen can
only match on COMPANY NAME (and geo). An off-ICP lead whose company name looks in-ICP but
whose *title* is off-ICP (a "Consultant"/"Fractional CMO" at a plainly-named shop) will
NOT be auto-detected — target those explicitly with --also-email a@b.com,c@d.com. Every
--also-email row still passes the same hard safety gate before it can be deleted.

Destructive on the client's live sheet — run the dry run first, then --apply.
Delete this script after use (one-shot, like seed_test_lead.py).

Usage:
  python scripts/purge_off_icp.py                          # dry run (backup + report)
  python scripts/purge_off_icp.py --also-email a@b.com     # add explicit target(s)
  python scripts/purge_off_icp.py --apply                  # backup, confirm, delete
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT / "active" / "outreach"))
sys.path.insert(0, str(_ROOT / "active" / "execution"))

from config import (  # noqa: E402
    COL_NAME, COL_EMAIL, COL_COMPANY, COL_REGION, COL_STATUS,
    COL_LAST_CONTACTED, COL_FOLLOWUP_COUNT, STATUS_NEW,
)
from sheets_client import (  # noqa: E402
    _get_sheet, _with_backoff, _invalidate_leads_email_col,
    get_leads_raw_values,
)
# Reuse the EXACT deny screen discovery uses — single source of truth, no re-derived literals.
from run_vibe_api_discovery import (  # noqa: E402
    _deny_keywords, _icp_deny_reason,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("purge_off_icp")


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if len(row) > idx else ""


def _outreach_log_emails() -> set[str]:
    """Lowercase set of every email that appears in the outreach_log tab (send history)."""
    try:
        ws = _get_sheet("outreach_log")
        rows = _with_backoff(ws.get_all_values)
    except Exception as e:
        logger.warning(f"Could not read outreach_log ({e}); treating history as unknown → refusing all.")
        return None  # sentinel: caller must abort rather than risk deleting a contacted lead
    emails = set()
    for r in rows[1:]:
        e = _cell(r, 0).lower()
        if e:
            emails.add(e)
    return emails


def _is_history_free(row: list[str], log_emails: set[str]) -> bool:
    """The hard safety gate: status==new, never contacted, and absent from outreach_log."""
    if _cell(row, COL_STATUS).lower() != STATUS_NEW:
        return False
    if _cell(row, COL_LAST_CONTACTED):
        return False
    fc = _cell(row, COL_FOLLOWUP_COUNT)
    if fc and fc not in ("0", "0.0"):
        return False
    email = _cell(row, COL_EMAIL).lower()
    if email and email in log_emails:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge off-ICP status=new leads from the Leads tab.")
    ap.add_argument("--apply", action="store_true", help="Actually delete (default: dry run).")
    ap.add_argument("--also-email", default="",
                    help="Comma-separated emails to also target (for title-based off-ICP the "
                         "company deny screen can't see). Still subject to the safety gate.")
    args = ap.parse_args()

    deny = _deny_keywords()
    also = {e.strip().lower() for e in args.also_email.split(",") if e.strip()}
    logger.info(f"Deny keywords (company-name screen): {deny}")
    if also:
        logger.info(f"Explicit --also-email targets: {sorted(also)}")

    rows = get_leads_raw_values()
    if not rows:
        logger.info("Leads tab is empty. Nothing to do.")
        return 0

    # 1) Always back up the full Leads tab first.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = _ROOT / "active" / "leads" / f"leads_backup_officp_{ts}.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    logger.info(f"Backed up {len(rows) - 1} lead row(s) → {backup_path}")

    log_emails = _outreach_log_emails()
    if log_emails is None:
        logger.error("Aborting: outreach_log history could not be read (safety gate cannot be verified).")
        return 2

    # 2) Screen every data row (row 1 is the header; sheet rows are 1-indexed).
    candidates = []  # (row_index_1based, name, email, company, region, reason)
    for i, row in enumerate(rows[1:], start=2):
        company = _cell(row, COL_COMPANY)
        region = _cell(row, COL_REGION)
        email = _cell(row, COL_EMAIL).lower()
        name = _cell(row, COL_NAME)

        reason = ""
        # Sheet has no job-title column → screen the company name only (title is "").
        kw = _icp_deny_reason({"company_name": company, "job_title": ""}, deny)
        if kw:
            reason = f"deny_keyword:{kw}"
        elif email and email in also:
            reason = "explicit_also_email"
        if not reason:
            continue

        if not _is_history_free(row, log_emails):
            logger.info(f"SKIP (has history / not new): row {i} {name!r} <{email}> {company!r}")
            continue
        candidates.append((i, name, email, company, region, reason))

    if not candidates:
        logger.info("No off-ICP status=new leads matched. Nothing to purge.")
        return 0

    print("\n=== Off-ICP purge candidates (status=new, no outreach history) ===")
    for i, name, email, company, region, reason in candidates:
        print(f"  row {i:>4}  {name:<28} <{email or '(no email)'}>  company={company!r}  region={region!r}  [{reason}]")
    print(f"Total: {len(candidates)} row(s)\n")

    if not args.apply:
        print("DRY RUN — nothing deleted. Review the list above, then re-run with --apply.")
        return 0

    # 3) Confirm, then delete by descending row index so earlier indices stay valid.
    resp = input(f"Type DELETE to permanently remove these {len(candidates)} row(s): ").strip()
    if resp != "DELETE":
        print("Not confirmed. Aborted — nothing deleted.")
        return 1

    ws = _get_sheet("Leads")
    deleted = 0
    for i, name, email, company, region, reason in sorted(candidates, key=lambda c: c[0], reverse=True):
        try:
            _with_backoff(ws.delete_rows, i)
            deleted += 1
            logger.info(f"Deleted row {i}: {name!r} <{email}> {company!r} [{reason}]")
        except Exception as e:
            logger.error(f"Failed to delete row {i} ({name!r}): {e}")
    _invalidate_leads_email_col()
    logger.info(f"Done. Deleted {deleted}/{len(candidates)} row(s). Backup: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
