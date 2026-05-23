"""
run_vibe_api_discovery.py — Direct Vibe Prospecting HTTP MCP client.
Calls vibeprospecting.explorium.ai/mcp via JSON-RPC 2.0 using VIBE_PROSPECTING_API_KEY.
Runs on GitHub Actions without requiring a Claude Code MCP session.
"""

import csv
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from pipeline_metrics import log_pipeline_error, record_source_run, record_run_stats

logger = logging.getLogger(__name__)

MCP_URL = "https://vibeprospecting.explorium.ai/mcp"
REQUEST_TIMEOUT = 120
SOURCE_NAME = "vibe_api"


class _VibeMCPClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._mcp_session_id: str | None = None
        self._call_id = 0

    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._key}",
            "X-API-Key": self._key,
        }
        if self._mcp_session_id:
            h["Mcp-Session-Id"] = self._mcp_session_id
        return h

    @staticmethod
    def _parse_sse(text: str) -> dict | None:
        """Extract the last JSON-RPC result from an SSE stream."""
        last = None
        for line in text.splitlines():
            if line.startswith("data: "):
                data_str = line[6:].strip()
                if data_str in ("[DONE]", ""):
                    continue
                try:
                    last = json.loads(data_str)
                except json.JSONDecodeError:
                    pass
        return last

    def _post(self, body: dict) -> dict | None:
        try:
            resp = requests.post(MCP_URL, json=body, headers=self._headers(), timeout=REQUEST_TIMEOUT)
            if "Mcp-Session-Id" in resp.headers:
                self._mcp_session_id = resp.headers["Mcp-Session-Id"]
            if not resp.ok:
                logger.error(
                    f"[VIBE API] HTTP {resp.status_code} {resp.reason} | "
                    f"Content-Type: {resp.headers.get('Content-Type', '?')} | "
                    f"Body: {resp.text[:600]}"
                )
                if resp.status_code in (401, 403):
                    return None
                resp.raise_for_status()
            if not resp.content.strip():
                return {}
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                logger.debug(f"[VIBE API] SSE body: {resp.text[:500]}")
                return self._parse_sse(resp.text)
            return resp.json()
        except requests.exceptions.Timeout:
            logger.error("[VIBE API] Request timed out.")
            return None
        except requests.exceptions.HTTPError:
            return None
        except Exception as e:
            logger.error(f"[VIBE API] Request error: {e}")
            return None

    def initialize(self) -> bool:
        body = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "phase1-discovery", "version": "1.0"},
            },
            "id": self._next_id(),
        }
        result = self._post(body)
        if result is None:
            return False
        if "error" in result:
            logger.error(f"[VIBE API] Initialize error: {result['error']}")
            return False
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        logger.info("[VIBE API] MCP session initialized.")
        return True

    def call_tool(self, tool_name: str, arguments: dict) -> dict | None:
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
            "id": self._next_id(),
        }
        logger.debug(f"[VIBE API] Calling tool: {tool_name}")
        result = self._post(body)
        if result is None:
            return None
        if "error" in result:
            logger.error(f"[VIBE API] Tool {tool_name} error: {result['error']}")
            return None

        content = result.get("result", {}).get("content", [])
        logger.debug(f"[VIBE API] {tool_name} raw content: {str(content)[:500]}")

        for item in content:
            if item.get("type") == "text":
                text = item["text"]
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    logger.debug(f"[VIBE API] {tool_name} non-JSON text: {text[:500]}")
                    return {"_raw": text}

        return result.get("result")


