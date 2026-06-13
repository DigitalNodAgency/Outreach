"""
enrich_linkedin_step.py — Phase 1 Step 3.5: Social URL enrichment via Serper.
Fills in missing linkedin_url (col K) and facebook_url (col J) for leads.
Runs after email enrichment. Skips gracefully if SERPER_API_KEY is not set.
"""

import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parents[1] / "outreach"))

from config import (
    COL_NAME,
    COL_COMPANY,
    COL_FACEBOOK_URL,
    COL_LINKEDIN_URL,
)
from pipeline_metrics import log_pipeline_error

logger = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_SERPER_DELAY = 0.2
_SERPER_RESULTS = 3
_LI_PERSON_RE = re.compile(r"linkedin\.com/in/[a-zA-Z0-9_%-]+", re.IGNORECASE)
_FB_RE = re.compile(
    r"facebook\.com/(?!groups/|events/|marketplace/|watch/|stories/|share/|sharer/)"
    r"(?:profile\.php\?id=\d+|[a-zA-Z0-9._%-]{3,})",
    re.IGNORECASE,
)


def _normalize_linkedin_url(url: str) -> str:
    from social_engine import _normalize_linkedin_url as _norm
    return _norm(url)


def _serper_search(query: str, api_key: str, label: str) -> list[dict]:
    try:
        resp = requests.post(
            _SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": _SERPER_RESULTS},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.warning(f"[SOCIAL] Serper request failed for {label!r}: {e}")
        return []

    if resp.status_code == 402 or (resp.status_code != 200 and any(
        k in resp.text.lower() for k in ("quota", "credit", "limit exceeded", "insufficient")
    )):
        from notify import alert_token_exhausted
        alert_token_exhausted("Serper", resp.text[:200])
    if resp.status_code != 200:
        logger.warning(f"[SOCIAL] Serper {resp.status_code} for {label!r}: {resp.text[:120]}")
        return []

    return resp.json().get("organic", [])[:_SERPER_RESULTS]


def _search_linkedin_url(name: str, company: str, api_key: str) -> str | None:
    query = f'site:linkedin.com/in "{name}" "{company}"' if company else f'site:linkedin.com/in "{name}"'
    for result in _serper_search(query, api_key, name):
        link = result.get("link", "")
        if "linkedin.com/company/" in link.lower():
            continue
        if _LI_PERSON_RE.search(link):
            return _normalize_linkedin_url(link)
    return None


def _search_facebook_url(name: str, company: str, api_key: str) -> str | None:
    query = f'site:facebook.com "{name}" "{company}"' if company else f'site:facebook.com "{name}"'
    for result in _serper_search(query, api_key, name):
        link = result.get("link", "")
        m = _FB_RE.search(link)
        if m:
            raw = m.group(0)
            if "profile.php" in raw.lower():
                # Preserve the numeric ID query param, drop everything else
                id_part = raw.split("id=")[-1].split("&")[0]
                base = raw.split("?")[0]
                return f"https://{base}?id={id_part}"
            return f"https://{raw.rstrip('/')}"
    return None


def run_social_url_enrichment() -> dict:
    """
    Fetch all leads missing linkedin_url or facebook_url, query Serper, write results back.
    Returns stats: {li_found, li_not_found, fb_found, fb_not_found, skipped, errors}.
    """
    stats = {"li_found": 0, "li_not_found": 0, "fb_found": 0, "fb_not_found": 0, "skipped": 0, "errors": 0}

    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        logger.info("[SOCIAL] SERPER_API_KEY not set — skipping social URL enrichment.")
        return stats

    # Route Sheets I/O through the shared client (cached auth + 429 backoff)
    # so this step reuses Phase 1's connection and survives transient quota errors.
    from sheets_client import get_leads_raw_values, update_lead_cell

    try:
        all_values = get_leads_raw_values()
    except Exception as e:
        log_pipeline_error("social_enrichment", f"Sheets connect failed: {e}")
        logger.error(f"[SOCIAL] Sheets connect failed: {e}")
        return stats

    max_col = max(COL_LINKEDIN_URL, COL_FACEBOOK_URL) + 1

    for i, row in enumerate(all_values[1:], start=2):
        if len(row) < max_col:
            row += [""] * (max_col - len(row))

        name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""

        if not name:
            stats["skipped"] += 1
            continue

        needs_li = not row[COL_LINKEDIN_URL].strip()
        needs_fb = not row[COL_FACEBOOK_URL].strip()

        if not needs_li and not needs_fb:
            continue

        if needs_li:
            url = _search_linkedin_url(name, company, api_key)
            time.sleep(_SERPER_DELAY)
            if url:
                logger.info(f"[SOCIAL] LinkedIn {name} @ {company or '?'} -> {url} (row {i})")
                try:
                    update_lead_cell(i, COL_LINKEDIN_URL, url)
                    stats["li_found"] += 1
                except Exception as e:
                    log_pipeline_error("social_enrichment", f"LI write failed row {i}: {e}")
                    logger.error(f"[SOCIAL] LI write failed row {i}: {e}")
                    stats["errors"] += 1
            else:
                stats["li_not_found"] += 1

        if needs_fb:
            fb_url = _search_facebook_url(name, company, api_key)
            time.sleep(_SERPER_DELAY)
            if fb_url:
                logger.info(f"[SOCIAL] Facebook {name} @ {company or '?'} -> {fb_url} (row {i})")
                try:
                    update_lead_cell(i, COL_FACEBOOK_URL, fb_url)
                    stats["fb_found"] += 1
                except Exception as e:
                    log_pipeline_error("social_enrichment", f"FB write failed row {i}: {e}")
                    logger.error(f"[SOCIAL] FB write failed row {i}: {e}")
                    stats["errors"] += 1
            else:
                stats["fb_not_found"] += 1

    logger.info(
        f"[SOCIAL] Done — LI found: {stats['li_found']}, LI not found: {stats['li_not_found']}, "
        f"FB found: {stats['fb_found']}, FB not found: {stats['fb_not_found']}, "
        f"skipped: {stats['skipped']}, errors: {stats['errors']}"
    )
    return stats


# Backward-compat alias
run_linkedin_url_enrichment = run_social_url_enrichment
