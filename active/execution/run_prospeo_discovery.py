"""
run_prospeo_discovery.py — Multi-region Prospeo discovery batch runner.
Runs per-region discovery, normalizes, deduplicates, writes to Sheets.
Fallback when Vibe returns < 10 results or credits = 0.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from config import MAX_LEADS_PER_RUN, ICP_REGIONS
from pipeline_metrics import (
    should_skip_source, record_source_run, record_run_stats,
    log_pipeline_error,
)
from prospeo_client import discover_with_prospeo
from notify import alert_scrape_zero_results

logger = logging.getLogger(__name__)

SOURCE_NAME = "prospeo_discovery"

# Regions to iterate. Overridden by ICP_REGIONS env var after client onboarding.
# Fallback default keeps pipeline runnable before ICP is configured.
DEFAULT_REGIONS = [r.strip() for r in ICP_REGIONS.split(",") if r.strip()] \
    if ICP_REGIONS and "[CLIENT" not in ICP_REGIONS \
    else ["US", "UK", "AU", "CA", "NZ", "IE"]


def run_prospeo_discovery(supplement_target: int = 25) -> dict:
    """
    Run Prospeo discovery across all configured regions.
    supplement_target: leads to request per region.
    Returns stats dict.
    """
    from ingest_vibe_export import _deduplicate
    from sheets_client import get_existing_emails, append_leads_batch

    stats = {
        "new_leads": 0,
        "dupes_skipped": 0,
        "failed": 0,
        "source": SOURCE_NAME,
        "regions_run": [],
        "regions_zero": [],
    }

    if should_skip_source(SOURCE_NAME):
        logger.info(f"[PROSPEO] Source skipped due to health check.")
        return stats

    all_leads = []
    existing_emails = get_existing_emails()

    for region in DEFAULT_REGIONS:
        if len(all_leads) >= MAX_LEADS_PER_RUN:
            break

        logger.info(f"[PROSPEO] Running discovery for region: {region}")
        leads = discover_with_prospeo(region=region, target=supplement_target)

        if not leads:
            logger.warning(f"[PROSPEO] Zero results for region: {region}")
            alert_scrape_zero_results(f"prospeo_discovery:{region}")
            record_source_run(f"{SOURCE_NAME}:{region}", 0)
            stats["regions_zero"].append(region)
            continue

        stats["regions_run"].append(region)
        record_source_run(f"{SOURCE_NAME}:{region}", len(leads))
        all_leads.extend(leads)

    if not all_leads:
        record_source_run(SOURCE_NAME, 0)
        return stats

    # Cap to max leads
    all_leads = all_leads[:MAX_LEADS_PER_RUN]

    # Dedup against existing + within batch
    clean, dupe_count = _deduplicate(all_leads, existing_emails)
    stats["dupes_skipped"] = dupe_count

    if clean:
        written = append_leads_batch(clean)
        stats["new_leads"] = written
        has_email_pct = sum(1 for l in clean if l.get("email")) / len(clean)
        record_run_stats(SOURCE_NAME, written, has_email_pct, dupe_count)

    record_source_run(SOURCE_NAME, len(clean))
    logger.info(
        f"[PROSPEO] Done. Regions run: {stats['regions_run']}, "
        f"Zero regions: {stats['regions_zero']}, "
        f"Written: {stats['new_leads']}, Dupes: {dupe_count}"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_prospeo_discovery()
    print(result)
