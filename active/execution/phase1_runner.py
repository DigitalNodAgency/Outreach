"""
phase1_runner.py — Phase 1 full orchestrator.
Entry point for GitHub Actions (phase1-discovery.yml) and Windows Task Scheduler.
Runs: Vibe discovery (Explorium, with built-in email enrichment) →
social URL enrichment → follow-up staging → summary email.

VIBE-ONLY: Prospeo/Apify/Serper discovery + email enrichment are intentionally
disabled. Vibe (Explorium) is the sole discovery + email source. Leads Vibe
cannot find an email for are KEPT (not deleted) for PhantomBuster social outreach;
Phase 2 safely skips them for email touches.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from config import PIPELINE_PAUSED_FLAG, VIBE_EXPORT_CSV, MAX_LEADS_PER_RUN
from pipeline_metrics import read_pipeline_errors, log_pipeline_error
from notify import send_run_summary

_LOG_DIR = Path(__file__).parents[2] / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_LOG_DIR / "phase1_run.log"), mode="a"),
    ],
)
logger = logging.getLogger(__name__)


def main() -> int:
    logger.info("=== Phase 1 — Starting ===")

    # Pause flag check
    if os.path.exists(PIPELINE_PAUSED_FLAG):
        logger.info("[PHASE1] PIPELINE_PAUSED flag found. Exiting.")
        return 0

    total_new = 0
    total_dupes = 0
    enrichment_results = {}
    verify_results = {}
    social_results = {}

    # Step 1 — Vibe Prospecting ingestion (primary source)
    vibe_stats = {"new_leads": 0, "dupes_skipped": 0, "failed": 0}
    vibe_api_key = os.getenv("VIBE_PROSPECTING_API_KEY", "").strip()

    if vibe_api_key:
        # GHA path: call Vibe MCP HTTP API directly
        try:
            from run_vibe_api_discovery import run_vibe_api_discovery
            vibe_stats = run_vibe_api_discovery(target=MAX_LEADS_PER_RUN)
            total_new += vibe_stats["new_leads"]
            total_dupes += vibe_stats["dupes_skipped"]
            logger.info(f"[PHASE1] Vibe API: {vibe_stats}")
        except Exception as e:
            log_pipeline_error("vibe_api", f"{type(e).__name__}: {e}")
            logger.error(f"[PHASE1] Vibe API error: {type(e).__name__}: {e}")
    elif os.path.exists(VIBE_EXPORT_CSV):
        # Legacy path: CSV exported from a Claude Code MCP session
        try:
            from ingest_vibe_export import run_vibe_ingest
            vibe_stats = run_vibe_ingest()
            total_new += vibe_stats["new_leads"]
            total_dupes += vibe_stats["dupes_skipped"]
            logger.info(f"[PHASE1] Vibe CSV: {vibe_stats}")
        except Exception as e:
            log_pipeline_error("vibe_ingest", str(e))
            logger.error(f"[PHASE1] Vibe ingest error: {e}")
    else:
        logger.info("[PHASE1] No VIBE_PROSPECTING_API_KEY and no CSV. Skipping Vibe.")

    # Step 2 + 3 — DISABLED (Vibe-only mode).
    # Prospeo discovery fallback and Prospeo/Apify/Serper email enrichment are
    # intentionally removed. Vibe (Explorium) discovers AND enriches emails in
    # run_vibe_api_discovery._enrich_email. Leads Vibe cannot email are retained
    # for PhantomBuster social outreach instead of being auto-deleted.
    enrichment_results = {"mode": "vibe-only", "note": "Prospeo/Apify/Serper email enrichment disabled"}

    # Step 1.6 — Email verification (BillionVerify) on Sheets emails.
    # KEEP valid/catchall/role; bad emails are blanked + audited but the lead row
    # is RETAINED for PhantomBuster social outreach. Skipped if BV_API_KEY unset.
    try:
        from verify_emails_step import run_email_verification
        verify_results = run_email_verification()
        logger.info(f"[PHASE1] Email verification: {verify_results}")
    except Exception as e:
        log_pipeline_error("email_verification", f"{type(e).__name__}: {e}")
        logger.error(f"[PHASE1] Email verification error (non-fatal): {type(e).__name__}: {e}")

    # Step 3.5 — Social URL enrichment via Serper (fills LinkedIn col K + Facebook col J)
    try:
        from enrich_linkedin_step import run_social_url_enrichment
        social_results = run_social_url_enrichment()
        logger.info(f"[PHASE1] Social enrichment: {social_results}")
    except Exception as e:
        log_pipeline_error("social_enrichment", f"{type(e).__name__}: {e}")
        logger.error(f"[PHASE1] Social enrichment error (non-fatal): {type(e).__name__}: {e}")

    # Step 4 — REMOVED. Follow-up sequencing is owned entirely by the Phase 2
    # outreach engine (run_followup_outreach), which advances followup_count only
    # AFTER it actually sends each touch. The old advance_followup_staging() call
    # here bumped followup_count WITHOUT sending, so Phase 2 then sent touch
    # (count + 1) and the staged touch's email was silently skipped. Worse, on any
    # Phase 2 run that sent no follow-ups (daily cap hit, reply-poll fail-safe),
    # Phase 1 kept bumping the count every Mon/Thu until the lead was CLOSED having
    # received only 1-2 of its touches. Phase 1 no longer touches follow-up state.
    # See CLAUDE.md §5.

    # Step 5 — Summary email
    errors = [e["message"] for e in read_pipeline_errors(limit=20)]
    try:
        send_run_summary(
            new_leads=total_new,
            dupes_skipped=total_dupes,
            icp_rejected=vibe_stats.get("icp_rejected", 0),
            enrichment_results=enrichment_results,
            verify_results=verify_results,
            social_results=social_results,
            errors=errors,
        )
    except Exception as e:
        logger.error(f"[PHASE1] Failed to send summary email: {e}")

    logger.info(f"=== Phase 1 — Done. New: {total_new}, Dupes: {total_dupes} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
