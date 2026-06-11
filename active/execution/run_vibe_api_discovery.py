"""
run_vibe_api_discovery.py — Direct Explorium REST API client.
Calls api.explorium.ai/v1 using VIBE_PROSPECTING_API_KEY.
Runs on GitHub Actions without requiring a Claude Code MCP session or CSV export.
All ICP filters (regions, job levels, company sizes, industries) are read from
environment variables — never hardcoded.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from config import (
    STATUS_NEW,
    ICP_REGIONS,
    ICP_PERSONA,
    ICP_COMPANY_SIZE,
    ICP_INDUSTRIES,
)
from pipeline_metrics import log_pipeline_error, record_source_run, record_run_stats
from ingest_vibe_export import _validate_email, _deduplicate
from sheets_client import get_existing_name_company_pairs
from notify import alert_token_exhausted

logger = logging.getLogger(__name__)

BASE_URL = "https://api.explorium.ai/v1"
REQUEST_TIMEOUT = 60
SOURCE_NAME = "vibe_api"
ENRICH_DELAY = 0.3  # seconds between per-prospect enrich calls

_CREDIT_KEYWORDS = ("credit", "quota", "limit exceeded", "insufficient", "out of")

# ── ICP → Explorium filter mappers ────────────────────────────────────────────

# State name (lowercase) → Explorium region code
_STATE_CODE_MAP = {
    "florida": "us-fl",
    "texas": "us-tx",
    "georgia": "us-ga",
    "north carolina": "us-nc",
    "tennessee": "us-tn",
    "california": "us-ca",
    "new york": "us-ny",
    "ohio": "us-oh",
    "illinois": "us-il",
    "arizona": "us-az",
    "colorado": "us-co",
    "virginia": "us-va",
    "washington": "us-wa",
    "nevada": "us-nv",
    "michigan": "us-mi",
    "new jersey": "us-nj",
    "pennsylvania": "us-pa",
    "massachusetts": "us-ma",
    "utah": "us-ut",
    "minnesota": "us-mn",
}

# Job title keyword (lowercase) → Explorium job_level code
_PERSONA_TO_JOB_LEVEL = {
    "owner": "cxo",
    "founder": "cxo",
    "ceo": "cxo",
    "cmo": "cxo",
    "coo": "cxo",
    "cto": "cxo",
    "president": "cxo",
    "principal": "cxo",
    "partner": "partner",
    "director": "director",
    "head of": "director",
    "vp": "vp",
    "vice president": "vp",
    "manager": "manager",
}

# Explorium company_size buckets with numeric bounds for range matching
_EXPLORIUM_SIZE_BUCKETS = [
    ("1-10",       1,    10),
    ("11-50",     11,    50),
    ("51-200",    51,   200),
    ("201-500",  201,   500),
    ("501-1000", 501,  1000),
    ("1001-5000", 1001, 5000),
]

# Industry keyword (lowercase) → NAICS codes
_INDUSTRY_TO_NAICS = {
    "hvac": ["238220"],
    "heating ventilation": ["238220"],
    "heating and cooling": ["238220"],
    "air conditioning": ["238220"],
    "heating": ["238220"],
    "cooling": ["238220"],
    "mechanical contractor": ["238220"],
    "plumbing": ["238220"],
    "electrical": ["238210"],
    "roofing": ["238160"],
    "landscaping": ["561730"],
    "pest control": ["561710"],
    "cleaning": ["561720"],
    "construction": ["236220"],
    "painting": ["238320"],
}


def _build_region_codes() -> list[str]:
    """Parse ICP_REGIONS env var into Explorium region codes. Falls back to us-fl."""
    raw = ICP_REGIONS.strip()
    if not raw or raw.startswith("["):
        return ["us-fl"]
    codes = []
    for token in raw.split(","):
        name = token.strip().lower()
        if name in _STATE_CODE_MAP:
            codes.append(_STATE_CODE_MAP[name])
    return codes if codes else ["us-fl"]


def _build_job_levels() -> list[str]:
    """Parse ICP_PERSONA env var into Explorium job_level codes. Falls back to cxo+director."""
    raw = ICP_PERSONA.strip()
    if not raw or raw.startswith("["):
        return ["cxo", "partner", "director", "vp"]
    levels = set()
    for token in raw.split(","):
        token_lower = token.strip().lower()
        for keyword, level in _PERSONA_TO_JOB_LEVEL.items():
            if keyword in token_lower:
                levels.add(level)
                break
    return list(levels) if levels else ["cxo", "partner", "director", "vp"]


def _build_company_sizes() -> list[str]:
    """Parse ICP_COMPANY_SIZE env var into Explorium company_size codes.
    Matches any Explorium bucket that overlaps the requested range.
    Falls back to 1-10, 11-50, 51-200."""
    raw = ICP_COMPANY_SIZE.strip()
    if not raw or raw.startswith("["):
        return ["1-10", "11-50", "51-200"]
    sizes = set()
    for token in raw.split(","):
        token = token.strip()
        try:
            parts = token.split("-")
            lo = int(parts[0])
            hi = int(parts[1]) if len(parts) > 1 else lo
        except (ValueError, IndexError):
            continue
        for bucket, b_lo, b_hi in _EXPLORIUM_SIZE_BUCKETS:
            if lo <= b_hi and hi >= b_lo:
                sizes.add(bucket)
    return list(sizes) if sizes else ["1-10", "11-50", "51-200"]


def _build_naics_codes() -> list[str]:
    """Parse ICP_INDUSTRIES env var into NAICS codes. Falls back to 238220 (HVAC)."""
    raw = ICP_INDUSTRIES.strip()
    if not raw or raw.startswith("["):
        return ["238220"]
    codes = set()
    raw_lower = raw.lower()
    for keyword, naics_list in _INDUSTRY_TO_NAICS.items():
        if keyword in raw_lower:
            codes.update(naics_list)
    return list(codes) if codes else ["238220"]


# ── Core API functions ─────────────────────────────────────────────────────────

def _is_credit_exhausted(resp) -> bool:
    try:
        body = resp.text.lower()
        return any(kw in body for kw in _CREDIT_KEYWORDS)
    except Exception:
        return False


def _compute_warmth_score(prospect: dict, has_email: bool) -> int:
    """Score 0–10: seniority (5) + company size (2) + linkedin url (2) + email (1)."""
    score = 0
    job_level = prospect.get("job_level", "").lower()
    if job_level in ("cxo", "partner"):
        score += 5
    elif job_level in ("director", "vp"):
        score += 3
    else:
        score += 1
    company_size = prospect.get("company_size", "")
    if company_size in ("11-50", "51-200"):
        score += 2
    elif company_size == "1-10":
        score += 1
    if prospect.get("linkedin_url", "").strip():
        score += 2
    if has_email:
        score += 1
    return min(score, 10)


def _headers(api_key: str) -> dict:
    return {
        "api_key": api_key,
        "content-type": "application/json",
        "accept": "application/json",
    }


def _source_tag() -> str:
    return f"vibe_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def _fetch_prospects(api_key: str, target: int) -> list[dict]:
    """Fetch prospect records via POST /v1/prospects with ICP filters from env vars."""
    region_codes = _build_region_codes()
    job_levels = _build_job_levels()
    company_sizes = _build_company_sizes()
    naics_codes = _build_naics_codes()
    logger.info(
        f"[VIBE API] Filters — regions: {region_codes}, "
        f"job_levels: {job_levels}, sizes: {company_sizes}, naics: {naics_codes}"
    )
    body = {
        "mode": "full",
        "page": 1,
        "page_size": min(target, 100),
        "filters": {
            "company_region_country_code": {"values": region_codes},
            "job_level": {"values": job_levels},
            "company_size": {"values": company_sizes},
            "naics_category": {"values": naics_codes},
        },
    }
    try:
        resp = requests.post(
            f"{BASE_URL}/prospects",
            json=body,
            headers=_headers(api_key),
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            if resp.status_code == 402 or _is_credit_exhausted(resp):
                alert_token_exhausted("Explorium", resp.text[:300])
            logger.error(f"[VIBE API] fetch_prospects {resp.status_code}: {resp.text[:500]}")
            return []
        data = resp.json()
        prospects = data.get("data", [])
        logger.info(
            f"[VIBE API] fetch_prospects: {len(prospects)} returned "
            f"(total_results={data.get('total_results', '?')})"
        )
        if prospects:
            logger.debug(f"[VIBE API] First prospect fields: {list(prospects[0].keys())}")
            logger.debug(f"[VIBE API] First prospect sample: {prospects[0]}")
        return prospects
    except Exception as e:
        logger.error(f"[VIBE API] fetch_prospects error: {e}")
        return []


def _enrich_email(api_key: str, prospect_id: str) -> str:
    """Enrich a single prospect to get their email. Returns email string or ''."""
    body = {
        "prospect_id": prospect_id,
        "parameters": {"contact_types": ["email"]},
    }
    try:
        resp = requests.post(
            f"{BASE_URL}/prospects/contacts_information/enrich",
            json=body,
            headers=_headers(api_key),
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            if resp.status_code == 402 or _is_credit_exhausted(resp):
                alert_token_exhausted("Explorium", resp.text[:300])
            logger.debug(f"[VIBE API] enrich_email {resp.status_code} for {prospect_id[:12]}: {resp.text[:200]}")
            return ""
        data = resp.json().get("data", {})
        email = data.get("professions_email", "").strip()
        if not email:
            for entry in data.get("emails", []):
                if entry.get("email"):
                    email = entry["email"].strip()
                    break
        return email
    except Exception as e:
        logger.debug(f"[VIBE API] enrich_email error for {prospect_id[:12]}: {e}")
        return ""


def _normalize_prospect(prospect: dict, email: str, source_tag: str) -> dict | None:
    """Normalize a REST API prospect object to Sheets schema."""
    if email and not _validate_email(email):
        return None
    company = (
        prospect.get("company_name", "")
        or prospect.get("organization_name", "")
    ).strip()
    if not company:
        return None
    name = (
        prospect.get("full_name", "")
        or f"{prospect.get('first_name', '')} {prospect.get('last_name', '')}".strip()
    ).strip()
    region = (
        prospect.get("region_name", "")
        or prospect.get("country_name", "")
    ).strip().upper()
    return {
        "name": name,
        "email": email.lower(),
        "company": company,
        "region": region,
        "warmth_score": _compute_warmth_score(prospect, bool(email)),
        "status": STATUS_NEW,
        "last_contacted": "",
        "followup_count": 0,
        "notes": f"source:{source_tag}",
        "facebook_url": prospect.get("facebook_url", prospect.get("facebook", "")).strip(),
        "linkedin_url": prospect.get("linkedin_url", "").strip(),
    }


def run_vibe_api_discovery(target: int = 100) -> dict:
    """
    Discover leads via Explorium REST API and write to Sheets.
    Returns stats dict: new_leads, dupes_skipped, failed, source.
    """
    from sheets_client import get_existing_emails, append_leads_batch

    stats = {"new_leads": 0, "dupes_skipped": 0, "failed": 0, "source": SOURCE_NAME}

    api_key = os.getenv("VIBE_PROSPECTING_API_KEY", "").strip()
    if not api_key:
        log_pipeline_error(SOURCE_NAME, "VIBE_PROSPECTING_API_KEY not set.")
        return stats

    prospects = _fetch_prospects(api_key, target)
    if not prospects:
        log_pipeline_error(SOURCE_NAME, "fetch_prospects returned no results.")
        record_source_run(SOURCE_NAME, 0)
        return stats

    source_tag = _source_tag()
    normalized = []
    failed = 0

    for i, prospect in enumerate(prospects):
        prospect_id = prospect.get("prospect_id", "")
        if not prospect_id:
            failed += 1
            continue
        email = _enrich_email(api_key, prospect_id)
        if i < len(prospects) - 1:
            time.sleep(ENRICH_DELAY)
        record = _normalize_prospect(prospect, email, source_tag)
        if record:
            normalized.append(record)
        else:
            failed += 1

    stats["failed"] = failed
    logger.info(f"[VIBE API] Normalized {len(normalized)} leads, failed {failed}.")

    if not normalized:
        record_source_run(SOURCE_NAME, 0)
        return stats

    existing_emails = get_existing_emails()
    existing_no_email = get_existing_name_company_pairs()
    clean, dupe_count = _deduplicate(normalized, existing_emails, existing_no_email)
    stats["dupes_skipped"] = dupe_count

    if clean:
        written = append_leads_batch(clean)
        stats["new_leads"] = written
        has_email_pct = sum(1 for lead in clean if lead.get("email")) / len(clean)
        record_run_stats(SOURCE_NAME, written, has_email_pct, dupe_count)

    record_source_run(SOURCE_NAME, len(clean))
    logger.info(
        f"[VIBE API] Done. Written: {stats['new_leads']}, "
        f"Dupes: {dupe_count}, Failed: {failed}"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    result = run_vibe_api_discovery(target=1)
    print(result)
