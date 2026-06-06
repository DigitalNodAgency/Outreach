# CLAUDE.md — Lead Manager Pipeline
> Auto-managed. Propose updates whenever patterns, errors, or inefficiencies are observed.
> Under 300 lines. Satellite files live in active/research/.

---

## 1. Project Identity

**What it does:** Two-phase autonomous B2B lead pipeline. Phase 1 handles discovery, structuring, deduplication, and enrichment on a local Windows schedule. Phase 2 handles outreach sequencing, follow-up automation, and Brevo reconciliation via GitHub Actions.
**Success metric:** Qualified leads with verified emails reaching `status=outreach_sent` within 24 hours of discovery, with zero duplicate sends and accurate Brevo reconciliation on every run.
**Stack:** Python 3.11, Google Sheets API, Brevo SMTP + API, GitHub Actions, Windows Task Scheduler, Vibe Prospecting MCP, Prospeo API, Apify Contact Info Scraper, Serper API, Gmail SMTP, PhantomBuster (Facebook + LinkedIn social outreach)
**Type:** Freelance Deliverable / AI Agent

---

## 2. Pipeline Architecture

### Phase 1 — Discovery and Prep (Local, Windows Task Scheduler)
Entry point: `run_phase1.bat` → `active/execution/phase1_runner.py`

```
Trigger (run_phase1.bat)
  → Pause flag check (PIPELINE_PAUSED at root)
  → Source health check (skip dead sources via source_health.json)
  → Vibe MCP export → ingest_vibe_export.py → structure → dedup → Sheets write
  → Prospeo fallback if Vibe < 10 results → run_prospeo_discovery.py
  → Email enrichment: Prospeo (T0) → Apify (T1) → Serper (T2) → auto-delete (T3)
  → Follow-up staging (status/count advance only, no emails sent)
  → Phase 1 summary email (notify.py → Gmail SMTP)
```

### Phase 2 — Outreach Engine (GitHub Actions, daily 10:00 UTC)
Entry point: `.github/workflows/phase2-outreach.yml` → `active/outreach/main.py`

```
Trigger (GitHub Actions)
  → Brevo pre-sync if Sheets < Brevo count
  → Touch 1: status=new → outreach_sent
  → Touch 2/3: status=outreach_sent|followup_sent + delay elapsed → followup_sent
  → Cleanup: dedup outreach_log, record template metrics
  → Phase 2 summary email → Brevo post-sync (always)
  → Upload logs as GitHub artifact (30-day retention)

Reply Logger (11:00 UTC, 1h after outreach):
  → IMAP poll → log inbound replies → Outreach Reply Log sheet
```

### Architectural Decisions (numbered, append only)
1. Sheets batch write: `append_rows()` single call per run. Per-row fallback on batch failure only.
2. Dedup primary key: email (lowercase). Secondary: domain fuzzy match >85%.
3. No Anthropic API calls from Python. All AI structuring runs in Claude Code session only.
4. Phase 2 runs from repo root: `python active/outreach/main.py`. No working-directory override.
5. All file paths in config.py are absolute via `_ROOT = Path(__file__).parents[2]`. No CWD dependency.
6. Logs write to `logs/` at repo root. GitHub Actions uploads from `logs/phase2_run.log`.

---

## 3. Schema Reference
> Full definitions: [active/research/SCHEMA.md](active/research/SCHEMA.md)

**Leads tab primary key:** `email` (lowercase, deduplicated)
**outreach_log dedup key:** `(lead_email, stage_number)` — never write duplicates
**Status values:** `new → outreach_sent → followup_sent → replied | closed | failed`

---

## 4. Deduplication Rules

- Fetch existing emails via `get_existing_emails()` before every Sheets write. Never trust in-memory state alone.
- Primary: exact email match (lowercase). Secondary: company domain fuzzy match >85% (`rapidfuzz`).
- One contact per domain — keep most senior role: founder > CEO > owner > director > other.
- Log every skipped duplicate with reason to `pipeline_errors.jsonl`. Never silently drop.

