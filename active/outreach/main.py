"""
main.py — Phase 2 entry point.
Orchestrates: config validation → SMTP reset → pre-sync → initial outreach →
follow-up outreach → cleanup → summary email → post-sync.
Run via GitHub Actions or locally via run_outreach.bat.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "execution"))

from config import PIPELINE_PAUSED_FLAG, FOLLOWUP_DELAY_DAYS
from smtp_client import get_session, reset_session
from outreach_engine import run_initial_outreach, run_followup_outreach
from reply_logger import run_reply_logger
from brevo_reconcile import run_reconcile
from sheets_client import (
    reset_smtp_failures,
    dedup_outreach_log,
    get_outreach_log_cache,
    get_suppression_set,
    advance_followup_staging,
)
from notify import send_outreach_summary, alert_smtp_degraded
from pipeline_metrics import read_pipeline_errors, log_pipeline_error
from template_metrics import record_template_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(Path(__file__).parents[2] / "logs" / "phase2_run.log"), mode="a"),
    ],
)
logger = logging.getLogger(__name__)


def main() -> int:
    """
    Main Phase 2 orchestrator.
    Returns exit code: 0 = success, 1 = SMTP health degraded or critical error.
    """
    logger.info("=== Phase 2 Outreach — Starting ===")

    # Pause flag check
    if os.path.exists(PIPELINE_PAUSED_FLAG):
        logger.info(f"[MAIN] PIPELINE_PAUSED flag found. Exiting.")
        return 0

    # Step 1 — Config validation (hard fail is handled in config.py import)
    logger.info("[MAIN] Config validated.")

    # Step 2 — Poll replies FIRST so any lead who replied is marked status=replied
    # before follow-up (Touch 2+) eligibility is evaluated. Non-fatal: IMAP issues must not
    # block outreach.
    reply_poll_ok = False
    try:
        reply_stats = run_reply_logger()
        logger.info(f"[MAIN] Reply poll: {reply_stats}")
        # Only trust the poll if it completed without IMAP errors. A failed/errored poll
        # means a lead who replied may NOT be marked status=replied yet — so we must not
        # send follow-ups this run (see Step 6). Touch 1 to brand-new leads is unaffected.
        reply_poll_ok = reply_stats.get("errors", 0) == 0
        if not reply_poll_ok:
            logger.error("[MAIN] Reply poll reported errors — follow-ups will be SKIPPED this run.")
    except Exception as e:
        log_pipeline_error("reply_poll", str(e))
        logger.error(f"[MAIN] Reply poll failed — follow-ups will be SKIPPED this run: {e}")

    # Step 3 — Reset leads stuck at failed from previous crashes
    reset_count = reset_smtp_failures()
    logger.info(f"[MAIN] Reset {reset_count} failed leads.")

    # Step 3 — Pre-sync (only if Sheets < Brevo count)
    try:
        reconcile_pre = run_reconcile(pre_sync=True)
        logger.info(f"[MAIN] Pre-sync: {reconcile_pre}")
    except Exception as e:
        log_pipeline_error("brevo_pre_sync", str(e))
        logger.warning(f"[MAIN] Pre-sync failed (non-fatal): {e}")

    # Step 4 — Load outreach log cache (dedup key for all sends this run) + the
    # do-not-contact / suppression list (cross-checked before every send).
    outreach_log_cache = get_outreach_log_cache()
    try:
        suppression = get_suppression_set()
    except Exception as e:
        log_pipeline_error("suppression_load", str(e))
        logger.warning(f"[MAIN] Could not load suppression list (non-fatal, treating as empty): {e}")
        suppression = (set(), set())

    # Step 5 — Initial outreach (Touch 1)
    initial_stats = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0, "cap_hit": False}
    try:
        initial_stats = run_initial_outreach(outreach_log_cache, suppression)
        logger.info(f"[MAIN] Initial outreach: {initial_stats}")
    except Exception as e:
        log_pipeline_error("initial_outreach", str(e))
        logger.error(f"[MAIN] Initial outreach error: {e}")

    # Step 6 — Follow-up outreach (Touch 2..N, capped by MAX_FOLLOWUPS). Gated on BOTH a
    # clear daily cap AND a clean reply poll — never chase Touch 2+ when we couldn't verify
    # who replied (fail-safe, not fail-open: a broken reply poll must not email a lead back
    # after they already answered).
    followup_stats = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0}
    if initial_stats.get("cap_hit"):
        logger.info("[MAIN] Daily cap hit on Touch 1 — skipping follow-ups.")
    elif not reply_poll_ok:
        logger.warning("[MAIN] Skipping follow-ups: reply poll was unreliable this run.")
        log_pipeline_error(
            "followup_skipped",
            "Reply poll failed/errored; follow-ups skipped to avoid emailing leads who replied.",
        )
    else:
        try:
            followup_stats = run_followup_outreach(outreach_log_cache, suppression)
            logger.info(f"[MAIN] Follow-up outreach: {followup_stats}")
        except Exception as e:
            log_pipeline_error("followup_outreach", str(e))
            logger.error(f"[MAIN] Follow-up outreach error: {e}")

    # Step 7 — Cleanup (dedup outreach_log + template metrics)
    session = get_session()
    try:
        dedup_outreach_log()
        record_template_metrics(
            series="all",
            touch_number=0,
            sent=initial_stats["sent"] + followup_stats["sent"],
            failed=initial_stats["failed"] + followup_stats["failed"],
        )
    except Exception as e:
        log_pipeline_error("cleanup", str(e))
        logger.error(f"[MAIN] Cleanup error (non-fatal): {e}")

    # Step 8 — Summary email
    errors = [e["message"] for e in read_pipeline_errors(limit=20)]
    try:
        send_outreach_summary(
            initial_sent=initial_stats["sent"],
            followup_sent=followup_stats["sent"],
            failed=initial_stats["failed"] + followup_stats["failed"],
            cap_hit=session.cap_hit,
            health_degraded=session.health_degraded,
            errors=errors,
        )
    except Exception as e:
        logger.error(f"[MAIN] Failed to send summary email: {e}")

    # Step 9 — Alert if SMTP degraded
    if session.health_degraded:
        try:
            alert_smtp_degraded(session.fail_rate, session.errors)
        except Exception:
            pass

    # Step 10 — Post-sync (always runs)
    try:
        reconcile_post = run_reconcile(pre_sync=False)
        logger.info(f"[MAIN] Post-sync: {reconcile_post}")
    except Exception as e:
        log_pipeline_error("brevo_post_sync", str(e))
        logger.warning(f"[MAIN] Post-sync failed (non-fatal): {e}")

    exit_code = 1 if session.health_degraded else 0
    logger.info(f"=== Phase 2 Outreach — Done (exit {exit_code}) ===")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
