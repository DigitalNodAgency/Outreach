# FALLBACK_SOURCES.md — Source and Enrichment Reference
> Covers discovery source priority, enrichment tier logic, and retired source decisions.
> Mirrors protocols.md but focused on source-level detail only.

---

## Discovery Source Priority

1. **Vibe Prospecting MCP** (primary)
   - Pre-check credits: `mcp estimate-cost` before every run.
   - Credits = 0: log `vibe_prospecting_exhausted` to `pipeline_errors.jsonl`, fall through.
   - Export: PowerShell `Invoke-WebRequest` → `active/leads/vibe_export.csv`.
   - Source tag: `vibe_YYYY-MM-DD` on every record.
   - If results < 10: fall through to Prospeo to supplement (do not stop).

2. **Prospeo** (secondary / fallback)
   - `/search-person`: run per target region with ICP filters.
   - Source tag: `prospeo_YYYY-MM-DD`.
   - Zero results per region: log via `notify.alert_scrape_zero_results`, continue other regions.

3. **Manual assist** (last resort)
   - Rows may have blank emails — flag explicitly for enrichment.
   - Do not write to Sheets without email unless manually flagged.

---

## Enrichment Tiers (missing email only)

| Tier | Source           | Conditions                          | Timeout    |
|------|------------------|-------------------------------------|------------|
| T0   | Prospeo /enrich-person | Requires contact_name + company  | —          |
| T1   | Apify Contact Info Scraper | 100-URL chunks per actor run  | 5 min      |
| T2   | Serper snippet + contact-page scrape | Domain search        | 30s        |
| T3   | Auto-delete row  | No email found after T0–T2          | —          |

**Apify notes:**
- Actor: `apify/contact-info-scraper`
- Domain-matched emails only. Apply skip-list before passing URLs.
- Returns no person names — name column untouched by Apify.
- Source tag: `apify_contact_YYYY-MM-DD`.

**Serper notes:**
- Enrichment only. Never for discovery.
- Name extraction: only write if `_is_person_name()` passes (2–5 title-case words, letters only).
- Email local-part fallback: extract name from `john.smith@...` as last resort.
- Source tag: `serper_YYYY-MM-DD`.

---

## Retired Sources (never re-add)

| Source                  | Reason                                                        |
|-------------------------|---------------------------------------------------------------|
| Apify Places (Maps)     | Returns business listings, not people. ~3% email yield.       |
| Serper discovery        | Same issue — no verified personal emails at discovery stage.  |
| SerpAPI                 | Same issue.                                                   |
| Apify Leads Finder      | Same issue.                                                   |

Root cause: these sources return venue/listing data, not B2B contact data. Real-world yield after quality gate is ~3%. Not worth the credit spend.

---

## Source Health Tracking

- Stored in: `active/leads/source_health.json`
- Updated by: `pipeline_metrics.py → record_source_run()`
- Skip condition: source returned 0 leads in last 2 consecutive runs.
- Skip logged as warning to `pipeline_errors.jsonl`.
- Format:
  ```json
  {
    "vibe": {"last_5_runs": [12, 0, 0, 8, 15], "last_run_date": "2026-05-17"},
    "prospeo": {"last_5_runs": [22, 18, 0, 31, 20], "last_run_date": "2026-05-17"}
  }
  ```
