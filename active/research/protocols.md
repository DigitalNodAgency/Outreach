# tasks/protocols.md — Technical Protocols
> Detailed rules for API usage, token optimization, error handling, and context management.
> Routed from CLAUDE.md to keep the main file lean.

---

## Output Token Rules

- Default to concise. 3 lines if 3 lines work.
- No preamble. Never restate the task before doing it.
- No postamble. Never summarize what was just done unless asked.
- No filler: "Certainly!", "Great question", "Of course", "Sure thing."
- Code blocks only for actual code. Not for paths, commands, short outputs.
- Multi-step tasks: complete all steps first, present results together.
- Verbosity allowed only when: user asks for walkthrough, decision has non-obvious tradeoffs,
  or writing docs for others to read.

---

## API Call Rules

**Prompt efficiency:**
- System prompts: concise, non-redundant. No repeating context from user message.
- Send only recent relevant turns, not full conversation history.
- If 1 API call does the job, never use 2.
- Batch where API supports it.

**Model selection:**
- claude-haiku-4-5: classification, routing, simple extraction, short Q&A
- claude-sonnet-4-6: complex reasoning, multi-step, code generation, agents
- claude-opus-4-6: maximum capability tasks only, cost justified
- Always justify non-obvious model choice in a comment.

**Token control:**
- Always set explicit max_tokens. Never leave unlimited.
- Structured outputs: use JSON mode or tool use, not free-text parsing.
- Repeated calls: cache system prompts using Anthropic cache_control.
- Stream when user needs progressive output. Do not stream when full response needed first.

**Context window:**
- Never dump entire files into context. Summarize or chunk first.
- Agent loops: keep system prompt + last 3-5 turns + current task only.
- Structuring batches: 10 lead objects max per Claude Code pass. Hard limit.
- Context near limit: summarize earlier turns into compressed memory block.

---

## Google Sheets API Protocol

**Quota rules:**
- Batch all writes via append_rows() single API call. Per-row fallback only on batch failure.
- Read quota: fetch only the fields needed. Never pull full row objects for dedup. Use get_existing_emails() flat array only.
- Retry on 429: exponential backoff, 1s base, max 3 retries, then log and skip.

**Column mapping:**
- Leads tab: name, email, company, region, warmth_score, status, last_contacted, followup_count, notes
- outreach_log tab: lead_email, lead_name, sequence_type, stage_number, email_subject, sent_date, status
- Outreach Reply Log tab: populated by reply_logger.py, read-only from pipeline perspective

**Idempotency:**
- All Sheets writes must be safe to re-run. Check before write, not after.
- outreach_log: always check (email, stage_number) cache before append. Never write duplicates.

---

## Brevo SMTP + API Protocol

**Daily cap:** [CLIENT_DAILY_CAP] sends. Hard stop when hit. Never exceed.
**Delay between sends:** 5 seconds minimum.
**Health halt trigger:** >50% failure rate after 5+ sends in session. Send alert email, exit code 1.
**Reconciliation:** brevo_reconcile.py runs after every Phase 2 execution (local and Actions). Always. Idempotent.
**Pre-sync:** Run brevo_reconcile.py before outreach only if Sheets count < Brevo count. Prevents out-of-sync overwrites.

---

## Vibe Prospecting MCP Protocol

**Pre-check credits** before every run: mcp estimate-cost call.
- Credits = 0: log vibe_prospecting_exhausted to pipeline_errors.jsonl, fall through to Prospeo.
- Credits > 0: run fetch-entities or fetch-businesses-events, then export-to-csv.
- Results < 10: fall through to Prospeo to supplement. Do not stop.

**Download:** PowerShell Invoke-WebRequest to active/leads/vibe_export.csv. Not curl, not WebFetch.
**Source tag:** vibe_YYYY-MM-DD on every record.

---

## Prospeo API Protocol

**Discovery (/search-person):** Run per target region. ICP filters applied automatically (see CLAUDE.md ICP section).
**Enrichment (/enrich-person):** Requires contact_name + company_name. Verified emails only. Skip leads with no contact_name.
**Bulk enrichment (/bulk-enrich-person):** Use for enrichment batches, not for discovery.
**Source tag:** prospeo_YYYY-MM-DD on every record.
**Zero results per region:** Log via notify.alert_scrape_zero_results("prospeo_discovery"), continue other regions.

---

## Apify Protocol

**Actor:** apify/contact-info-scraper
**Batch size:** 100 URLs per actor run.
**Timeout:** 5 minutes per run.
**Domain-matched emails only.** Apply skip-list before passing URLs.
**Returns:** no person names. Name column untouched by Apify.
**Source tag:** apify_contact_YYYY-MM-DD on every record.

---

## Serper Protocol

