"""
verify_emails_step.py — Phase 1 email-verification step (BillionVerify).

Two entry points:

  run_email_verification()  — HEADLESS Sheets path, called by phase1_runner.
      Verifies every Leads-tab email not already BillionVerify-flagged, then:
        KEEP   (valid/catchall/role)            → flag in notes, email retained.
        REMOVE (invalid/risky/disposable/unknown):
                 • status=new/unset  → email blanked (so Phase 2 skips the email
                   touch) but the LEAD ROW IS RETAINED for PhantomBuster social
                   outreach (Vibe-only retention rule, CLAUDE.md v11).
                 • already contacted → email kept, just flagged (do not disrupt
                   in-flight sequences).
                 Either way the bad address is logged to the 'Removed Emails' tab.

  verify_csv(path)          — CSV path. Writes verified_/removed_ files per the
      VERIFY_EMAILS.md spec. Used for manual / local runs and pre-import cleaning.

BV_API_KEY missing → step is skipped gracefully (returns {"skipped": True}),
mirroring the Serper social-enrichment soft-dependency. Never logs the key.

CLI:
  python active/execution/verify_emails_step.py            # Sheets path
  python active/execution/verify_emails_step.py leads.csv  # CSV path
"""

import csv as _csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from billionverify_client import (
    BillionVerifyClient, BillionVerifyAuthError,
    KEEP_STATUSES, REMOVE_STATUSES,
)

logger = logging.getLogger(__name__)


def _bv_key() -> str:
    return os.getenv("BV_API_KEY", "").strip()


# ── Sheets path (pipeline) ───────────────────────────────────────────────────────

def run_email_verification() -> dict:
    """Verify un-flagged Leads-tab emails via BillionVerify. See module docstring."""
    api_key = _bv_key()
    if not api_key:
        logger.info("[VERIFY] BV_API_KEY not set — skipping email verification.")
        return {"skipped": True, "reason": "no BV_API_KEY"}

    from config import COL_EMAIL, COL_NAME, COL_COMPANY, COL_STATUS, COL_NOTES
    from sheets_client import (
        get_leads_raw_values, batch_update_cells, append_removed_emails,
    )

    rows = get_leads_raw_values()
    if len(rows) <= 1:
        return {"skipped": False, "verified": 0, "kept": 0, "removed": 0, "note": "no leads"}

    # Collect emails not yet verified (no "bv:" marker in notes). Idempotent + credit-safe.
    pending: dict[int, str] = {}
    for i, row in enumerate(rows[1:], start=2):
        email = row[COL_EMAIL].strip() if len(row) > COL_EMAIL else ""
        notes = row[COL_NOTES] if len(row) > COL_NOTES else ""
        if email and "bv:" not in notes.lower():
            pending[i] = email

    if not pending:
        return {"skipped": False, "verified": 0, "kept": 0, "removed": 0, "note": "all already verified"}

    client = BillionVerifyClient(api_key)
    balance = client.get_credits()
    logger.info(f"[VERIFY] Credits available: {balance if balance is not None else 'unknown'}")
    if balance == 0:
        from notify import alert_token_exhausted
        alert_token_exhausted("BillionVerify", "Credit balance is 0; verification skipped this run.")
        return {"skipped": True, "reason": "0 credits"}

    results = client.verify_bulk(list(pending.values()))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_status: dict[str, int] = {}
    cells: list[tuple] = []
    removed_audit: list[dict] = []
    kept = removed = 0

    for row_idx, email in pending.items():
        res = results.get(email.strip().lower()) or {}
        status = res.get("status", "unknown")
        score = res.get("score", "")
        by_status[status] = by_status.get(status, 0) + 1

        raw = rows[row_idx - 1]
        existing_notes = raw[COL_NOTES] if len(raw) > COL_NOTES else ""
        flag = f"bv:{status}"
        base_notes = f"{existing_notes} | {flag}" if existing_notes else flag
        lead_status = (raw[COL_STATUS].strip().lower() if len(raw) > COL_STATUS else "")
        is_uncontacted = lead_status in ("", "new")

        if status in REMOVE_STATUSES:
            removed += 1
            if is_uncontacted:
                cells.append((row_idx, COL_EMAIL, ""))            # blank bad email
                cells.append((row_idx, COL_NOTES, f"{base_notes} (email removed)"))
            else:
                cells.append((row_idx, COL_NOTES, f"{base_notes} (bad email — already contacted)"))
            removed_audit.append({
                "email": email,
                "name": raw[COL_NAME] if len(raw) > COL_NAME else "",
                "company": raw[COL_COMPANY] if len(raw) > COL_COMPANY else "",
                "bv_status": status,
                "bv_reason": str(score),
                "removed_date": now,
            })
        else:
            kept += 1
            cells.append((row_idx, COL_NOTES, base_notes))

    batch_update_cells(cells)
    append_removed_emails(removed_audit)

    stats = {
        "skipped": False,
        "verified": len(pending),
        "kept": kept,
        "removed": removed,
        "by_status": by_status,
        "credits_before": balance,
    }
    logger.info(f"[VERIFY] {stats}")
    return stats


