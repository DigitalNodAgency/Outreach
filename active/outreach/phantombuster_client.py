"""
phantombuster_client.py — PhantomBuster API v2 wrapper.
Handles launching phantoms, polling status, and retrieving output.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_PB_BASE = "https://api.phantombuster.com/api/v2"
_POLL_INTERVAL = 15   # seconds between status polls
_POLL_MAX_WAIT = 600  # 10 minutes before timeout


def _headers(api_key: str) -> dict:
    return {"X-Phantombuster-Key": api_key, "Content-Type": "application/json"}


def launch_phantom(api_key: str, phantom_id: str, input_data: list[dict], session_cookie: str = "") -> Optional[str]:
    """
    Launch a PhantomBuster phantom with the given lead input.
    Returns the container ID on success, None on failure.
    """
    url = f"{_PB_BASE}/agents/launch"
    argument: dict = {"leads": input_data}
    if session_cookie:
        argument["sessionCookie"] = session_cookie
    payload = {
        "id": phantom_id,
        "argument": argument,
    }
    try:
        resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=30)
        if not resp.ok:
            logger.error(f"[PB] Failed to launch phantom {phantom_id}: {resp.status_code} — {resp.text}")
            return None
        container_id = resp.json().get("containerId")
        logger.info(f"[PB] Launched phantom {phantom_id} — container: {container_id}")
        return container_id
    except requests.RequestException as e:
        logger.error(f"[PB] Failed to launch phantom {phantom_id}: {e}")
        return None


def get_phantom_status(api_key: str, container_id: str) -> dict:
    """
    Poll status of a running phantom container.
    Returns the status dict from PhantomBuster API.
    """
    url = f"{_PB_BASE}/containers/fetch"
    try:
        resp = requests.get(url, params={"id": container_id}, headers=_headers(api_key), timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error(f"[PB] Status poll failed for container {container_id}: {e}")
        return {}


def get_phantom_output(api_key: str, container_id: str) -> list[dict]:
    """
    Fetch output results from a completed phantom run.
    Returns list of result dicts (PhantomBuster result objects).
    """
    url = f"{_PB_BASE}/containers/fetch-result-object"
    try:
        resp = requests.get(url, params={"id": container_id}, headers=_headers(api_key), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("resultObject", [])
        if isinstance(results, list):
            return results
        return []
    except requests.RequestException as e:
        logger.error(f"[PB] Failed to fetch output for container {container_id}: {e}")
        return []


def wait_for_completion(api_key: str, container_id: str) -> bool:
    """
    Poll until phantom finishes or timeout is reached.
    Returns True if finished successfully, False on error or timeout.
    """
    elapsed = 0
    while elapsed < _POLL_MAX_WAIT:
        status_data = get_phantom_status(api_key, container_id)
        status = status_data.get("status", "")
        logger.info(f"[PB] Container {container_id} status: {status} ({elapsed}s elapsed)")

        if status == "finished":
            return True
        if status in ("error", "stopped", "killed"):
            logger.error(f"[PB] Phantom ended with status: {status}")
            return False

        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

    logger.error(f"[PB] Timeout waiting for container {container_id} after {_POLL_MAX_WAIT}s")
    return False
