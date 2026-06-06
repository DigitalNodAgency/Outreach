"""
enrich_linkedin_urls.py — One-shot back-fill of linkedin_url (column K) for existing leads.

Reads all leads missing linkedin_url, queries Serper (site:linkedin.com/in) for each,
validates the result is a person profile, and writes found URLs back to the Leads sheet.

Usage:
  python scripts/enrich_linkedin_urls.py          # dry-run (safe, no writes)
  python scripts/enrich_linkedin_urls.py --live   # writes to Sheets

Delete this script after the one-off run is complete.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "active" / "outreach"))

from config import (
    GOOGLE_SERVICE_ACCOUNT_JSON,
    SPREADSHEET_ID,
    COL_NAME,
    COL_EMAIL,
    COL_COMPANY,
    COL_LINKEDIN_URL,
)
import gspread
import requests
from google.oauth2.service_account import Credentials
from social_engine import _normalize_linkedin_url

_LOG_DIR = Path(__file__).parents[1] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / f"linkedin_enrichment_{datetime.now().strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_LOG_FILE), mode="a"),
    ],
)
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SERPER_URL = "https://google.serper.dev/search"
SERPER_DELAY = 0.2
SERPER_RESULTS = 3
_LI_PERSON_RE = re.compile(r"linkedin\.com/in/[a-zA-Z0-9_%-]+", re.IGNORECASE)


def _get_client() -> gspread.Client:
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _search_linkedin_url(name: str, company: str, api_key: str) -> str | None:
    query = f'site:linkedin.com/in "{name}" "{company}"' if company else f'site:linkedin.com/in "{name}"'
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": SERPER_RESULTS},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.warning(f"Serper request failed for {name!r}: {e}")
        return None

    if resp.status_code != 200:
        logger.warning(f"Serper {resp.status_code} for {name!r}: {resp.text[:120]}")
        return None

    for result in resp.json().get("organic", [])[:SERPER_RESULTS]:
        link = result.get("link", "")
        if "linkedin.com/company/" in link.lower():
            continue
        if _LI_PERSON_RE.search(link):
            return _normalize_linkedin_url(link)

    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Write found URLs to Sheets")
    args = parser.parse_args()
    live = args.live

    serper_api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not serper_api_key:
        logger.error("SERPER_API_KEY not set in .env — aborting.")
        sys.exit(1)

    logger.info(f"{'[LIVE]' if live else '[DRY-RUN]'} LinkedIn URL enrichment starting.")

    client = _get_client()
    ws = client.open_by_key(SPREADSHEET_ID).worksheet("Leads")
    all_values = ws.get_all_values()

    found = not_found = already_has = skipped = errors = 0

    for i, row in enumerate(all_values[1:], start=2):
        if len(row) <= COL_LINKEDIN_URL:
            row += [""] * (COL_LINKEDIN_URL + 1 - len(row))

        linkedin_url = row[COL_LINKEDIN_URL].strip()
        if linkedin_url:
            already_has += 1
            continue

        name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        email = row[COL_EMAIL].strip() if len(row) > COL_EMAIL else ""

        if not name:
            logger.info(f"  SKIP (no name): row {i} — {email or '(no email)'}")
            skipped += 1
            continue

        url = _search_linkedin_url(name, company, serper_api_key)
        time.sleep(SERPER_DELAY)

        if not url:
            logger.info(f"  NOT FOUND: {name} @ {company or '(no company)'}")
            not_found += 1
            continue

        label = "WRITE" if live else "DRY-RUN"
        logger.info(f"  {label}: {name} @ {company or '(no company)'} -> {url} (row {i})")

        if live:
            try:
                ws.update_cell(i, COL_LINKEDIN_URL + 1, url)
                found += 1
            except Exception as e:
                logger.error(f"  ERROR writing row {i}: {e}")
                errors += 1
        else:
            found += 1

    logger.info("=" * 55)
    logger.info(f"{'[DRY-RUN] ' if not live else ''}Done.")
    logger.info(f"  Already had URL : {already_has}")
    logger.info(f"  Found           : {found}")
    logger.info(f"  Not found       : {not_found}")
    logger.info(f"  Skipped (no name): {skipped}")
    logger.info(f"  Errors          : {errors}")
    logger.info(f"  Log             : {_LOG_FILE}")
    if not live:
        logger.info("  Re-run with --live to write changes to Sheets.")


if __name__ == "__main__":
    main()