# ── CSV path (manual / pre-import cleaning) ──────────────────────────────────────

_EMAIL_COL_CANDIDATES = ("email", "Email", "Email Address", "email_address")


def _detect_email_column(fieldnames: list[str]) -> str | None:
    for cand in _EMAIL_COL_CANDIDATES:
        if cand in fieldnames:
            return cand
    lower = {fn.lower(): fn for fn in fieldnames}
    for cand in ("email", "email address", "email_address"):
        if cand in lower:
            return lower[cand]
    return None


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def verify_csv(csv_path: str) -> dict:
    """Verify a leads CSV. Writes active/leads/verified_<name> + removed_<name>.
    Preserves all original columns and adds bv_status (+ bv_reason on removed)."""
    api_key = _bv_key()
    if not api_key:
        raise BillionVerifyAuthError("BV_API_KEY is missing. Add it to your .env file.")

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(csv_path)

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    email_col = _detect_email_column(fieldnames)
    if email_col is None:
        raise ValueError(f"No email column found. Available columns: {fieldnames}")
    if not rows:
        raise ValueError("CSV file is empty or has no data rows.")

    client = BillionVerifyClient(api_key)
    balance = client.get_credits()
    logger.info(f"[VERIFY] Credits available: {balance if balance is not None else 'unknown'}")

    results = client.verify_bulk([r.get(email_col, "") for r in rows])

    out_dir = Path(__file__).resolve().parents[2] / "active" / "leads"
    out_dir.mkdir(parents=True, exist_ok=True)
    verified_path = out_dir / f"verified_{path.name}"
    removed_path = out_dir / f"removed_{path.name}"

    kept_rows, removed_rows = [], []
    by_status: dict[str, int] = {}
    for r in rows:
        email = (r.get(email_col) or "").strip().lower()
        res = results.get(email, {"status": "unknown", "score": ""})
        status = res["status"]
        by_status[status] = by_status.get(status, 0) + 1
        out = dict(r)
        out["bv_status"] = status
        if status in KEEP_STATUSES:
            kept_rows.append(out)
        else:
            out["bv_reason"] = str(res.get("score", ""))
            removed_rows.append(out)

    _write_csv(verified_path, fieldnames + ["bv_status"], kept_rows)
    _write_csv(removed_path, fieldnames + ["bv_status", "bv_reason"], removed_rows)

    stats = {
        "input": path.name,
        "total": len(rows),
        "kept": len(kept_rows),
        "removed": len(removed_rows),
        "by_status": by_status,
        "verified_file": str(verified_path),
        "removed_file": str(removed_path),
        "credits_before": balance,
    }
    logger.info(f"[VERIFY] {stats}")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".csv"):
        print(verify_csv(sys.argv[1]))
    else:
        print(run_email_verification())