def _source_tag() -> str:
    return f"vibe_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def run_vibe_api_discovery(target: int = 100) -> dict:
    """
    Discover leads via Vibe Prospecting HTTP MCP API and write to Sheets.
    Returns stats dict: new_leads, dupes_skipped, failed, source.
    """
    from ingest_vibe_export import _normalize_record, _deduplicate
    from sheets_client import get_existing_emails, append_leads_batch

    stats = {"new_leads": 0, "dupes_skipped": 0, "failed": 0, "source": SOURCE_NAME}

    api_key = os.getenv("VIBE_PROSPECTING_API_KEY", "").strip()
    if not api_key:
        log_pipeline_error(SOURCE_NAME, "VIBE_PROSPECTING_API_KEY not set.")
        return stats

    client = _VibeMCPClient(api_key)

    if not client.initialize():
        log_pipeline_error(SOURCE_NAME, "MCP initialization failed.")
        record_source_run(SOURCE_NAME, 0)
        return stats

    vibe_session = f"session_{int(time.time())}"

    # Step 1: fetch prospects matching ICP filters
    fetch_result = client.call_tool("fetch-entities", {
        "entity_type": "prospects",
        "filters": {
            "company_region_country_code": {"values": ["US-FL"]},
            "job_level": {"values": ["c-suite", "owner", "director", "founder", "president"]},
            "website_keywords": {"values": ["HVAC"]},
            "company_size": {"values": ["1-10", "11-50", "51-200"]},
            "has_email": True,
        },
        "number_of_results": target,
        "session_id": vibe_session,
        "tool_reasoning": "Find HVAC company owners/CEOs/founders in Florida for outreach",
    })

    if not fetch_result:
        log_pipeline_error(SOURCE_NAME, "fetch-entities returned no result.")
        record_source_run(SOURCE_NAME, 0)
        return stats

    table_name = fetch_result.get("table_name")
    session_id = fetch_result.get("session_id", vibe_session)

    if not table_name:
        logger.error(f"[VIBE API] fetch-entities missing table_name: {str(fetch_result)[:400]}")
        log_pipeline_error(SOURCE_NAME, "fetch-entities missing table_name")
        record_source_run(SOURCE_NAME, 0)
        return stats

    logger.info(f"[VIBE API] fetch-entities OK. table_name={table_name}")

    # Step 2: enrich to add email values
    enrich_result = client.call_tool("enrich-prospects", {
        "table_name": table_name,
        "session_id": session_id,
        "enrichments": ["enrich-prospects-contacts"],
        "parameters": {"contact_types": ["email"]},
        "sample_size": 5,
    })

    enriched_table = table_name
    if enrich_result:
        enriched_table = enrich_result.get("table_name", table_name)
        logger.info(f"[VIBE API] enrich-prospects OK. enriched_table={enriched_table}")
    else:
        logger.warning("[VIBE API] enrich-prospects failed — using fetch table (may lack emails).")

    # Step 3: export to CSV
    export_result = client.call_tool("export-to-csv", {
        "table_name": enriched_table,
        "session_id": session_id,
        "dataset_name": "hvac_florida_leads",
        "tool_reasoning": "Export HVAC Florida leads for Phase 1 discovery",
    })

    if not export_result:
        log_pipeline_error(SOURCE_NAME, "export-to-csv returned no result.")
        record_source_run(SOURCE_NAME, 0)
        return stats

    download_url = (
        export_result.get("download_url")
        or export_result.get("url")
        or export_result.get("csv_url")
    )
    row_count = export_result.get("row_count", "?")

    if not download_url:
        logger.error(f"[VIBE API] export-to-csv missing download_url: {str(export_result)[:400]}")
        log_pipeline_error(SOURCE_NAME, "export-to-csv missing download_url")
        record_source_run(SOURCE_NAME, 0)
        return stats

    logger.info(f"[VIBE API] export-to-csv OK. row_count={row_count}")
    logger.info(f"[VIBE API] download_url: {download_url}")
    logger.debug(f"[VIBE API] Full export_result: {export_result}")

    # Step 4: download CSV
    # Try plain GET first — pre-signed URLs (S3/GCS) reject or misbehave with auth headers.
    # Fall back to auth headers if plain GET returns HTML.
    csv_resp = None
    for attempt, req_headers in enumerate([
        {},
        {"Authorization": f"Bearer {api_key}", "X-API-Key": api_key},
    ]):
        try:
            csv_resp = requests.get(download_url, headers=req_headers, timeout=120)
        except Exception as e:
            logger.error(f"[VIBE API] CSV download request error (attempt {attempt + 1}): {e}")
            log_pipeline_error(SOURCE_NAME, f"CSV download failed: {e}")
            record_source_run(SOURCE_NAME, 0)
            return stats

        content_type = csv_resp.headers.get("Content-Type", "")
        logger.info(f"[VIBE API] Download attempt {attempt + 1}: status={csv_resp.status_code} content-type={content_type}")

        if csv_resp.ok and "text/html" not in content_type:
            break

        if "text/html" in content_type:
            logger.error(
                f"[VIBE API] Download returned HTML (attempt {attempt + 1}). "
                f"URL={download_url} | First 300 chars: {csv_resp.text[:300]}"
            )
            if attempt == 1:
                log_pipeline_error(SOURCE_NAME, f"CSV download returned HTML on both attempts. URL={download_url}")
                record_source_run(SOURCE_NAME, 0)
                return stats

    if not csv_resp.ok:
        logger.error(f"[VIBE API] CSV download HTTP {csv_resp.status_code}: {csv_resp.text[:300]}")
        log_pipeline_error(SOURCE_NAME, f"CSV download HTTP {csv_resp.status_code}")
        record_source_run(SOURCE_NAME, 0)
        return stats

    # Step 5: parse + normalize
    source_tag = _source_tag()
    raw_rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
    logger.info(f"[VIBE API] Downloaded {len(raw_rows)} CSV rows.")
    if raw_rows:
        logger.info(f"[VIBE API] CSV columns: {list(raw_rows[0].keys())}")
        logger.info(f"[VIBE API] First row sample: {dict(list(raw_rows[0].items())[:8])}")

    normalized = []
    failed = 0
    for raw in raw_rows:
        record = _normalize_record(raw, source_tag)
        if record:
            normalized.append(record)
        else:
            failed += 1

    stats["failed"] = failed

    if not normalized:
        logger.warning("[VIBE API] No valid leads after normalization.")
        record_source_run(SOURCE_NAME, 0)
        return stats

    # Step 6: dedup + write to Sheets
    existing_emails = get_existing_emails()
    clean, dupe_count = _deduplicate(normalized, existing_emails)
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
    result = run_vibe_api_discovery()
    print(result)
