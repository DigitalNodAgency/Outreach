"""
pipeline_metrics.py — Source health tracking and run statistics.
Reads/writes source_health.json and run_metrics.tsv.
"""

import json
import logging
import os
from datetime import datetime, timezone

from config import SOURCE_HEALTH_JSON, RUN_METRICS_TSV, PIPELINE_ERRORS_JSONL

logger = logging.getLogger(__name__)

HEALTH_WINDOW = 2  # consecutive zero-result runs before a source is skipped


def _load_health() -> dict:
    if not os.path.exists(SOURCE_HEALTH_JSON):
        return {}
    try:
        with open(SOURCE_HEALTH_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_health(data: dict) -> None:
    os.makedirs(os.path.dirname(SOURCE_HEALTH_JSON), exist_ok=True)
    with open(SOURCE_HEALTH_JSON, "w") as f:
        json.dump(data, f, indent=2)


def should_skip_source(source_name: str) -> bool:
    """
    Returns True if source should be skipped.
    Skip condition: leads_returned == 0 in last HEALTH_WINDOW consecutive entries.
    """
    health = _load_health()
    entries = health.get(source_name, [])
    if len(entries) < HEALTH_WINDOW:
        return False
    recent = entries[-HEALTH_WINDOW:]
    skip = all(e.get("leads_returned", 1) == 0 for e in recent)
    if skip:
        log_pipeline_error(
            stage="source_health_check",
            message=f"Skipping source '{source_name}': {HEALTH_WINDOW} consecutive zero-result runs.",
            source=source_name,
        )
        logger.warning(f"[METRICS] Skipping source: {source_name}")
    return skip


def record_source_run(source_name: str, leads_returned: int) -> None:
    """
    Append a run entry for a source to source_health.json.
    Keeps last 5 entries per source.
    """
    health = _load_health()
    if source_name not in health:
        health[source_name] = []

    health[source_name].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "leads_returned": leads_returned,
    })
    health[source_name] = health[source_name][-5:]  # keep last 5
    _save_health(health)
    logger.info(f"[METRICS] Recorded: {source_name} → {leads_returned} leads")


def record_run_stats(
    source: str,
    leads_written: int,
    has_email_pct: float,
    dedup_skipped: int,
) -> None:
    """Append a row to run_metrics.tsv."""
    os.makedirs(os.path.dirname(RUN_METRICS_TSV), exist_ok=True)
    write_header = not os.path.exists(RUN_METRICS_TSV)

    with open(RUN_METRICS_TSV, "a") as f:
        if write_header:
            f.write("timestamp\tsource\tleads_written\thas_email_pct\tdedup_skipped\n")
        now = datetime.now(timezone.utc).isoformat()
        f.write(f"{now}\t{source}\t{leads_written}\t{has_email_pct:.2f}\t{dedup_skipped}\n")


def log_pipeline_error(stage: str, message: str, source: str = "") -> None:
    """Append a structured error entry to pipeline_errors.jsonl."""
    os.makedirs(os.path.dirname(PIPELINE_ERRORS_JSONL), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "source": source,
        "message": message,
    }
    with open(PIPELINE_ERRORS_JSONL, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_pipeline_errors(limit: int = 50) -> list[dict]:
    """Read last N pipeline errors for summary email."""
    if not os.path.exists(PIPELINE_ERRORS_JSONL):
        return []
    errors = []
    with open(PIPELINE_ERRORS_JSONL, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    errors.append(json.loads(line))
                except Exception:
                    pass
    return errors[-limit:]


def log_failed_record(record: dict, reason: str) -> None:
    """Log a structuring/quality gate reject to failed_records.jsonl."""
    from config import FAILED_RECORDS_JSONL
    os.makedirs(os.path.dirname(FAILED_RECORDS_JSONL), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "record": record,
    }
    with open(FAILED_RECORDS_JSONL, "a") as f:
        f.write(json.dumps(entry) + "\n")
