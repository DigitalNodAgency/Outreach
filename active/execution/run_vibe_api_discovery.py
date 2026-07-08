"""
run_vibe_api_discovery.py — Direct Explorium REST API client.
Calls api.explorium.ai/v1 using VIBE_PROSPECTING_API_KEY.
Runs on GitHub Actions without requiring a Claude Code MCP session or CSV export.
All ICP filters (regions, job levels, company sizes, industries) are read from
environment variables — never hardcoded.
"""

import hashlib
import json
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
    ICP_LINKEDIN_CATEGORIES,
    ICP_DENY_KEYWORDS,
)
from pipeline_metrics import (
    log_pipeline_error,
    record_source_run,
    record_run_stats,
    log_failed_record,
)
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

# The Explorium `company_region_country_code` filter only accepts region codes
# (e.g. us-ca), NOT a bare country code ("us" → 422). So a nationwide ICP must
# be expanded to every US state/territory region code.
_US_STATE_CODES = [
    "us-al", "us-ak", "us-az", "us-ar", "us-ca", "us-co", "us-ct", "us-de",
    "us-fl", "us-ga", "us-hi", "us-id", "us-il", "us-in", "us-ia", "us-ks",
    "us-ky", "us-la", "us-me", "us-md", "us-ma", "us-mi", "us-mn", "us-ms",
    "us-mo", "us-mt", "us-ne", "us-nv", "us-nh", "us-nj", "us-nm", "us-ny",
    "us-nc", "us-nd", "us-oh", "us-ok", "us-or", "us-pa", "us-ri", "us-sc",
    "us-sd", "us-tn", "us-tx", "us-ut", "us-vt", "us-va", "us-wa", "us-wv",
    "us-wi", "us-wy", "us-dc",
]

# Country-level token (lowercase) → expands to all US region codes.
# Lets ICP_REGIONS="USA" target the whole country instead of silently
# falling back to a single state.
_COUNTRY_REGION_EXPANSION = {
    "usa": _US_STATE_CODES,
    "us": _US_STATE_CODES,
    "u.s.": _US_STATE_CODES,
    "u.s.a.": _US_STATE_CODES,
    "united states": _US_STATE_CODES,
    "united states of america": _US_STATE_CODES,
    "america": _US_STATE_CODES,
}

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

