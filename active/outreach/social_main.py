"""
social_main.py — Entry point for the social-outreach GitHub Actions workflow.
Runs LinkedIn Touch 1 (connection request) via PhantomBuster.
Touches 2 and 3 are handled natively by PhantomBuster's built-in follow-up system.
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
from notify import send_social_summary


def main() -> int:
    logger.info(f"=== Social Outreach — Starting (dry_run={DRY_RUN}) ===")

    if not PHANTOMBUSTER_API_KEY:
        logger.error("[SOCIAL] PHANTOMBUSTER_API_KEY not set. Exiting.")
        return 1

    all_stats = []
    stats = run_social_outreach(1)
    all_stats.append(stats)

    for s in all_stats:
        logger.info(
            f"  Touch {s['touch_number']}: targeted={s['targeted']}, failed={s['failed']}, "
            f"launched={s.get('launched')}, succeeded={s.get('succeeded')}"
        )

    try:
        send_social_summary(all_stats, dry_run=DRY_RUN)
    except Exception as e:
        logger.warning(f"[SOCIAL] Failed to send summary email: {e}")

    logger.info("=== Social Outreach — Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