---

## 5. Agent Design Rules

**Phase 1 agent (phase1_runner.py):**
- Hard cap: 100 leads per run (`MAX_LEADS_PER_RUN`). Never exceed.
- Structuring batches: 10 objects max per Claude Code pass.
- Never write a lead without a verified email (exception: manual assist rows flagged for enrichment).
- Never overwrite discovery-sourced contact names. Only Serper-extracted names written if `_is_person_name()` passes.

**Phase 2 agent (main.py):**
- Never send Touch 2/3 unless `last_contacted` is at least `FOLLOWUP_DELAY_DAYS` ago.
- Never send to `status=replied` or `status=closed`.
- Check outreach_log cache before every `append_outreach_log()`. No duplicate `(email, stage_number)` pairs.
- SMTP health halt: >50% failure rate after 5+ sends. Exit code 1, alert email sent.

**Reply logger (reply_logger.py):**
- IMAP poll only. Never sends email.
- Runs 1h after outreach (11:00 UTC). Logs to Outreach Reply Log sheet.

---

## 6. Self-Improvement Protocol

After every meaningful task, run silently:
- Did this take more steps than needed?
- Were there repeated errors or retries?
- Was output more verbose than necessary?
- Did any API call fail or behave unexpectedly?
- Was the output what the user needed vs. what they literally asked?

If 2+ triggers: propose a concrete CLAUDE.md or lessons.md update before closing.
Never propose vague improvements. Propose exact rule additions.
Lessons log: [active/research/lessons.md](active/research/lessons.md)

---

## 7. Output Token Optimization

- Default to concise. 3 lines if 3 lines work.
- No preamble. Never restate the task before doing it.
- No postamble. Never summarize what was just done unless asked.
- No filler phrases ("Certainly!", "Great question", "Sure thing").
- Multi-step tasks: complete all steps first, present results together.
- Verbosity allowed only for: walkthroughs, non-obvious tradeoff decisions, docs for others.

---

## 8. API Call Optimization

- If 1 API call does the job, never use 2. Batch where API supports it.
- Sheets: always `append_rows()` single call. Never write rows one at a time in a loop.
- Prospeo: 200ms delay between requests. Exponential backoff on 429 (1s base, 3 retries max).
- Apify: 100-URL chunks, 5-min timeout per run.
- Serper: 30s timeout per scrape.
- Brevo: 5s delay between sends. Hard daily cap enforced in `smtp_client.py`.
- Full API protocols: [active/research/protocols.md](active/research/protocols.md)

---

## 9. Error Handling

- 429 / 529 / 503: exponential backoff, 1s base, 3 retries, then log and skip.
- 401 / 403: fail immediately, surface to user. Never retry auth errors silently.
- All stage-level errors → `active/leads/pipeline_errors.jsonl`
  Format: `{"timestamp": "ISO8601", "stage": "...", "record_id": "...", "error_message": "..."}`
- Quality gate failures → `active/leads/failed_records.jsonl`
- Never halt pipeline on a single record failure. Log and continue.
- Never fabricate results. Zero results is preferable to invented data.
- Always surface errors in summary emails.

---

## 10. Project State and Persistent Decisions

- **Last milestone:** LinkedIn URL backfill script ready + PhantomBuster timezone resolved (v5 | 2026-06-02)
- **Current focus:** Waiting on Mohit to reconnect PhantomBuster session cookie. Florida leads exhausted — all sequences sent. ICP expansion to TX/GA/NC/TN recommended and communicated to client. Mohit currently analysing data (2026-06-06).
- **Pending decisions:** ICP expansion to TX/GA/NC/TN (recommended, awaiting approval). GitHub Actions vars FOLLOWUP_DELAY_DAYS=3 and MAX_LEADS_PER_RUN=30 still need manual update in repo settings.
- **Known issues:** PhantomBuster session cookie expired (Mohit must reconnect). "No slots left (4/1)" confirmed not a blocker — max slots already filled.
- **Locked choices:** No Apify Places, Serper discovery, SerpAPI, Apify Leads Finder (retired — ~3% email yield)

