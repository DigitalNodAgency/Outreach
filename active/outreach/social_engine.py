"""
social_engine.py — Social outreach orchestration via PhantomBuster (LinkedIn).
Pulls leads eligible for the given touch number, launches the phantom,
polls for completion, and logs results.
"""

import logging
import os
from datetime import datetime, timezone

from config import (
    PHANTOMBUSTER_API_KEY,
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


def _load_template(touch_number: int) -> str:
    path = os.path.join(TEMPLATES_DIR, f"social-linkedin-{touch_number}.txt")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Social template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _normalize_linkedin_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def _render_message(template: str, name: str, sender_name: str) -> str:
    first_name = name.split()[0] if name.strip() else "there"
    return (
        template
        .replace("{{name}}", first_name)
        .replace("{{sender_name}}", sender_name)
    )


def run_social_outreach(touch_number: int) -> dict:
    """
    Run LinkedIn social outreach for one touch number (1, 2, or 3).
    Returns stats dict: touch_number, targeted, launched, succeeded, failed.
    """
    stats = {
        "touch_number": touch_number,
        "targeted": 0,
        "launched": False,
        "succeeded": False,
        "failed": 0,
    }

    if not PHANTOMBUSTER_LI_PHANTOM_ID:
        logger.warning(f"[SOCIAL] PHANTOMBUSTER_LI_PHANTOM_ID not set. Skipping touch {touch_number}.")
        return stats

    leads = get_leads_for_social_outreach("linkedin", touch_number)

    if not leads:
        logger.info(f"[SOCIAL] Touch {touch_number} — no eligible leads.")
        return stats

    stats["targeted"] = len(leads)
    logger.info(f"[SOCIAL] Touch {touch_number} — {len(leads)} leads targeted.")

    try:
        template = _load_template(touch_number)
    except FileNotFoundError as e:
        logger.error(f"[SOCIAL] {e}")
        return stats

    input_data = []
    for lead in leads:
        name = lead.get("name", "").strip()
        input_data.append({
            "profileUrl": _normalize_linkedin_url(lead.get("linkedin_url", "")),
            "name": name,
            "message": _render_message(template, name, SENDER_NAME),
        })

    if DRY_RUN:
        logger.info(f"[SOCIAL] DRY RUN — would launch touch {touch_number} with {len(input_data)} leads:")
        for item in input_data:
            logger.info(f"  profileUrl={item['profileUrl']} | message={item['message'][:80]}...")
        return stats

    container_id = launch_phantom(
        PHANTOMBUSTER_API_KEY,
        PHANTOMBUSTER_LI_PHANTOM_ID,
        input_data,
        PHANTOMBUSTER_LI_SESSION_COOKIE,
    )
    if not container_id:
        logger.error(f"[SOCIAL] Failed to launch phantom for touch {touch_number}.")
        _log_all_failed(leads, touch_number, "launch_failed")
        stats["failed"] = len(leads)
        return stats

    stats["launched"] = True
    finished = wait_for_completion(PHANTOMBUSTER_API_KEY, container_id)

    if not finished:
        _log_all_failed(leads, touch_number, "phantom_timeout_or_error")
        stats["failed"] = len(leads)
        return stats

    stats["succeeded"] = True
    results = get_phantom_output(PHANTOMBUSTER_API_KEY, container_id)
    sent_date = _now_iso()

    result_map = {r.get("profileUrl", ""): r for r in results if isinstance(r, dict)}

    for lead in leads:
        profile_url = lead.get("linkedin_url", "")
        result = result_map.get(profile_url, {})
        status = "sent" if result.get("messageSent") else "failed"
        if status == "failed":
            stats["failed"] += 1

        append_social_log({
            "lead_email": lead.get("email", ""),
            "lead_name": lead.get("name", ""),
            "platform": "linkedin",
            "profile_url": profile_url,
            "sent_date": sent_date,
            "status": status,
            "notes": result.get("error", ""),
            "touch_number": touch_number,
        })

    logger.info(
        f"[SOCIAL] Touch {touch_number} done. Targeted: {stats['targeted']}, "
        f"Failed: {stats['failed']}"
    )
    return stats


def _log_all_failed(leads: list[dict], touch_number: int, reason: str) -> None:
    sent_date = _now_iso()
    for lead in leads:
        append_social_log({
            "lead_email": lead.get("email", ""),
            "lead_name": lead.get("name", ""),
            "platform": "linkedin",
            "profile_url": lead.get("linkedin_url", ""),
            "sent_date": sent_date,
            "status": "failed",
            "notes": reason,
            "touch_number": touch_number,
        })
