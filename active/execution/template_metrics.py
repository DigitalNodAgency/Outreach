"""
template_metrics.py — Per-series send/fail metrics logger.
Appends a row to template_metrics.tsv after each Phase 2 run.
"""

import logging
import os
from datetime import datetime, timezone

from config import TEMPLATE_METRICS_TSV

logger = logging.getLogger(__name__)


def record_template_metrics(
    series: str,
    touch_number: int,
    sent: int,
    failed: int,
) -> None:
    """
    Append a metrics row for a given template series and touch number.
    Called once per series per run from main.py cleanup block.
    """
    os.makedirs(os.path.dirname(TEMPLATE_METRICS_TSV), exist_ok=True)
    write_header = not os.path.exists(TEMPLATE_METRICS_TSV)

    with open(TEMPLATE_METRICS_TSV, "a") as f:
        if write_header:
            f.write("timestamp\tseries\ttouch_number\tsent\tfailed\tsuccess_rate\n")
        now = datetime.now(timezone.utc).isoformat()
        total = sent + failed
        rate = f"{sent / total:.2f}" if total > 0 else "0.00"
        f.write(f"{now}\t{series}\t{touch_number}\t{sent}\t{failed}\t{rate}\n")
        logger.info(f"[METRICS] Template metrics recorded: {series} touch {touch_number} — sent {sent}, failed {failed}")
