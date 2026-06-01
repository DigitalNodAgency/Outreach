"""
social_engine.py — Social outreach orchestration via PhantomBuster.
Pulls leads with social profile URLs from Sheets, launches the appropriate
PhantomBuster phantom, polls for completion, and logs results.
"""

import logging
import os
from datetime import datetime, timezone

from config import (
    PHANTOMBUSTER_API_KEY,
    PHANTOMBUSTER_FB_PHANTOM_ID,
    PHANTOMBUSTER_FB_SESSION_COOKIE,
    PHANTOMBUSTER_LI_PHANTOM_ID,
    PHANTOMBUSTER_LI_SESSION_COOKIE,
    DRY_RUN,
    TEMPLATES_DIR,
    SENDER_NAME,
)
from phantombuster_client import launch_phantom, wait_for_completion, get_phantom_output
from sheets_client import get_leads_for_social_outreach, append_social_log

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phantom_id_for(platform: str) -> str:
    return PHANTOMBUSTER_FB_PHANTOM_ID if platform == "facebook" else PHANTOMBUSTER_LI_PHANTOM_ID


def _session_cookie_for(platform: str) -> str:
    return PHANTOMBUSTER_FB_SESSION_COOKIE if platform == "facebook" else PHANTOMBUSTER_LI_SESSION_COOKIE


def _url_field_for(platform: str) -> str:
    return "facebook_url" if platform == "facebook" else "linkedin_url"


def _load_social_template(platform: str) -> str:
    path = os.path.join(TEMPLATES_DIR, f"social-{platform}-1.txt")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Social template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _render_social_message(template: str, name: str, company: str) -> str:
    first_name = name.split()[0] if name.strip() else "there"
    return (
        template
        .replace("{{name}}", first_name)
        .replace("{{company}}", company)
        .replace("{{sender_name}}", SENDER_NAME)
    )


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

    try:
        template = _load_social_template(platform)
    except FileNotFoundError as e:
        logger.error(f"[SOCIAL] {e}")
        return stats

    input_data = []
    for lead in leads:
        name = lead.get("name", "").strip()
        company = lead.get("company", "").strip()
        input_data.append({
            "profileUrl": lead.get(url_field, ""),
            "name": name,
            "company": company,
            "message": _render_social_message(template, name, company),
        })

    if DRY_RUN:
        logger.info(f"[SOCIAL] DRY RUN — would launch {platform} phantom with {len(input_data)} leads:")
        for item in input_data:
            logger.info(f"  profileUrl={item['profileUrl']} | message={item['message'][:80]}...")
        stats["launched"] = False
        return stats

    session_cookie = _session_cookie_for(platform)
    container_id = launch_phantom(PHANTOMBUSTER_API_KEY, phantom_id, input_data, session_cookie)
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
