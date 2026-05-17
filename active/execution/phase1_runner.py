"""
phase1_runner.py — Phase 1 full orchestrator.
Entry point for Windows Task Scheduler.
Runs: source health check → Vibe ingest → Prospeo fallback →
enrichment → follow-up staging → summary email.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from config import PIPELINE_PAUSED_FLAG, FOLLOWUP_DELAY_DAYS, VIBE_EXPORT_CSV
from pipeline_metrics import read_pipeline_errors, log_pipeline_error
from notify import send_run_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(Path(__file__).parents[2] / "logs" / "phase1_run.log"), mode="a"),
    ],
)
logger = logging.getLogger(__name__)

VIBE_MIN_RESULTS = 10  # fall through to Prospeo if Vibe returns fewer than this


def main() -> int:
    logger.info("=== Phase 1 — Starting ===")

    # Pause flag check
    if os.path.exists(PIPELINE_PAUSED_FLAG):
        logger.info("[PHASE1] PIPELINE_PAUSED flag found. Exiting.")
        return 0

    total_new = 0
    total_dupes = 0
    enrichment_results = {}
    followup_staged = 0

    # Step 1 — Vibe Prospecting ingestion (primary source)
    vibe_stats = {"new_leads": 0, "dupes_skipped": 0, "failed": 0}
    if os.path.exists(VIBE_EXPORT_CSV):
        try:
            from ingest_vibe_export import run_vibe_ingest
            vibe_stats = run_vibe_ingest()
            total_new += vibe_stats["new_leads"]
            total_dupes += vibe_stats["dupes_skipped"]
            logger.info(f"[PHASE1] Vibe: {vibe_stats}")
        except Exception as e:
            log_pipeline_error("vibe_ingest", str(e))
            logger.error(f"[PHASE1] Vibe ingest error: {e}")
    else:
        logger.info(f"[PHASE1] No Vibe export CSV found at {VIBE_EXPORT_CSV}. Skipping Vibe.")

    # Step 2 — Prospeo fallback (if Vibe returned < VIBE_MIN_RESULTS or no CSV)
    if vibe_stats["new_leads"] < VIBE_MIN_RESULTS:
        logger.info(f"[PHASE1] Vibe returned < {VIBE_MIN_RESULTS}. Running Prospeo fallback.")
        try:
            from run_prospeo_discovery import run_prospeo_discovery
            prospeo_stats = run_prospeo_discovery(supplement_target=25)
            total_new += prospeo_stats["new_leads"]
            total_dupes += prospeo_stats["dupes_skipped"]
            logger.info(f"[PHASE1] Prospeo: {prospeo_stats}")
        except Exception as e:
            log_pipeline_error("prospeo_discovery", str(e))
            logger.error(f"[PHASE1] Prospeo discovery error: {e}")

    # Step 3 — Email enrichment (only for leads with missing emails)
    try:
        from enrich_sheets_emails import run_enrichment
        enrichment_results = run_enrichment()
        logger.info(f"[PHASE1] Enrichment: {enrichment_results}")
    except Exception as e:
        log_pipeline_error("enrichment", str(e))
        logger.error(f"[PHASE1] Enrichment error (non-fatal): {e}")

    # Step 4 — Follow-up staging (advance status/count, no emails sent)
    try:
        from sheets_client import advance_followup_staging
        staged = advance_followup_staging(delay_days=FOLLOWUP_DELAY_DAYS)
        followup_staged = len(staged)
        logger.info(f"[PHASE1] Follow-up staged: {followup_staged}")
    except Exception as e:
        log_pipeline_error("followup_staging", str(e))
        logger.error(f"[PHASE1] Follow-up staging error (non-fatal): {e}")

    # Step 5 — Summary email
    errors = [e["message"] for e in read_pipeline_errors(limit=20)]
    try:
        send_run_summary(
            new_leads=total_new,
            dupes_skipped=total_dupes,
            enrichment_results=enrichment_results,
            followup_staged=followup_staged,
            errors=errors,
        )
    except Exception as e:
        logger.error(f"[PHASE1] Failed to send summary email: {e}")

    logger.info(f"=== Phase 1 — Done. New: {total_new}, Dupes: {total_dupes} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
