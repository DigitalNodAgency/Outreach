"""
billionverify_client.py — Thin client for the BillionVerify email-verification API.

Used by verify_emails_step.py (Phase 1) to validate emails before outreach.
Auth: BV-API-KEY header. Key is passed in by the caller (loaded from BV_API_KEY).
The key is NEVER logged or printed. Base URL + endpoints per the BillionVerify v1
spec (see VERIFY_EMAILS.md at repo root).

Status → decision (per spec):
  valid / catchall / role           → KEEP  (catchall + role are flagged)
  invalid / risky / disposable / unknown → REMOVE
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.billionverify.com/v1"
_TIMEOUT = 60
_BULK_CHUNK = 100            # /verify/bulk hard cap per call
_RETRYABLE = (429, 500, 502, 503)
_MAX_RETRIES = 3

KEEP_STATUSES = {"valid", "catchall", "role"}
REMOVE_STATUSES = {"invalid", "risky", "disposable", "unknown"}


class BillionVerifyError(Exception):
    """Generic BillionVerify failure."""


class BillionVerifyAuthError(BillionVerifyError):
    """401 — key missing/invalid. Never retried."""


class BillionVerifyCreditsError(BillionVerifyError):
    """402 — insufficient credits. Never retried."""


class BillionVerifyClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise BillionVerifyAuthError("BV_API_KEY is missing.")
        # Key lives only in the header dict — never logged.
        self._headers = {"BV-API-KEY": api_key, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{BASE_URL}{path}"
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.request(method, url, headers=self._headers, timeout=_TIMEOUT, **kwargs)
            except requests.RequestException as e:
                if attempt < _MAX_RETRIES:
                    logger.warning(f"[BV] network error ({e}); retry in 5s")
                    time.sleep(5)
                    continue
                raise BillionVerifyError(f"network error: {e}") from e

            if resp.status_code == 401:
                raise BillionVerifyAuthError(
                    "BV_API_KEY is invalid or missing (401). Check your .env / repo secret."
                )
            if resp.status_code == 402:
                raise BillionVerifyCreditsError(
                    "Insufficient BillionVerify credits (402). Top up at billionverify.com."
                )
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = 10 * (2 ** attempt)   # 10s, 20s, 40s per spec
                logger.warning(f"[BV] 429 rate limited; backoff {wait}s (retry {attempt + 1}/{_MAX_RETRIES})")
                time.sleep(wait)
                continue
            if resp.status_code in _RETRYABLE and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise BillionVerifyError(f"{resp.status_code}: {resp.text[:300]}")
            return resp
        raise BillionVerifyError("exhausted retries")

    def get_credits(self):
        """Return available credit balance as int, or None if the field is not found.
        Defensive: the spec does not pin the exact response key."""
        data = self._request("GET", "/credits").json()
        candidates = ("credits", "balance", "available", "remaining")
        for src in (data, data.get("data") or {}):
            if not isinstance(src, dict):
                continue
            for key in candidates:
                if isinstance(src.get(key), (int, float)):
                    return int(src[key])
        return None

    def verify_bulk(self, emails) -> dict:
        """Verify emails via POST /verify/bulk, chunked to 100/call.
        Returns {email_lower: {"status": str, "score": str}}."""
        results: dict[str, dict] = {}
        unique = list(dict.fromkeys(e.strip() for e in emails if e and e.strip()))
        for i in range(0, len(unique), _BULK_CHUNK):
            chunk = unique[i:i + _BULK_CHUNK]
            payload = self._request(
                "POST", "/verify/bulk", json={"emails": chunk, "check_smtp": False}
            ).json()
            rows = (payload.get("data") or {}).get("results") or payload.get("results") or []
            for r in rows:
                email = (r.get("email") or "").strip().lower()
                if not email:
                    continue
                results[email] = {
                    "status": (r.get("status") or "unknown").strip().lower(),
                    "score": str(r.get("score") if r.get("score") is not None else r.get("reason") or ""),
                }
            time.sleep(0.1)
        return results
