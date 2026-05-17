"""
prospeo_client.py — Prospeo API client.
Handles /search-person (discovery), /enrich-person (single enrichment),
and /bulk-enrich-person (batch enrichment).
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from pipeline_metrics import log_pipeline_error

logger = logging.getLogger(__name__)

PROSPEO_API_KEY = os.getenv("PROSPEO_API_KEY", "")
BASE_URL = "https://api.prospeo.io"
REQUEST_TIMEOUT = 30
RETRY_DELAYS = [1, 3, 7]


def _headers() -> dict:
    return {
        "X-KEY": PROSPEO_API_KEY,
        "Content-Type": "application/json",
    }


def _post(endpoint: str, payload: dict, retries: int = 3) -> Optional[dict]:
    url = f"{BASE_URL}{endpoint}"
    for attempt, delay in enumerate(RETRY_DELAYS[:retries], start=1):
        try:
            resp = requests.post(url, json=payload, headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                logger.warning(f"[PROSPEO] Rate limit hit. Retrying in {delay}s.")
                time.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                log_pipeline_error("prospeo", f"Auth error {resp.status_code}: {resp.text}")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            logger.warning(f"[PROSPEO] Timeout on {endpoint}, attempt {attempt}.")
            time.sleep(delay)
        except Exception as e:
            logger.error(f"[PROSPEO] Request error on {endpoint}: {e}")
            time.sleep(delay)
    return None


def _source_tag() -> str:
    return f"prospeo_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def _is_person_name(name: str) -> bool:
    """Basic check: 2-5 title-case words, letters only."""
    parts = name.strip().split()
    if not (2 <= len(parts) <= 5):
        return False
    return all(p.istitle() and p.isalpha() for p in parts)


def discover_with_prospeo(region: str, target: int = 25) -> list[dict]:
    """
    Discover prospects via Prospeo /search-person.
    Applies ICP filters from environment config.
    Returns normalized lead dicts with source tag.
    """
    from config import ICP_PERSONA, ICP_COMPANY_SIZE, ICP_INDUSTRIES

    if not PROSPEO_API_KEY:
        log_pipeline_error("prospeo_discovery", "PROSPEO_API_KEY not set.")
        return []

    payload = {
        "query": {
            "seniority": ICP_PERSONA,
            "company_size": ICP_COMPANY_SIZE,
            "industry": ICP_INDUSTRIES,
            "location": region,
        },
        "limit": target,
    }

    data = _post("/search-person", payload)
    if not data:
        log_pipeline_error("prospeo_discovery", f"No response from /search-person for region: {region}", source="prospeo")
        return []

    raw_leads = data.get("response", data.get("data", []))
    if not raw_leads:
        logger.info(f"[PROSPEO] Zero results for region: {region}")
        return []

    normalized = []
    tag = _source_tag()
    for item in raw_leads:
        email = item.get("email", "").strip()
        if not email:
            continue
        normalized.append({
            "name": item.get("full_name", "").strip(),
            "email": email,
            "company": item.get("company", {}).get("name", "").strip() if isinstance(item.get("company"), dict) else str(item.get("company", "")).strip(),
            "region": region.upper(),
            "warmth_score": "",
            "status": "new",
            "last_contacted": "",
            "followup_count": 0,
            "notes": f"source:{tag}",
        })

    logger.info(f"[PROSPEO] Discovered {len(normalized)} leads for region: {region}")
    return normalized


def enrich_person(contact_name: str, company_name: str) -> Optional[str]:
    """
    Enrich a single contact via /enrich-person.
    Returns verified email string or None.
    Requires contact_name + company_name.
    """
    if not PROSPEO_API_KEY:
        return None
    if not contact_name or not company_name:
        logger.debug(f"[PROSPEO] Skipping enrich — missing name or company.")
        return None

    payload = {
        "full_name": contact_name,
        "company": company_name,
    }
    data = _post("/enrich-person", payload)
    if not data:
        return None

    email = data.get("response", {}).get("email", "") or data.get("email", "")
    return email.strip() if email else None


def bulk_enrich_persons(contacts: list[dict]) -> list[dict]:
    """
    Bulk enrich a list of contacts via /bulk-enrich-person.
    Each contact dict must have: full_name, company.
    Returns list of dicts with email added.
    """
    if not PROSPEO_API_KEY or not contacts:
        return contacts

    payload = {"contacts": contacts}
    data = _post("/bulk-enrich-person", payload)
    if not data:
        return contacts

    results = data.get("response", data.get("data", []))
    email_map = {}
    for r in results:
        name = r.get("full_name", "")
        email = r.get("email", "")
        if name and email:
            email_map[name.lower()] = email.strip()

    for contact in contacts:
        key = contact.get("full_name", "").lower()
        if key in email_map:
            contact["email"] = email_map[key]

    return contacts