**Use case:** Tier 2 enrichment only. Not for discovery.
**Timeout:** 30 seconds per contact-page scrape.
**Name extraction:** Only write if _is_person_name() passes: 2-5 title-case words, letters only.
**Email local part fallback:** Extract name from email (e.g. john.smith@...) as last resort.
**Source tag:** serper_YYYY-MM-DD on every record.

---

## Error Handling

**API errors:**
- 429 / 529 / 503: exponential backoff, start 1s, max 3 retries.
- 401 / 403: fail immediately, surface to user. Never retry auth errors silently.
- Timeout: 30s for sync calls, 120s for streaming.
- Unexpected schema: validate before using. Never assume shape is correct.

**Pipeline errors:**
- All stage-level errors log to active/leads/pipeline_errors.jsonl.
- Enrichment failures: log and continue. Never halt pipeline for a single failed record.
- Scrape zero results: log via notify.alert_scrape_zero_results, continue remaining sources.
- Quality gate failures: log to active/leads/failed_records.jsonl, continue with remaining batch.

**Agent and loop safety:**
- Structuring loop: hard cap at 10 objects per pass. Never exceed.
- Stuck detection: same source returning 0 in 2 consecutive runs triggers skip + warning.
- Partial failure: preserve completed steps. Never restart from zero without operator confirm.

**Reporting:**
- Surface in every summary email: what failed, why if known, what was attempted.
- Never swallow errors silently.
- Never fabricate results when a call fails. Zero results is preferable to invented data.

---

## Environment Reference

**Phase 1 — local .env:**
```
PROSPEO_API_KEY                Prospeo discovery + enrichment
APIFY_API_TOKEN                Apify Contact Info Scraper (Tier 1 enrichment)
SERPER_API_KEY                 Serper fallback (enrichment only)
MAX_LEADS_PER_RUN              Discovery cap per run
```

**Phase 2 — GitHub repo secrets:**
```
GOOGLE_SERVICE_ACCOUNT_JSON    Full JSON content of service account file
SPREADSHEET_ID                 Google Sheet ID
SMTP_USER                      Brevo sender email
SMTP_PASS                      Brevo SMTP password
BREVO_API_KEY                  Brevo API key
GMAIL_SENDER                   Gmail address for operator notifications
GMAIL_APP_PASSWORD             Gmail app password (not account password)
NOTIFY_EMAIL                   Where summary emails go
```

**Phase 2 — GitHub repo variables (not secrets):**
```
FOLLOWUP_DELAY_DAYS            Days between touches
MAX_FOLLOWUPS                  Max touches per lead
GAMMA_URL                      Landing page URL injected into templates
```

---

## File Reference

**Phase 1:**
```
active/execution/ingest_vibe_export.py      → Vibe CSV → structure → dedup → write
active/execution/run_prospeo_discovery.py   → multi-region Prospeo batch runner
active/execution/prospeo_client.py          → Prospeo API client
active/execution/enrich_sheets_emails.py    → 3-tier email enrichment orchestrator
active/execution/apify_client.py            → Apify Contact Info Scraper (Tier 1)
active/execution/pipeline_metrics.py        → source health tracking + run stats
active/execution/notify.py                  → summary + alert emails to operator
active/leads/vibe_export.csv               → raw Vibe export (PowerShell download)
active/leads/run_metrics.tsv               → per-run stats TSV
active/leads/source_health.json            → last-5-run health per source
active/leads/failed_records.jsonl          → quality gate rejects
active/leads/pipeline_errors.jsonl         → stage-level errors
```

**Phase 2:**
```
.github/workflows/phase2-outreach.yml      → scheduled outreach (daily + manual)
.github/workflows/reply-logger.yml         → scheduled reply poller (1hr after outreach)
active/outreach/main.py                    → entry point, orchestrates run + cleanup
active/outreach/outreach_engine.py         → initial + follow-up send logic
active/outreach/config.py                  → all settings and column mappings
active/outreach/sheets_client.py           → Google Sheets read/write
active/outreach/smtp_client.py             → Brevo SMTP + daily cap + health check
active/outreach/brevo_reconcile.py         → post-run Brevo sync + outreach_log dedup
active/outreach/reply_logger.py            → IMAP polling + reply logging
active/outreach/brevo_tagger.py            → tags Brevo contacts by stage/geo/engagement
active/outreach/sequence_manager.py        → 3-touch sequence orchestrator
active/execution/template_metrics.py       → TSV metrics per series per run
```

---

## Pipeline State Flags

**Pause Phase 1:**
Create PIPELINE_PAUSED file at project root. Windows Task Scheduler will not run.
Resume: delete PIPELINE_PAUSED, then run:
  powershell Enable-ScheduledTask -TaskName 'LeadManagerPipeline'

**Pause Phase 2:**
Disable phase2-outreach.yml from the GitHub Actions tab.
Manual trigger: GitHub Actions tab → phase2-outreach.yml → Run workflow.

**Workflow timeouts:**
- phase2-outreach.yml: 90 minutes
- reply-logger.yml: 15 minutes

---