# Industry keyword (lowercase) → NAICS codes.
# LEGACY (retired v17): superseded by the linkedin_category filter (see
# _build_linkedin_categories). NO LONGER called in the active filter path — inferred
# NAICS misclassified martech (TapClicks → 541613) and PR firms into the agency pool,
# and Explorium forbids combining naics_category with linkedin_category. Kept for
# reference only. Matched as a substring against the lowercased ICP_INDUSTRIES string.
_INDUSTRY_TO_NAICS = {
    # Marketing / advertising / PR agencies (v13 agency pivot). Codes verified
    # against the live Explorium naics_category taxonomy.
    "marketing": ["541810", "541820", "541613", "541890"],
    "advertising": ["541810", "541890", "541830"],
    "digital marketing": ["541613", "541810", "541890"],
    "social media": ["541613", "541810", "541890"],
    "public relations": ["541820"],
    "communications": ["541820"],
    "media buying": ["541830"],
    "branding": ["541810"],
    "creative agency": ["541810"],
    "design agency": ["541810"],
    # HVAC / home services (retired ICP — kept so old configs still resolve)
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
    """Parse ICP_REGIONS env var into Explorium company_region_country_code values.

    Accepts country-level tokens (USA / United States → 'us') and US state names
    (→ us-xx region codes). Returns [] on unset/unrecognized input so the caller
    omits the region filter entirely — never silently reverts to a single state."""
    raw = ICP_REGIONS.strip()
    if not raw or raw.startswith("["):
        logger.warning("[VIBE API] ICP_REGIONS unset/placeholder; region filter omitted.")
        return []
    codes: list[str] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name:
            continue
        if name in _COUNTRY_REGION_EXPANSION:
            codes.extend(_COUNTRY_REGION_EXPANSION[name])
        elif name in _STATE_CODE_MAP:
            codes.append(_STATE_CODE_MAP[name])
        else:
            logger.warning(f"[VIBE API] Unrecognized ICP_REGIONS token '{name}' — skipped.")
    seen: set[str] = set()
    deduped = [c for c in codes if not (c in seen or seen.add(c))]
    if not deduped:
        logger.warning(f"[VIBE API] No region codes resolved from ICP_REGIONS={raw!r}; filter omitted.")
    return deduped


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
    # Sorted so the resolved list (and the cursor key derived from it) is stable
    # across processes — a set's iteration order is randomized per PYTHONHASHSEED.
    return sorted(levels) if levels else ["cxo", "director", "partner", "vp"]


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
    # Sorted for a process-stable resolved list / cursor key (see _build_job_levels).
    return sorted(sizes) if sizes else ["1-10", "11-50", "51-200"]


def _build_naics_codes() -> list[str]:
    """Parse ICP_INDUSTRIES env var into NAICS codes.

    Returns [] on unset/unrecognized input so the caller omits the industry
    filter entirely — never silently reverts to HVAC (238220)."""
    raw = ICP_INDUSTRIES.strip()
    if not raw or raw.startswith("["):
        logger.warning("[VIBE API] ICP_INDUSTRIES unset/placeholder; industry filter omitted.")
        return []
    codes = set()
    raw_lower = raw.lower()
    for keyword, naics_list in _INDUSTRY_TO_NAICS.items():
        if keyword in raw_lower:
            codes.update(naics_list)
    if not codes:
        logger.warning(f"[VIBE API] No NAICS match for ICP_INDUSTRIES={raw!r}; industry filter omitted.")
        return []
    return sorted(codes)


def _build_linkedin_categories() -> list[str]:
    """Parse ICP_LINKEDIN_CATEGORIES into Explorium linkedin_category filter values.

    This is the PRIMARY industry filter (replaces naics_category, v17). Explorium allows
    only one of naics / linkedin / google category per query, and the self-labeled
    LinkedIn category is a much cleaner agency signal than inferred NAICS. Returns [] on
    unset/placeholder input so the caller omits the filter and warns — never silently
    broadens the search. Sorted for a process-stable resolved list / cursor key."""
    raw = (ICP_LINKEDIN_CATEGORIES or "").strip()
    if not raw or raw.startswith("["):
        logger.warning("[VIBE API] ICP_LINKEDIN_CATEGORIES unset/placeholder; industry filter omitted.")
        return []
    seen: set[str] = set()
    cats: list[str] = []
    for token in raw.split(","):
        c = token.strip().lower()
        if c and c not in seen:
            seen.add(c)
            cats.append(c)
    return sorted(cats)


def _deny_keywords() -> list[str]:
    """Parse ICP_DENY_KEYWORDS into a lowercase keyword list. Empty → screen disabled."""
    raw = (ICP_DENY_KEYWORDS or "").strip()
    if not raw:
        return []
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def _icp_deny_reason(prospect: dict, deny: list[str]) -> str:
    """Return the first deny keyword found in the prospect's COMPANY NAME, else "".

    Credit-free secondary screen — the linkedin_category filter is the primary gate;
    this catches the occasional off-ICP company (staffing/recruiting/pure-tech) that
    self-labeled into a marketing/advertising LinkedIn category. Matches the company
    name only (not personal skills/experience, which are too noisy — an agency owner
    routinely lists "SaaS"/"software" skills)."""
    if not deny:
        return ""
    name = (prospect.get("company_name", "") or prospect.get("organization_name", "") or "").lower()
    if not name:
        return ""
    for kw in deny:
        if kw in name:
            return kw
    return ""


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


_EXPLORIUM_PAGE_SIZE = 100  # Explorium API hard cap per page

# Bounds page-fetch spend and guards infinite loops when everything is a dupe.
_SCAN_CAP_MULT = 6    # scan at most target*6 records per run...
_SCAN_CAP_MIN = 300   # ...but never fewer than this (covers small targets after a cursor reset)


# ── Discovery pagination cursor ────────────────────────────────────────────────
# Explorium returns prospects in a stable order, so fetching page 1 on every run
# re-reads the same top-of-pool prospects — which dedup then drops as already-seen,
# yielding 0 new leads even though tens of thousands match. We persist a per-ICP
# record OFFSET across runs so each run walks the NEXT slice of the pool. The offset
# is keyed by a hash of the resolved filters, so changing the ICP starts a fresh walk
# automatically. The cursor is stored IN THE GOOGLE SHEET ('Discovery State' tab) via
# sheets_client so it survives GitHub Actions' ephemeral filesystem and is shared
# across every run host (local or cloud), keyed off the same SPREADSHEET_ID.

def _filter_key(filters: dict) -> str:
    """Stable short hash of the resolved filter set — the cursor key. Changing the
    ICP changes the key, so the offset resets to 0 automatically (pivot-safe).

    The key MUST be identical across processes for the same ICP, or the cursor never
    resumes. Some filter value lists are built from a set, whose iteration order is
    randomized per process (PYTHONHASHSEED) — so we sort every value list before
    hashing. Without this, each run computed a different key, fell back to offset 0,
    and re-scraped the top of the pool every time (the all-dupes bug)."""
    normalized = {
        k: {**v, "values": sorted(v["values"])}
        if isinstance(v, dict) and isinstance(v.get("values"), list) else v
        for k, v in filters.items()
    }
    blob = json.dumps(normalized, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _load_cursor() -> dict:
    """Load the per-ICP discovery cursor from the Sheet, as
    {filter_key: {"offset": int, "total_results": int}}. Returns {} on any error so
    discovery degrades to offset 0 rather than crashing."""
    try:
        from sheets_client import get_discovery_cursor
        return get_discovery_cursor()
    except Exception as e:
        logger.warning(f"[VIBE API] Could not load discovery cursor: {e}")
        return {}


def _save_cursor(filter_key: str, offset: int, total_results: int) -> None:
    """Persist the per-ICP discovery offset to the Sheet. Best-effort (never raises)."""
    try:
        from sheets_client import set_discovery_cursor
        set_discovery_cursor(filter_key, offset, total_results)
    except Exception as e:
        logger.warning(f"[VIBE API] Could not save discovery cursor: {e}")


def _fetch_page(api_key: str, filters: dict, page: int) -> tuple[list[dict], int, bool]:
    """Fetch one /v1/prospects page (fixed page_size). Returns
    (prospects, total_results, ok). ok=False signals a hard error → caller stops."""
    body = {"mode": "full", "page": page, "page_size": _EXPLORIUM_PAGE_SIZE, "filters": filters}
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
            logger.error(f"[VIBE API] fetch_prospects page {page} {resp.status_code}: {resp.text[:500]}")
            return [], 0, False
        data = resp.json()
        prospects = data.get("data", [])
        total_available = data.get("total_results", 0)
        logger.info(
            f"[VIBE API] page {page}: {len(prospects)} returned "
            f"(total_results={total_available})"
        )
        return prospects, total_available, True
    except Exception as e:
        logger.error(f"[VIBE API] fetch_prospects page {page} error: {e}")
        return [], 0, False


def _prospect_pair(prospect: dict) -> tuple[str, str] | None:
    """(name_lower, company_lower) dedup key for a raw prospect, or None when either
    field is missing (record is kept; post-enrichment dedup remains the safety net)."""
    name = (
        prospect.get("full_name", "")
        or f"{prospect.get('first_name', '')} {prospect.get('last_name', '')}".strip()
    ).strip()
    company = (
        prospect.get("company_name", "")
        or prospect.get("organization_name", "")
    ).strip()
    if not name or not company:
        return None
    return (name.lower(), company.lower())


def _fetch_prospects(api_key: str, target: int, known_pairs: set) -> tuple[list[dict], int, int]:
    """Fetch up to `target` NEW prospect records via POST /v1/prospects with ICP filters
    from env vars, resuming from the persisted per-ICP offset so each run pulls the
    NEXT slice of the pool (not the same page-1 prospects every time). Prospects whose
    (name, company) pair is already known (in `known_pairs` or already returned earlier
    in this same walk) are skipped at fetch time, before enrichment — so a cursor
    landing on already-scraped records doesn't waste enrichment calls on guaranteed
    dupes. Prospects failing the ICP deny screen are likewise rejected pre-enrichment.
    Returns (prospects, skipped_known, icp_rejected)."""
    region_codes = _build_region_codes()
    job_levels = _build_job_levels()
    company_sizes = _build_company_sizes()
    linkedin_categories = _build_linkedin_categories()
    deny = _deny_keywords()
    logger.info(
        f"[VIBE API] Filters — regions: {region_codes}, "
        f"job_levels: {job_levels}, sizes: {company_sizes}, "
        f"linkedin_categories: {linkedin_categories}; deny_keywords: {deny}"
    )

    # Only include a filter key when its mapper resolved values; an empty list
    # would otherwise risk matching nothing (or being silently ignored).
    # Industry filter: linkedin_category (self-labeled) REPLACES naics_category (v17).
    # Explorium permits only ONE of naics/linkedin/google category per query, and the
    # LinkedIn category is a far cleaner agency signal — inferred NAICS misfiled martech
    # (e.g. TapClicks → 541613) and PR firms into the pool. See CLAUDE.md v17.
    filters: dict = {}
    if region_codes:
        filters["company_region_country_code"] = {"values": region_codes}
    if job_levels:
        filters["job_level"] = {"values": job_levels}
    if company_sizes:
        filters["company_size"] = {"values": company_sizes}
    if linkedin_categories:
        filters["linkedin_category"] = {"values": linkedin_categories}

    # Resume from the persisted offset for this exact filter set.
    key = _filter_key(filters)
    entry = _load_cursor().get(key, {})
    start_offset = int(entry.get("offset", 0) or 0)
    prior_total = int(entry.get("total_results", 0) or 0)
    # If we previously walked off the end of the pool, wrap to the start.
    if prior_total and start_offset >= prior_total:
        logger.info(f"[VIBE API] Cursor offset {start_offset} >= pool {prior_total}; wrapping to 0.")
        start_offset = 0

    all_prospects: list[dict] = []
    total_available = prior_total
    offset = start_offset
    wrapped = False  # wrap to the start at most once per run (guards against a loop)

    # Scan cap bounds how many pool records we'll walk this run — without it, a run
    # where every remaining record is already known (e.g. right after a cursor
    # reset landed on already-scraped territory) would page through the entire
    # pool looking for `target` new ones.
    scan_cap = max(target * _SCAN_CAP_MULT, _SCAN_CAP_MIN)
    scanned = 0
    skipped_known = 0
    icp_rejected = 0
    seen_this_run: set = set()

    while len(all_prospects) < target and scanned < scan_cap:
        page = offset // _EXPLORIUM_PAGE_SIZE + 1   # Explorium pages are 1-indexed
        skip = offset % _EXPLORIUM_PAGE_SIZE        # drop records already consumed
        page_prospects, page_total, ok = _fetch_page(api_key, filters, page)
        if not ok:
            break
        if page_total:
            total_available = page_total
        usable = page_prospects[skip:] if skip else page_prospects
        if not usable:
            # End of pool (empty page) or offset landed past the last record.
            if not wrapped and offset > 0:
                logger.info("[VIBE API] Reached end of pool; wrapping cursor to offset 0.")
                offset = 0
                wrapped = True
                continue
            break
        for rec in usable:
            if len(all_prospects) >= target or scanned >= scan_cap:
                break
            scanned += 1
            offset += 1
            pair = _prospect_pair(rec)
            if pair and (pair in known_pairs or pair in seen_this_run):
                skipped_known += 1
                continue
            # Secondary ICP screen, BEFORE enrichment — denied prospects cost no
            # enrichment call and don't count toward `target`.
            deny_kw = _icp_deny_reason(rec, deny)
            if deny_kw:
                icp_rejected += 1
                log_failed_record(
                    {
                        "company": rec.get("company_name", ""),
                        "website": rec.get("company_website", ""),
                        "name": (rec.get("full_name", "")
                                 or f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip()),
                        "prospect_id": rec.get("prospect_id", ""),
                    },
                    reason=f"icp_deny_keyword:{deny_kw}",
                )
                continue
            if pair is not None:
                seen_this_run.add(pair)
            all_prospects.append(rec)
        # Stop if we've now consumed the entire pool.
        if total_available and offset >= total_available:
            break

    # Persist the new offset so the next run continues where this one left off.
    _save_cursor(key, offset, total_available)

    if skipped_known > 0:
        logger.info(
            f"[VIBE API] Skipped {skipped_known} already-known prospect(s) at fetch "
            f"time (pre-enrichment) — no enrichment call spent on them."
        )
    if icp_rejected > 0:
        logger.info(
            f"[VIBE API] Rejected {icp_rejected} prospect(s) on the ICP deny screen "
            f"(pre-enrichment) — logged to failed_records.jsonl."
        )

    if all_prospects:
        logger.debug(f"[VIBE API] First prospect fields: {list(all_prospects[0].keys())}")
        logger.debug(f"[VIBE API] First prospect sample: {all_prospects[0]}")
    logger.info(
        f"[VIBE API] Total fetched: {len(all_prospects)} prospects (target={target}); "
        f"cursor offset {start_offset} -> {offset} of {total_available}; "
        f"fetch-skipped {skipped_known} already-known, {icp_rejected} ICP-denied."
    )
    return all_prospects, skipped_known, icp_rejected


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
    # Record the company website in notes for ICP auditability — the /v1/prospects
    # payload carries no company-industry field, so the website is the best free signal
    # for eyeballing whether a written lead is a genuine agency after the fact.
    website = (prospect.get("company_website", "") or "").strip()
    notes = f"source:{source_tag}"
    if website:
        notes += f" | site:{website}"
    return {
        "name": name,
        "email": email.lower(),
        "company": company,
        "region": region,
        "warmth_score": _compute_warmth_score(prospect, bool(email)),
        "status": STATUS_NEW,
        "last_contacted": "",
        "followup_count": 0,
        "notes": notes,
        "facebook_url": prospect.get("facebook_url", prospect.get("facebook", "")).strip(),
        "linkedin_url": prospect.get("linkedin_url", "").strip(),
    }


def run_vibe_api_discovery(target: int = 100) -> dict:
    """
    Discover leads via Explorium REST API and write to Sheets.
    Returns stats dict: new_leads, dupes_skipped, failed, source.
    """
    from sheets_client import get_existing_emails, get_all_name_company_pairs, append_leads_batch

    stats = {"new_leads": 0, "dupes_skipped": 0, "icp_rejected": 0, "failed": 0, "source": SOURCE_NAME}

    api_key = os.getenv("VIBE_PROSPECTING_API_KEY", "").strip()
    if not api_key:
        log_pipeline_error(SOURCE_NAME, "VIBE_PROSPECTING_API_KEY not set.")
        return stats

    # Loaded once up front and reused below (for the post-enrichment _deduplicate call)
    # so a single run only pays for one Leads-tab read of each kind.
    existing_emails = get_existing_emails()
    known_pairs = get_all_name_company_pairs()

    prospects, fetch_skips, icp_rejected = _fetch_prospects(api_key, target, known_pairs)
    stats["icp_rejected"] = icp_rejected
    if not prospects:
        stats["dupes_skipped"] = fetch_skips
        if fetch_skips > 0 or icp_rejected > 0:
            logger.info(
                f"[VIBE API] No new in-ICP prospects in this slice "
                f"(already-known {fetch_skips}, ICP-denied {icp_rejected})."
            )
        else:
            # Genuinely empty result (API failure, exhausted pool, etc.) — a real error.
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
        stats["dupes_skipped"] = fetch_skips
        record_source_run(SOURCE_NAME, 0)
        return stats

    existing_no_email = get_existing_name_company_pairs()
    clean, dupe_count = _deduplicate(normalized, existing_emails, existing_no_email)
    stats["dupes_skipped"] = fetch_skips + dupe_count

    if clean:
        written = append_leads_batch(clean)
        stats["new_leads"] = written
        has_email_pct = sum(1 for lead in clean if lead.get("email")) / len(clean)
        record_run_stats(SOURCE_NAME, written, has_email_pct, dupe_count)

    record_source_run(SOURCE_NAME, len(clean))
    logger.info(
        f"[VIBE API] Done. Written: {stats['new_leads']}, "
        f"Dupes: {stats['dupes_skipped']} (fetch-skipped {fetch_skips} + post-enrich {dupe_count}), "
        f"ICP-denied: {icp_rejected}, Failed: {failed}"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    result = run_vibe_api_discovery(target=1)
    print(result)
