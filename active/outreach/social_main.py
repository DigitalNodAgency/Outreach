"""
social_main.py — Entry point for the social-outreach GitHub Actions workflow.
Runs LinkedIn touches 1, 2, and 3 in sequence via PhantomBuster.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "execution"))

_LOG_DIR = Path(__file__).parents[2] / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_LOG_DIR / "social_run.log"), mode="a"),
    ],
)
logger = logging.getLogger(__name__)

from config import DRY_RUN, PHANTOMBUSTER_API_KEY
from social_engine import run_social_outreach
from notify import alert_pipeline_error


def main() -> int:
    logger.info(f"=== Social Outreach — Starting (dry_run={DRY_RUN}) ===")

    if not PHANTOMBUSTER_API_KEY:
        logger.error("[SOCIAL] PHANTOMBUSTER_API_KEY not set. Exiting.")
        return 1

    all_stats = []
    for touch in [1, 2, 3]:
        stats = run_social_outreach(touch)
        all_stats.append(stats)

    total_targeted = sum(s["targeted"] for s in all_stats)
    total_failed = sum(s["failed"] for s in all_stats)

    summary_lines = [f"LinkedIn social outreach complete (dry_run={DRY_RUN})."]
    for s in all_stats:
        summary_lines.append(
            f"  Touch {s['touch_number']}: targeted={s['targeted']}, failed={s['failed']}, "
            f"launched={s.get('launched')}, succeeded={s.get('succeeded')}"
        )
    summary = "\n".join(summary_lines)
    logger.info(summary)

    try:
        alert_pipeline_error(
            stage="social-outreach (linkedin)",
            message=summary,
        )
    except Exception as e:
        logger.warning(f"[SOCIAL] Failed to send summary email: {e}")

    logger.info("=== Social Outreach — Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
