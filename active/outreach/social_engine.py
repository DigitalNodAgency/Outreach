"""
social_engine.py — Social outreach orchestration via PhantomBuster.
Pulls leads with social profile URLs from Sheets, launches the appropriate
PhantomBuster phantom, polls for completion, and logs results.
"""

import logging
from datetime import datetime, timezone

from config import (
    PHANTOMBUSTER_API_KEY,
    PHANTOMBUSTER_FB_PHANTOM_ID,
    PHANTOMBUSTER_LI_PHANTOM_ID,
    DRY_RUN,
)
from phantombuster_client import launch_phantom, wait_for_completion, get_phantom_output
from sheets_client import get_leads_for_social_outreach, append_social_log

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phantom_id_for(platform: str) -> str:
    return PHANTOMBUSTER_FB_PHANTOM_ID if platform == "facebook" else PHANTOMBUSTER_LI_PHANTOM_ID


def _url_field_for(platform: str) -> str:
    return "facebook_url" if platform == "facebook" else "linkedin_url"


def run_social_outreach(platform: str) -> dict:
    """
    Run social outreach for one platform (facebook or linkedin).
    Returns stats dict: targeted, launched, succeeded, failed.
    """
    stats = {"platform": platform, "targeted": 0, "launched": False, "succeeded": False, "failed": 0}

    phantom_id = _phantom_id_for(platform)
    if not phantom_id:
        logger.warning(f"[SOCIAL] No phantom ID configured for {platform}. Skipping.")
        return stats

    url_field = _url_field_for(platform)
    leads = get_leads_for_social_outreach(platform)

    if not leads:
        logger.info(f"[SOCIAL] No leads with {url_field} found for {platform}.")
        return stats

    stats["targeted"] = len(leads)
    logger.info(f"[SOCIAL] {platform} — {len(leads)} leads targeted.")

    input_data = [
        {
            "profileUrl": lead.get(url_field, ""),
            "name": lead.get("name", ""),
            "company": lead.get("company", ""),
        }
        for lead in leads
    ]

    if DRY_RUN:
        logger.info(f"[SOCIAL] DRY RUN — would launch {platform} phantom with {len(input_data)} leads:")
        for item in input_data:
            logger.info(f"  {item}")
        stats["launched"] = False
        return stats

    container_id = launch_phantom(PHANTOMBUSTER_API_KEY, phantom_id, input_data)
    if not container_id:
        logger.error(f"[SOCIAL] Failed to launch {platform} phantom.")
        _log_all_failed(leads, platform, url_field, "launch_failed")
        stats["failed"] = len(leads)
        return stats

    stats["launched"] = True
    finished = wait_for_completion(PHANTOMBUSTER_API_KEY, container_id)

    if not finished:
        _log_all_failed(leads, platform, url_field, "phantom_timeout_or_error")
        stats["failed"] = len(leads)
        return stats

    stats["succeeded"] = True
    results = get_phantom_output(PHANTOMBUSTER_API_KEY, container_id)
    sent_date = _now_iso()

    # Map results back to leads — PhantomBuster returns one result per input row
    result_map = {r.get("profileUrl", ""): r for r in results if isinstance(r, dict)}

    for lead in leads:
        profile_url = lead.get(url_field, "")
        result = result_map.get(profile_url, {})
        status = "sent" if result.get("messageSent") else "failed"
        if status == "failed":
            stats["failed"] += 1

        append_social_log({
            "lead_email": lead.get("email", ""),
            "lead_name": lead.get("name", ""),
            "platform": platform,
            "profile_url": profile_url,
            "sent_date": sent_date,
            "status": status,
            "notes": result.get("error", ""),
        })

    logger.info(
        f"[SOCIAL] {platform} done. Targeted: {stats['targeted']}, "
        f"Failed: {stats['failed']}"
    )
    return stats


def _log_all_failed(leads: list[dict], platform: str, url_field: str, reason: str) -> None:
    sent_date = _now_iso()
    for lead in leads:
        append_social_log({
            "lead_email": lead.get("email", ""),
            "lead_name": lead.get("name", ""),
            "platform": platform,
            "profile_url": lead.get(url_field, ""),
            "sent_date": sent_date,
            "status": "failed",
            "notes": reason,
        })
