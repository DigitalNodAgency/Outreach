"""
enrich_sheets_emails.py — Email enrichment orchestrator.
Targets leads with status=new AND empty email.
Tier 0: Prospeo /enrich-person
Tier 1: Prospeo /bulk-enrich-person (batch fallback)
Tier 2: Auto-delete row (no email found after all tiers)

NOTE: Apify and Serper removed from enrichment chain.
Both tools are slow, expensive per verified email, and clog the workflow.
Vibe and Prospeo deliver verified emails at discovery time — enrichment
is only needed for edge-case leads that slipped through without an email.
"""

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "outreach"))

from pipeline_metrics import log_pipeline_error
from prospeo_client import enrich_person, bulk_enrich_persons

logger = logging.getLogger(__name__)


def _source_tag() -> str:
    return f"prospeo_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def run_enrichment() -> dict:
    """
    Enrich leads with missing emails.
    Returns stats dict: prospeo_enriched, deleted, skipped.
    """
    from sheets_client import (
        get_leads_for_enrichment,
        update_lead_email,
        delete_lead_by_email,
    )

    stats = {"prospeo_enriched": 0, "deleted": 0, "skipped": 0}
    tag = _source_tag()

    leads = get_leads_for_enrichment()
    if not leads:
        logger.info("[ENRICH] No leads require enrichment.")
        return stats

    logger.info(f"[ENRICH] {len(leads)} leads need email enrichment.")

    # Separate leads with and without contact names
    named_leads = [l for l in leads if l.get("name", "").strip()]
    unnamed_leads = [l for l in leads if not l.get("name", "").strip()]

    # Tier 0 — Prospeo /enrich-person (single, verified, requires name)
    remaining = []
    for lead in named_leads:
        email = enrich_person(
            contact_name=lead["name"],
            company_name=lead.get("company", ""),
        )
        if email:
            update_lead_email(lead["email"], email)
            stats["prospeo_enriched"] += 1
            logger.info(f"[ENRICH] Tier 0 enriched: {lead['name']} @ {lead.get('company')}")
        else:
            remaining.append(lead)

    remaining.extend(unnamed_leads)

    # Tier 1 — Prospeo /bulk-enrich-person (batch for remaining named leads)
    bulk_candidates = [
        {"full_name": l["name"], "company": l.get("company", ""), "_lead": l}
        for l in remaining if l.get("name", "").strip()
    ]

    if bulk_candidates:
        enriched = bulk_enrich_persons([
            {"full_name": c["full_name"], "company": c["company"]}
            for c in bulk_candidates
        ])
        email_map = {
            e.get("full_name", "").lower(): e.get("email", "")
            for e in enriched if e.get("email")
        }
        still_remaining = []
        for candidate in bulk_candidates:
            key = candidate["full_name"].lower()
            if key in email_map:
                update_lead_email(candidate["_lead"]["email"], email_map[key])
                stats["prospeo_enriched"] += 1
                logger.info(f"[ENRICH] Tier 1 bulk enriched: {candidate['full_name']}")
            else:
                still_remaining.append(candidate["_lead"])
        remaining = still_remaining + [l for l in remaining if not l.get("name", "").strip()]

    # Tier 2 — Auto-delete (no email found after all tiers)
    for lead in remaining:
        identifier = lead.get("email") or lead.get("name") or "unknown"
        log_pipeline_error(
            stage="enrichment",
            message=f"No email found after all enrichment tiers. Auto-deleting: {identifier}",
        )
        delete_lead_by_email(lead.get("email", ""))
        stats["deleted"] += 1
        logger.info(f"[ENRICH] Auto-deleted (no email): {identifier}")

    logger.info(
        f"[ENRICH] Done. Prospeo enriched: {stats['prospeo_enriched']}, "
        f"Deleted: {stats['deleted']}, Skipped: {stats['skipped']}"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_enrichment()
    print(result)