---

## 11. Environment

> All credentials are client-owned. Never substitute operator keys. Await client onboarding before filling any credential field.

### Phase 1 — local `.env` (never committed)
```
PROSPEO_API_KEY          Client's Prospeo key — discovery + enrichment
APIFY_API_TOKEN          Client's Apify token — Contact Info Scraper (Tier 1)
SERPER_API_KEY           Client's Serper key — fallback scrape (Tier 2)
VIBE_PROSPECTING_API_KEY Client's Vibe Prospecting key — primary discovery
MAX_LEADS_PER_RUN        Discovery cap per run (default 100)
ICP_PERSONA              Target job title(s) — set after onboarding call
ICP_COMPANY_SIZE         Target employee range — set after onboarding call
ICP_INDUSTRIES           Target industries (comma-separated) — set after onboarding call
ICP_REGIONS              Target regions (comma-separated) — set after onboarding call
ICP_DISQUALIFY           Disqualification conditions — set after onboarding call
```

### Phase 1 — GitHub repo secrets
```
GOOGLE_SERVICE_ACCOUNT_JSON    Client's service account JSON
SPREADSHEET_ID                 Client's Google Sheet ID
PROSPEO_API_KEY                Client's Prospeo key
VIBE_PROSPECTING_API_KEY       Client's Vibe Prospecting key
SERPER_API_KEY                 Client's Serper key — LinkedIn URL enrichment step (Step 3.5). Skipped gracefully if absent.
GMAIL_SENDER                   Client's Gmail address for operator alerts
GMAIL_APP_PASSWORD             Client's Gmail App Password
NOTIFY_EMAIL                   Client's email for summary reports
```

### Phase 2 — GitHub repo secrets
```
GOOGLE_SERVICE_ACCOUNT_JSON    Client's service account JSON
SPREADSHEET_ID                 Client's Google Sheet ID
SMTP_USER                      Client's Brevo sender email
SMTP_PASS                      Client's Brevo SMTP password
BREVO_API_KEY                  Client's Brevo API key
GMAIL_SENDER                   Client's Gmail address for operator alerts
GMAIL_APP_PASSWORD             Client's Gmail App Password (same value as IMAP_PASS if same inbox)
NOTIFY_EMAIL                   Client's email for summary reports
IMAP_HOST                      imap.gmail.com (fixed)
IMAP_PASS                      Client's Gmail App Password for reply logger (same as GMAIL_APP_PASSWORD if same inbox)
PHANTOMBUSTER_API_KEY          Client's PhantomBuster API key — social outreach (standby: needs session cookie from Mohit)
PHANTOMBUSTER_FB_PHANTOM_ID    PhantomBuster Facebook Message Sender phantom ID
PHANTOMBUSTER_LI_PHANTOM_ID    PhantomBuster LinkedIn Message Sender phantom ID
PHANTOMBUSTER_LI_SESSION_COOKIE LinkedIn session cookie — required to activate social-outreach.yml (pending Mohit)
```

### Phase 2 — GitHub repo variables (non-secret)
```
FOLLOWUP_DELAY_DAYS    Days between touches (default 4)
MAX_FOLLOWUPS          Max touches per lead (default 3)
CALENDLY_URL           Client's Calendly booking link injected into templates
SENDER_NAME            Sender display name for email sign-off (e.g. Mohit Mirchandani)
```

---

## 12. Improvement Log
> Format: [vN | YYYY-MM-DD | description]

- [v1 | 2026-05-17 | Initial project restructure to spec layout. Flat-root → active/ hierarchy. Absolute file paths in config. Log files routed to logs/.]
- [v2 | 2026-05-21 | Social outreach added: PhantomBuster Facebook + LinkedIn via social-outreach.yml (manual dispatch). Leads sheet col K = linkedin_url. Instagram replaced by LinkedIn throughout.]
- [v3 | 2026-05-23 | Switched Vibe discovery from MCP export-to-csv to direct Explorium REST API (api.explorium.ai/v1). Root cause: export-to-csv returns a portal URL (app.vibeprospecting.ai/lists), not a programmable download endpoint. REST API requires mode:"full" at root and all filters nested under "filters" key. Removed has_email filter to allow name-only leads through to the enrichment tier (Prospeo T0).]
- [v4 | 2026-05-24 | Phase 2 SMTP fix: SMTP_FROM was silently falling back to SMTP_USER (Brevo relay credential) causing Brevo to reject sends. Fixed smtp_client.py to use SMTP_FROM exclusively with hard validation. Workflow fix: SMTP_FROM missing from phase2-outreach.yml env block. Warmth score formula added (seniority+size+linkedin+email, 0-10). Explorium credit alert added. Template: removed EY/Waseda line from touch-standard-1.txt.]
- [v5 | 2026-06-02 | LinkedIn URL backfill: one-shot scripts/enrich_linkedin_urls.py created to back-fill column K for existing leads using Serper (site:linkedin.com/in query). Delete after use. PhantomBuster timezone confirmed as workspace-level setting (Account Settings → Workspace Settings → Timezone → America/New_York). Session cookie refresh required from Mohit to unblock LinkedIn outreach.]
- [v6 | 2026-06-06 | LinkedIn URL enrichment integrated into Phase 1 as Step 3.5 (enrich_linkedin_step.py). Serper used as fallback when Vibe/Explorium does not return linkedin_url. SERPER_API_KEY added to phase1-discovery.yml (GitHub secret — add to repo settings). social-outreach.yml remains workflow_dispatch only (standby, pending Mohit credentials). No-email lead dedup fixed: added Level 3 (name+company pair) to _deduplicate() and get_existing_name_company_pairs() to sheets_client.py.]

---

## 13. Discovery Hierarchy, ICP, and Two-Phase Protocol

### Discovery Source Priority
Vibe Prospecting MCP → Prospeo → Manual assist

### Enrichment Tiers (missing email only)
- T0: Prospeo `/enrich-person` (requires contact name, verified)
- T1: Apify Contact Info Scraper (100-URL chunks, 5-min timeout)
- T2: Serper snippet + contact-page scrape (30s timeout)
- T3: Auto-delete row if still no email

### Retired Sources (never re-add)
Apify Places, Serper discovery, SerpAPI, Apify Leads Finder.
Root cause: return business listings without verified personal emails. Yield ~3%.

### ICP Configuration
```
ICP_PERSONA     = HVAC company owner,founder,CEO,director,Head of Marketing,CMO
ICP_COMPANY_SIZE= 10-50,50-200
ICP_INDUSTRIES  = HVAC,Heating Ventilation and Air Conditioning
ICP_REGIONS     = Florida,USA  ← SATURATING. Expansion to TX,GA,NC,TN recommended, awaiting client approval.
ICP_DISQUALIFY  = Any company outside the HVAC industry or outside Florida USA  ← update when ICP expands
DAILY_EMAIL_CAP = 300
MAX_LEADS_PER_RUN = 100
```

### Outreach Sequence
- Series A: `[CLIENT_REGION_SERIES_A]` → `[CLIENT_TEMPLATE_PREFIX_A]-{1,2,3}.txt`
- Series B: `[CLIENT_REGION_SERIES_B]` → `[CLIENT_TEMPLATE_PREFIX_B]-{1,2,3}.txt`
- Templates live in: `active/outreach/templates/`
- Region → prefix routing defined in `config.py → REGION_TEMPLATE_MAP`

### Pipeline Pause / Resume
- Phase 1: create `PIPELINE_PAUSED` file at repo root → runner exits gracefully.
- Phase 2: disable `phase2-outreach.yml` from GitHub Actions tab.
- Manual Phase 2 trigger: GitHub Actions tab → phase2-outreach.yml → Run workflow.
