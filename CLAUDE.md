# CLAUDE.md — Lead Manager Pipeline
> Auto-managed. Propose updates whenever patterns, errors, or inefficiencies are observed.
> Under 300 lines. Satellite files live in active/research/.

---

## 1. Project Identity

**What it does:** Two-phase autonomous B2B lead pipeline. Phase 1 handles discovery, structuring, deduplication, and enrichment on a local Windows schedule. Phase 2 handles outreach sequencing, follow-up automation, and Brevo reconciliation via GitHub Actions.
**Target / Offer (v13 pivot):** US social-media & digital-marketing agencies. Offer = guaranteed PR placements + reputation management (remove policy-violating Google reviews, suppress negative URLs ranking for the agency's brand). Previously HVAC companies in FL/TX/GA/NC/TN.
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
  → Email verification: BillionVerify (verify_emails_step.py) → bad emails blanked
      + logged to "Removed Emails" tab, lead row RETAINED for social outreach
  → Follow-up staging (status/count advance only, no emails sent)
  → Phase 1 summary email (notify.py → Gmail SMTP)
```

### Phase 2 — Outreach Engine (GitHub Actions, 10:17 UTC + 15:17 UTC catch-up)
Entry point: `.github/workflows/phase2-outreach.yml` → `active/outreach/main.py`
Fires twice daily (v21) — 15:17 is a reliability safety net, not a second budget; see §5.

```
Trigger (GitHub Actions)
  → Seed today's send count from outreach_log (shared daily cap across both firings)
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
- NEVER advance `followup_count`/status in Phase 1. Follow-up sequencing belongs
  SOLELY to the Phase 2 outreach engine, which bumps the count only AFTER it sends
  a touch. The old `advance_followup_staging()` Step 4 bumped the count without
  sending → Phase 2 sent touch (count+1) → the staged touch's email was silently
  skipped (and under-sent leads got closed early). It was removed; the function is
  deprecated in `sheets_client.py`. Do not re-wire any staging step into a runner.

**Phase 2 agent (main.py):**
- Never send Touch 2/3 unless `last_contacted` is at least `FOLLOWUP_DELAY_DAYS` ago.
- Never send to `status=replied` or `status=closed`.
- Check outreach_log cache before every `append_outreach_log()`. No duplicate `(email, stage_number)` pairs.
- SMTP health halt: >50% failure rate after 5+ sends. Exit code 1, alert email sent.
- NEVER sleep between an SMTP send and its Sheet writes (status + outreach_log) — a hard
  kill in that window strands a sent-but-unrecorded lead → duplicate send next run.
  Pacing = engine-side `pace_sleep()` AFTER the writes; `send_email(pace=False)` for leads.
- Time-budget stop (v20) is a NORMAL outcome: loops stop `TIME_BUDGET_SAFETY_MINUTES`
  before the workflow hard kill, cleanup/summary/post-sync still run, exit 0. Exit 1
  stays reserved for SMTP health degradation. Effective cap = min(warm-up rung,
  DAILY_EMAIL_CAP, budget ÷ MAX_SEND_GAP_SECONDS) — self-governing, never outgrows the timeout.
- Phase 2 fires TWICE daily (v21): 10:17 UTC primary + 15:17 UTC catch-up, ≥4h apart
  (must exceed the 180-min run budget so firings can never overlap). The catch-up
  firing is a reliability safety net for GitHub schedule drift/drops AND hosted-runner
  capacity failures (e.g. run #59, "job not acquired") — idempotent by construction
  (status machine + outreach_log dedup), so it is a no-op when the primary already
  cleared the queue. `DAILY_EMAIL_CAP` is a per-CALENDAR-DAY ceiling shared across both
  firings, not per-run: `main.py` seeds `SMTPSession.sends_today` from outreach_log rows
  already logged today (UTC) before either send loop runs
  (`sheets_client.get_outreach_log_cache_and_today_count` → `smtp_client.seed_sends_today`)
  — never do a second full Sheets read for this; it's fused into the existing
  outreach_log cache load (one read, two results).

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

- **Last milestone:** Phase 1 made Vibe-only + scheduled-run reliability fixed (v10/v11 | 2026-06-15). Diagnosed missed Mon run = GitHub best-effort `schedule` delay/drop (not disabled/failing); crons moved off top-of-hour; missed run compensated via manual dispatch. Prospeo/Apify/Serper email enrichment removed (was 400-failing + auto-deleting Vibe leads).
- **Current focus:** DISCOVERY US → US+CANADA, person AND company in-region (v22 | 2026-07-12). Client (Mohit) confirmed expanding to US+Canada (US alone too thin on mid agencies) and clarified "no C-level" = big-brand execs who already have a PR firm, NOT agency owners — so persona is UNCHANGED (agency owners/founders/CEOs ARE our buyers, kept via cxo; big brands excluded by linkedin_category + 11-50 size). Canada added in run_vibe_api_discovery.py: 13 `ca-xx` province codes + `canada`/`ca` tokens on the company-region filter; the v19 person-level `country_code` filter + `country_name` geo-screen made Canada-aware (country derived from resolved region-code prefixes — never hardcoded), so BOTH the agency and the contact must be in-region. Verified live (all checks pass; pool 48,327 US → 52,028 US+CA). GATING repo var Rizan must set: `ICP_REGIONS=USA,Canada` (+ `ICP_DISQUALIFY` → US/Canada). Also told Rizan: PhantomBuster free reset ≈ 30 leads/mo of social = a trickle, not scalable (email stays the engine; social still standby pending the LinkedIn cookie). Prior: HANDS-OFF RELIABILITY (v20-v21 | 2026-07-10). Goal: hand the whole pipeline to client Mohit (non-technical) with zero runs needing a human — reframed from "0% failed runs" (impossible; GitHub hosted runners have real incidents, see run #59) to "0% failures that need a human," via self-healing redundancy. v20 = graceful time-budget stop: night run 2026-07-09 hard-killed at `timeout-minutes: 60` (50/day × 100-120s pacing ≈ 92 min); fix = one PHASE2_TIMEOUT_MINUTES knob (default 180) driving hard kill + engine soft stop, self-governing cap (budget ÷ max gap) so the ramp never outgrows the window, pacing moved AFTER Sheet writes (duplicate-send window closed), budget stop = exit 0 + summary line. v21 = redundant same-day cron (10:17 + 15:17 UTC, ≥4h apart) SUPERSEDES the cron-job.org plan for the hands-off goal — no expiring token, no second account for Mohit to maintain, and it also catches hosted-runner capacity failures that an external dispatch cannot fix. Safe because the pipeline is already idempotent (status machine + outreach_log dedup); DAILY_EMAIL_CAP made a true shared per-day ceiling via a seeded send count so two firings can't add up to 2x volume. Deferred: 1-page non-technical runbook for Mohit (not yet written). Prior: NON-US LEAD LEAK FIXED (v19 | 2026-07-09). The 2026-07-09 run wrote 2 leads with contacts in Taiwan/Israel — the geo filter only ever constrained the COMPANY's location, never the person's. Fix = prospect-level `country_code` filter derived from ICP_REGIONS + credit-free post-fetch `country_name` screen; both foreign leads deleted from the sheet before any send. v18 CONFIRMED LIVE from the Discovery State tab: today's run used key 236f1837 (11-50 + strict linkedin_category, pool 30,816) — Rizan's var flip is in effect. BillionVerify (v12) still pending first live field-shape confirmation.
- **Pending decisions:** (v22) Set repo var `ICP_REGIONS=USA,Canada` (+ `ICP_DISQUALIFY` → US/Canada), then merge `feat/discovery-add-canada` — code/docs alone do not redirect sourcing. Confirm no account-level Brevo footer double-ups with the baked-in template footer.
- **TODO next session:** Merge the schedule/Vibe-only PR. Consider external scheduler (cron service → workflow_dispatch API) if GitHub `schedule` drift keeps missing runs. Remove Python social outreach engine (social_main.py, social_engine.py, phantombuster_client.py, social-outreach.yml).
- **Known issues:** GitHub `schedule` events fire hours late / occasionally drop (best-effort) — inherent GitHub limitation, only fully solvable with an external trigger.
- **Locked choices:** No Apify Places, Serper discovery, SerpAPI, Apify Leads Finder. Prospeo retired (discovery + email enrichment) — Vibe-only. Serper kept for SOCIAL URL enrichment only. Social outreach = PhantomBuster native only (not Python engine).

---

## 11. Environment

> All credentials are client-owned. Never substitute operator keys. Await client onboarding before filling any credential field.

### Phase 1 — local `.env` (never committed)
```
PROSPEO_API_KEY          Client's Prospeo key — discovery + enrichment
APIFY_API_TOKEN          Client's Apify token — Contact Info Scraper (Tier 1)
SERPER_API_KEY           Client's Serper key — fallback scrape (Tier 2)
VIBE_PROSPECTING_API_KEY Client's Vibe Prospecting key — primary discovery
BV_API_KEY               Client's BillionVerify key — email verification (Phase 1 Step 1.6). Skipped gracefully if absent.
MAX_LEADS_PER_RUN        NEW leads targeted per run (default 100) — known prospects are skipped at fetch (v16)
ICP_PERSONA              Target job title(s) — set after onboarding call
ICP_COMPANY_SIZE         Target employee range — set after onboarding call
ICP_INDUSTRIES           Target industries (comma-separated) — set after onboarding call
ICP_REGIONS              Target regions (comma-separated). USA,Canada (v22). Country tokens
                         (USA/Canada) expand to all state/province codes AND drive the
                         person-level country_code filter — both company and contact in-region.
ICP_DISQUALIFY           Disqualification conditions — set after onboarding call
```

### Phase 1 — GitHub repo secrets
```
GOOGLE_SERVICE_ACCOUNT_JSON    Client's service account JSON
SPREADSHEET_ID                 Client's Google Sheet ID
PROSPEO_API_KEY                Client's Prospeo key
VIBE_PROSPECTING_API_KEY       Client's Vibe Prospecting key
SERPER_API_KEY                 Client's Serper key — LinkedIn URL enrichment step (Step 3.5). Skipped gracefully if absent.
BV_API_KEY                     Client's BillionVerify key — email verification (Step 1.6). Skipped gracefully if absent.
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
MAX_FOLLOWUPS          Total touches per lead (default 5). = highest touch-standard-{N}.txt.
                       Raising it only needs a matching template file; a missing file ends
                       the sequence gracefully (engine closes the lead, no failed sends).
CALENDLY_URL           Client's Calendly booking link injected into templates
SENDER_NAME            Sender display name for email sign-off (e.g. Mohit Mirchandani)
DAILY_EMAIL_CAP        Hard ceiling on sends per run (default 300). Set LOW (e.g. 25) to bound
                       GitHub Actions minutes on a private free plan — wall time ≈ (cap−1) ×
                       avg send gap, and GitHub bills the pacing sleep. Effective cap =
                       min(this, warm-up rung, time-budget ceiling).
PHASE2_TIMEOUT_MINUTES Single knob for run length (default 180, unset-safe). Drives BOTH the
                       workflow `timeout-minutes` (hard kill) and the engine's soft stop
                       (timeout − 15 min). Also bounds sends/run via budget ÷ MAX_SEND_GAP_SECONDS
                       (≈82 at 180/120s). Raise it to send more per day; 6h GitHub job ceiling.
```

---

## 12. Improvement Log
> Format: [vN | YYYY-MM-DD | description]

- [v22 | 2026-07-12 | DISCOVERY EXPANDED US → US + CANADA (person AND company in-region). Client (Mohit, Telegram) confirmed two things: (1) "C-level are not targets" = big-brand execs who already have a large PR firm, NOT agency owners — agency owners/founders/CEOs (`cxo`) are exactly who we email and stay in the persona; big brands are already excluded by the `linkedin_category` industry gate + the 11-50 size ceiling, so ICP_PERSONA is UNCHANGED (stripping `cxo` would delete the buyers — the paradox Rizan flagged). (2) He expanded targeting to US+Canada because the US alone is too thin on mid agencies → discovery must source both, and BOTH the contact person AND the company must be in-region. A repo-var flip alone would NOT work: `_build_region_codes` only knew US tokens, so a `Canada` token hit the "unrecognized → skipped" branch and kept sourcing US-only. Changes (all in run_vibe_api_discovery.py, built on top of the v19 person-geo plumbing — NOT a rewrite): added `_CANADA_PROVINCE_CODES` (13 ISO-3166-2:CA `ca-xx` codes) + wired `canada`/`ca` into `_COUNTRY_REGION_EXPANSION` (company-region filter); made `_build_prospect_countries` Canada-aware by DERIVING the person-level `country_code` values from the resolved region-code prefixes (us-* → us, ca-* → ca) instead of the old hardcoded "us" — so it always tracks ICP_REGIONS (no hardcoding); added `"ca":"canada"` to `_COUNTRY_CODE_TO_NAME` so the credit-free post-fetch `_geo_deny_reason` screen accepts Canadian contacts; relabeled the reject reason `non_us_prospect` → `out_of_region_prospect`. Adding Canada to the filter set rotates `_filter_key` → cursor walks fresh (pivot-safe, no manual reset). Live-probe findings that shaped it: `ca-xx` codes valid on the company filter (no 422); prospect payload exposes person-level `country_name`/`region_name`/`city`; `country_code` is genuinely person-scoped (Canadian-company prospects 3,701 → 3,280 when the person must also be in Canada). Verified live end-to-end against real main (Sheet isolated via monkeypatched cursor): region_codes=64 (51 US + 13 CA), prospect_countries=[ca,us], geo screen keeps US/CA + drops India + passes blank, filter_key rotates, real fetch returned only US/Canada contacts. Pool grew US 48,327 → US+CA 52,028. No change to templates (DEFAULT_TEMPLATE_PREFIX catches CA leads), config.py, persona/size/industry. GATING: Rizan must set repo var `ICP_REGIONS=USA,Canada` + update `ICP_DISQUALIFY` to US/Canada (code/docs alone never redirect sourcing). NOTE for the record: the initial edits were mistakenly made on the stale local `fix/sheet-backed-cursor` branch (pre-v17/v19) and discarded; the shipped change is on `feat/discovery-add-canada` off origin/main. Separately advised Rizan re PhantomBuster: the free plan's monthly credit reset ≈ 30 leads/mo of social outreach = a trickle, not a scalable channel — email stays the engine. Deploy = merge to main; effective next scheduled Phase 1 run.]
- [v21 | 2026-07-10 | REDUNDANT DAILY FIRING + SHARED DAILY CAP — follow-up to v20, same branch (`fix/phase2-time-budget`). Goal: Rizan wants to hand the pipeline to client Mohit (non-technical) and go fully hands-off; asked how to get failed runs to 0%. Literal 0% is impossible (run #59 was a GitHub hosted-runner capacity failure, not a config bug — the platform itself has incidents); the achievable target is 0% runs that need a human, via self-healing. SUPERSEDES the cron-job.org external-scheduler plan for this goal: an external dispatch needs a GitHub PAT that expires (fine-grained tokens max ≤1yr) plus a second account — a recurring maintenance chore for a non-technical owner, and it can't fix a capacity failure anyway (the runner still isn't there). FIX: `.github/workflows/phase2-outreach.yml` gained a second `schedule:` cron, `17 15 * * *` (15:17 UTC), 5h after the 10:17 primary — intentionally > the 180-min run budget so the two firings can never overlap. Safe by construction: Touch 1 only selects `status=new` (flipped by a successful send), Touch 2+ requires `last_contacted` older than `FOLLOWUP_DELAY_DAYS`, and every send is deduped on `(email, stage_number)` in outreach_log — a second firing cannot re-send what the first already recorded. On a normal day the catch-up firing is a ~2-3 min no-op (reply poll + syncs only); on a drift/drop/capacity-failure day it does the actual work. Companion fix so `DAILY_EMAIL_CAP` stays a true per-CALENDAR-DAY ceiling (not per-run, which would let two firings add up to 2x the day's volume): `sheets_client.py` new `get_outreach_log_cache_and_today_count()` — ONE outreach_log read (no second Sheets API call) building both the existing dedup cache AND a count of rows whose `sent_date` (full UTC isoformat) falls on today; `smtp_client.py` new `seed_sends_today(count)` (only raises, never lowers, `SMTPSession.sends_today`); `main.py` Step 4 seeds it before either send loop runs. `effective_daily_cap()`/`is_cap_hit()` needed no changes — they already read `sends_today`, so a firing that finds the day's cap already spent by the earlier firing correctly sends nothing. Verified: py_compile (6 files) + YAML re-parse (both cron entries + workflow_dispatch + env vars intact) + 13-check offline harness (today-only counting excludes yesterday's rows, seed raises but never lowers an in-process count, cap-exhausted seed correctly trips is_cap_hit, warm-up-rung interaction). Deferred (Rizan: "keep plan 3 for later"): 1-page non-technical runbook for Mohit covering pause/resume, manual dispatch, credit top-up, and alert setup — not yet written; also not yet done: transferring GitHub org ownership/Actions billing to Mohit, low-balance alerts on Explorium/Brevo/BillionVerify credits, confirming NOTIFY_EMAIL + repo Watch settings point to him. These are the residual hands-off gaps redundancy does NOT cover.]
- [v20 | 2026-07-10 | PHASE 2 TIMEOUT FIX — graceful time budget + self-governing cap + duplicate-send window closed. The 2026-07-09 night run hit `timeout-minutes: 60` and was HARD-KILLED mid-send-loop. Root cause = arithmetic: warm-up ramp reached 50/day on day 15 while pacing is 100-120s/send → ~92 min of sleep vs a 60-min timeout (the old YAML comment "60 min covers ≤50/day" was wrong; ramp keeps growing 75→100→150→200/day). A hard kill also (a) loses everything after the send loops (summary email, log dedup, template metrics, Brevo post-sync — none ran) and (b) exploits a DUPLICATE-SEND window: the pacing sleep lived INSIDE send_email AFTER the SMTP hand-off but BEFORE update_lead_status/append_outreach_log, so a kill during those ~2 min strands a sent-but-unrecorded lead that the next run re-sends. FIX (branch fix/phase2-time-budget): (1) config: PHASE2_TIMEOUT_MINUTES (_int_env, default 180) → RUN_TIME_BUDGET_SECONDS = (timeout − 15 min safety) — ONE repo var drives both the workflow `timeout-minutes: ${{ vars.PHASE2_TIMEOUT_MINUTES \|\| 180 }}` and the engine's soft stop; (2) smtp_client: effective_daily_cap = min(warm-up rung, DAILY_EMAIL_CAP, _budget_send_ceiling = budget ÷ MAX_SEND_GAP_SECONDS, floor 1) → sends/run can NEVER outgrow the window (set-and-forget per Rizan — ramp tops out at ~82/day at 180 min/120s until the var is raised); pacing extracted to pace_sleep(deadline) (skips when the gap would cross the deadline); (3) outreach_engine: both loops take `deadline` (time.monotonic), check it top-of-iteration alongside is_cap_hit → break with stats time_budget_hit/deferred; per-send order is now send_email(pace=False) → status write → log append → log_variant → pace_sleep AFTER the writes (window closed), no trailing sleep after the batch's last lead (~2 min saved/run); (4) main.py: deadline computed once, threaded to both loops, follow-ups skipped when Touch 1 exhausted the budget; time_budget_hit/deferred → summary email ("Time budget: stopped early, ~N deferred") — NORMAL outcome, exit 0 (exit 1 stays SMTP-health-only, orthogonal); (5) workflow comment math corrected. Deliberately NOT changed: send gap 100-120s (deliverability, Rizan), reply-logger.yml (15 min fine). Scaling note: 6h GitHub job ceiling ≈ 172 sends at 120s — past that, shrink gap or split runs. Verified: py_compile ×5 + offline harness (budget stop mid-batch, call-order assertion send→writes→pace, cap table incl. day-30 self-cap engage, exit 0 + summary line). Follow-up check: audit last night's window for a sent-but-unlogged lead (likely 1 duplicate went out today — retroactively unfixable, log-only). SEPARATE track: external scheduler (cron-job.org → workflow_dispatch, phase2+reply-logger only) agreed with Rizan; schedule-block-removal PR gated on a 3-4 day parallel window.]
- [v19 | 2026-07-09 | NON-US LEADS FIXED (person-level geo filter) — the 2026-07-09 scheduled Phase 1 run wrote 2 leads whose CONTACTS sit outside the US (Growth Hackers founder in Kaohsiung/Taiwan; Blue Seedling in Israel) despite ICP_REGIONS=USA. ROOT CAUSE (not a regression — gap existed since v3): the only geo filter ever sent was `company_region_country_code`, which constrains the COMPANY's location; both companies have a US footprint in Explorium, but the PERSON we email was never filtered. The sheet `region` column comes from the prospect's own region_name/country_name, which is why the mismatch was visible. Live-probe proof: current pool 30,816 → 28,092 with a prospect-level filter added, i.e. ~9% of the pool is people located abroad (matches 2-of-50 leaking). FIX in run_vibe_api_discovery.py: (1) new `_build_prospect_countries()` derives prospect-level `country_code` values from ICP_REGIONS (country aliases AND state names both imply "us"; unresolved → filter omitted + warning, mapper conventions preserved); `_fetch_prospects` now sends `country_code:{values:["us"]}` — Explorium ACCEPTS bare "us" at prospect level (unlike the company region filter which 422s, v14); (2) credit-free post-fetch geo screen `_geo_deny_reason` next to the deny-keyword screen: prospect `country_name` outside the ICP countries → rejected BEFORE enrichment, folded into icp_rejected, logged to failed_records.jsonl as `non_us_prospect:<country>`; blank/missing country_name PASSES (filter is primary, never over-drop); (3) shared `_log_rejected()` helper de-dupes the failed-record payload. Filter change → cursor key 236f1837 → 01338ac8 (auto-reset offset 0, credit-free via v16 dedup-aware fetch). SANITIZED the live sheet same session (BEFORE the delayed Phase 2 cron could fire — both leads were status=new, never emailed, no outreach_log rows): DELETED john@growth-hackers.net + netta@blueseedling.com per Rizan (99→97 rows); full-sheet audit found only 1 other non-US row ever — Dubayy 2026-06-20, already closed, kept. Verified: py_compile; offline harness (mapper resolutions incl. states→us + unset→omit; geo screen rejects taiwan/israel pre-enrichment with correct reasons, passes US + missing-country, known-pair skip intact; country_code present in captured request; key change + stability); LIVE free probe through the module's own builders (HTTP 200, pool 28,092, zero credits). Deploy = merge fix/us-only-prospect-filter before Mon 2026-07-13 07:17 UTC Phase 1 run.]
- [v18 | 2026-07-09 | ICP SIZE NARROWED TO MID-ONLY — Mohit explicitly wants ONLY mid-sized agencies (earlier read was medium + large). Mapping: live `ICP_COMPANY_SIZE=11-200` resolved via `_build_company_sizes` overlap matching to Explorium buckets 11-50 + 51-200; "mid-only" = the 11-50 bucket alone (Rizan confirmed; consistent with Mohit's on-record 1-50 spec while keeping the ≥11 no-micro floor). FIX = repo var `ICP_COMPANY_SIZE` 11-200 → 11-50, ZERO code change — exactly the "tighten later via repo var" path v17 designed for. Effective the next Phase 1 run after Rizan flips the var (workflow injects it; no merge needed for behavior — this commit is docs-only). Pool sanity (credit-free stats, linkedin_category ∩ US): 11-50-only = 260,308 prospects vs 514,161 at 11-200 (~51% retained; scaled onto v17's ~59k seniority-filtered pool ≈ 30k — years of runway at 50/run). Verified via harness through the REAL origin/main builders + `_filter_key`: `11-200`→['11-50','51-200'] key 9dc2ff81 (matches the live v17 cursor key = harness reproduces prod path), `11-50`→exactly ['11-50'] new key 236f1837 (cursor auto-resets to offset 0, credit-free per v16 dedup-aware fetch — known leads skipped pre-enrichment), key process-stable (identical across runs), and the floor gotcha proven live: `10-50`→['1-10','11-50'] (floor MUST stay 11). NOT changed: `_build_company_sizes` (var-driven by design), warmth-score branch `company_size in ("11-50","51-200")` (heuristic — the 51-200 arm just stops firing, same precedent as the v15 brevo_reconcile breakup branch), existing sheet leads sized 51-200 (Rizan's call: CONTINUE their sequence, no audit/close — only NEW discovery narrows). Confirm post-flip: first Phase 1 run logs `sizes: ['11-50']`, fresh Discovery State cursor row, new leads all 11-50.]
- [v17 | 2026-07-08 | STRICT-ICP DISCOVERY — client (Mohit) flagged off-ICP leads in the live sheet: "Babak" (founder of TapClicks) is martech/AI, plus two "media agency" contacts; he wants strictly US digital-marketing + social-media agencies (~$600k+/yr, 1-50 employees) and 5-10 replies/100. ROOT CAUSE of the leakage: the ONLY industry control was the `naics_category` filter, and inferred NAICS misclassifies — Explorium files TapClicks (a martech SaaS "Marketing Operations Platform") under 541613 "Marketing Consulting Services" (confirmed live via enrich-business firmographics), and the live filter also carried 541820 (PR Agencies = Mohit's competitors) + 541890 (grab-bag). The `/v1/prospects` payload was proven (live probe) to carry NO company-industry field, so a clean per-prospect NAICS/industry gate wasn't even possible post-fetch. FIX: replaced `naics_category` with the self-labeled `linkedin_category` as the sole industry filter — Explorium permits only ONE of naics/linkedin/google category per query (422 on both; confirmed live), and LinkedIn category classifies agencies far more accurately. New `ICP_LINKEDIN_CATEGORIES` config var (default `marketing services,advertising services`; both verified in the taxonomy; ~59k US-prospect pool at the strict filter). PR firms are their OWN category ("public relations and communications services") so competitors are excluded by construction; TapClicks-style martech sits in tech categories, excluded too. Added a credit-free secondary `ICP_DENY_KEYWORDS` screen (default `staffing,recruiting,recruitment,software development,web hosting`) matched against COMPANY NAME inside the dedup-aware fetch walk BEFORE enrichment — denied prospects cost no enrichment call, don't count toward target, and are logged to failed_records.jsonl (never silently dropped); high-precision tokens only ("saas"/"software" intentionally NOT denied — "SaaS marketing agency" is in-ICP). Every written lead now carries `site:<domain>` in notes for post-hoc ICP auditing. `_build_naics_codes`/`_INDUSTRY_TO_NAICS` retired (kept for reference, no longer called). `_fetch_prospects` now returns (prospects, skipped_known, icp_rejected); stats + Phase 1 summary email gained an "Off-ICP rejected" line; phase1-discovery.yml passes the two new repo vars (empty-injection-safe via `os.getenv(k) or default`). Swapping naics→linkedin changes the filter hash, so the discovery cursor AUTO-RESETS to offset 0 of the new pool (pivot-safe); with the v16 dedup-aware fetch already merged, that reset is credit-free (the ~61 known leads are skipped at fetch, zero enrichment). SIZE/REVENUE unchanged — Rizan's explicit call to follow the live repo vars (ICP_COMPANY_SIZE stays 11-200); tighten later via repo var, no code change. Verified: py_compile (4 files) + offline harness (linkedin_category present & naics absent in the filter dict; old key db017114 → new key 9dc2ff81 = cursor reset; key process-stable; deny screen keeps "SaaS Growth Agency"/"TapClicks", rejects "…Staffing"/"…Software Development"; website written to notes). NOT live-run (no sheet/credit mutation) — first GHA run after merge does the real offset-0 pass. Companion one-time audit of the existing sheet leads (flag tech/consulting/PR/media → status=closed) handled separately. Deploy = merge branch fix/strict-icp-discovery to main; Rizan optionally sets ICP_LINKEDIN_CATEGORIES / ICP_DENY_KEYWORDS repo vars to override the defaults.]
- [v16 | 2026-07-07 | DISCOVERY ALL-DUPES FIXED (two root causes) — the 2026-07-06 scheduled Phase 1 run yielded New: 0, Dupes: 50. ROOT CAUSE 1 (primary, why the cursor kept resetting): `_filter_key` hashed the resolved filter set, but `_build_job_levels` and `_build_company_sizes` returned their values from a Python `set`, whose iteration order is randomized PER PROCESS (PYTHONHASHSEED). So EVERY run computed a different filter_key for the SAME ICP → no matching cursor row → offset 0 → re-scrape the top of the pool → all dupes, forever, regardless of ICP edits. PROVED locally: 3 identical-config processes produced 3 different keys (1206ad1d / dae95028 / 37f8b496); the 3 rows in the live Discovery State tab (06-27, 07-03, 07-06, all different keys) are this, not ICP edits. FIX 1: `_filter_key` now sorts every filter value list before hashing (defense-in-depth), and both set-based builders return `sorted(...)` — key is now byte-identical across processes (verified 3/3 → 2a6d7303c5623d3a with prod ICP overrides). naics was already `sorted()` (unaffected); regions is order-deterministic. ROOT CAUSE 2 (amplifier): the fetch loop consumed exactly `target` records from the offset and deduped only AFTER per-prospect enrichment, so any offset-0 reset burned the whole budget + 50 enrichment calls for 0 new leads. FIX 2 (dedup-aware fetch): (a) `sheets_client.get_all_name_company_pairs()` — (name, company) pairs for EVERY lead row (existing `get_existing_name_company_pairs` = email-less rows only; raw prospects have no email pre-enrichment so name+company is the only fetch-time key); (b) `_fetch_prospects(api_key, target, known_pairs)` walks per-record, SKIPS prospects already in the sheet (or already taken this run) BEFORE enrichment, continues until `target` NEW prospects collected — bounded by scan_cap = max(target*6, 300) (`_SCAN_CAP_MULT`/`_SCAN_CAP_MIN`); cursor advances past everything scanned so dupes never re-scanned; returns (prospects, skipped_known); (c) all-known slice logs INFO not pipeline_error; fetch-skips fold into stats dupes_skipped; post-enrich `_deduplicate` untouched (email-exact + fuzzy safety net). (3) `phase1-discovery.yml` gained a `max_leads` workflow_dispatch input overriding MAX_LEADS_PER_RUN per-run (schedule unaffected; ""→_int_env default). SEMANTICS: MAX_LEADS_PER_RUN now = NEW leads targeted per run. Verified offline (13-check monkeypatched harness PASS) + py_compile, AND LIVE against the real sheet 2026-07-07: reset the stable key to offset 0 (the exact failure scenario), ran target=10 → skipped 50 already-known at fetch (0 enrichment spent on them), wrote 10 genuinely NEW US-agency leads (sheet 51→61, cursor 0→60, Dupes: 50 fetch-skipped + 0 post-enrich, Failed 0). New leads: Partnerboost, Feedmob, Content cucumber, Fire and spark, Starline, Common thread collective, Full circle agency, Ascendeum, Plein air agency, Incrementum digital — all with emails, status=new. NOTE: the live test used prod-ICP overrides because the LOCAL .env is stale HVAC (NAICS 238220) — after merge the GHA run computes ITS OWN stable key (fresh row, one graceful offset-0 skip of the now-61 known, then resumes forever).]
- [v15 | 2026-07-03 | EMAIL SEQUENCE REPLACED — client (Mohit) delivered a revised cold-email sequence (`New Agency_outreach_copy_revised by client.pdf`): PR white-label angle (guaranteed tier-1 US media placements, agency keeps client + margin), CAN-SPAM-revised, em dashes removed. Grew 4-touch → 5-touch (1 initial + 4 follow-ups): rewrote touch-standard-1..4.txt + NEW touch-standard-5.txt. The old Touch-4 breakup ("closing the loop") is GONE — every follow-up (2-5) now threads off Touch 1 via `Re:`. Copy conventions: PDF `{{first_name}}`→`{{name}}` (engine has no first_name token; {{name}} already renders first-name-only); sign-off via `{{sender_name}}`; Calendly kept as the LITERAL `www.calendly.com/mohitdm` (CALENDLY_URL repo var is empty, so the token would render blank); footer baked into every template (engine appends none). KEY GOTCHA fixed: the variation engine OVERRIDES the .txt — `variation_engine.build_plan` returns non-empty whenever `templates/variants/{prefix}-{N}.json` exists, and outreach_engine sends the variant body (+ Touch-1 subject) instead of the .txt. The 4 old variant JSONs held OLD-copy paraphrases, so editing only the .txt would still send old copy → DELETED all 4 variant JSONs (verbatim mode; build_plan → {}). Bumped config MAX_FOLLOWUPS default 4→5. Verified via render harness through the real _load_template/_render_template path: all 5 touches render, no leftover tokens, footer present, T1 unique subject, T2-5 carry the Re: flag, plans all {}. Client decisions (Rizan): verbatim (no variation), existing mid-sequence leads CONTINUE (no reset — they pick up new copy from their next touch, but still thread under the old "77%…reviews" subject recalled from outreach_log; accepted cosmetic mismatch), postal address NOT added (footer stays email-only, Rizan's call), MAX_FOLLOWUPS=5 repo var ALREADY set by Rizan. Not changed: brevo_reconcile.py breakup/stage heuristic (reconciliation backfill only — its "closing the loop" branch just won't fire now). Social templates untouched. Deploy = merge to main; effective next Phase 2 run.]
- [v14 | 2026-06-20 | DISCOVERY ICP-MAPPER FIX — the v13 pivot's discovery was still returning HVAC-in-Florida leads despite correct GitHub repo vars. Root cause in run_vibe_api_discovery.py: two ICP→Explorium mappers SILENTLY fell back to the retired ICP whenever they didn't recognize the new values. (1) `_build_naics_codes`: `_INDUSTRY_TO_NAICS` only held HVAC/home-services keywords, so `ICP_INDUSTRIES=Marketing & Advertising...` matched nothing → fell back to `["238220"]` (HVAC NAICS). (2) `_build_region_codes`: `ICP_REGIONS=USA` wasn't in the state map → fell back to `["us-fl"]` (Florida). Fix: added verified agency NAICS to the map (541810 Advertising Agencies, 541820 PR Agencies, 541613 Marketing Consulting, 541830 Media Buying, 541890 Other Advertising Services); added `_US_STATE_CODES` (all 51) + `_COUNTRY_REGION_EXPANSION` so USA/United States expands to every state region code (the `company_region_country_code` filter 422s on a bare `us` — needs region codes like us-ca). Both mappers + `_fetch_prospects` now OMIT an unresolved filter (and log a warning) instead of reverting to HVAC/FL — the ICP vars can never be silently ignored again. Validated live: 48,327 US agency prospects match (was HVAC). ICP_COMPANY_SIZE recommended 11-200 (excludes the 1-10 micro bucket) per Rizan "not too small". Repo vars Rizan must still fix: ICP_PERSONA + ICP_DISQUALIFY (screenshot showed stale HVAC text; PERSONA only affects seniority so harmless, but update for correctness), ICP_COMPANY_SIZE 10-50→11-200. Sheet re-reset to one Rizan QA lead.]
- [v13 | 2026-06-20 | NICHE PIVOT: HVAC → US social/digital marketing agencies; offer → guaranteed PR placements + reputation management (remove policy-violating Google reviews, suppress negative URLs). All outreach copy rewritten from the Digital Nod draft: email touch-standard-1..3 reworked + NEW touch-standard-4 breakup ("closing the loop"); social-linkedin-1..3 + social-facebook-1 reworked. Draft's unsupported tokens remapped — `{{Agency Name}}`→`{{company}}`/"your agency's", `{{number}}` dropped, Touch 3 = Version B (no invented numbers); only `{{name}}/{{company}}/{{calendly_url}}/{{sender_name}}` survive. Touch count made fully `MAX_FOLLOWUPS`-driven: de-hardcoded `>=3`/`<3` in sheets_client.advance_followup_staging, config default 3→4, brevo_reconcile breakup→`str(MAX_FOLLOWUPS)`; outreach_engine now CLOSES a lead (not fails) when the next touch template is missing, so MAX_FOLLOWUPS can exceed templates safely (effective ceiling = min(var, files on disk)). ICP block + env docs + SCHEMA stage_number updated. Live Sheet reset (scripts/seed_test_lead.py): backed up then wiped Leads + log/audit tabs to one Rizan QA lead. Action items for client: set repo vars MAX_FOLLOWUPS=4 + ICP_* (Section 13), add CAN-SPAM footer to sender. UPDATE_FOR_MOHIT.md drafted at root.]
- [v1 | 2026-05-17 | Initial project restructure to spec layout. Flat-root → active/ hierarchy. Absolute file paths in config. Log files routed to logs/.]
- [v2 | 2026-05-21 | Social outreach added: PhantomBuster Facebook + LinkedIn via social-outreach.yml (manual dispatch). Leads sheet col K = linkedin_url. Instagram replaced by LinkedIn throughout.]
- [v3 | 2026-05-23 | Switched Vibe discovery from MCP export-to-csv to direct Explorium REST API (api.explorium.ai/v1). Root cause: export-to-csv returns a portal URL (app.vibeprospecting.ai/lists), not a programmable download endpoint. REST API requires mode:"full" at root and all filters nested under "filters" key. Removed has_email filter to allow name-only leads through to the enrichment tier (Prospeo T0).]
- [v4 | 2026-05-24 | Phase 2 SMTP fix: SMTP_FROM was silently falling back to SMTP_USER (Brevo relay credential) causing Brevo to reject sends. Fixed smtp_client.py to use SMTP_FROM exclusively with hard validation. Workflow fix: SMTP_FROM missing from phase2-outreach.yml env block. Warmth score formula added (seniority+size+linkedin+email, 0-10). Explorium credit alert added. Template: removed EY/Waseda line from touch-standard-1.txt.]
- [v5 | 2026-06-02 | LinkedIn URL backfill: one-shot scripts/enrich_linkedin_urls.py created to back-fill column K for existing leads using Serper (site:linkedin.com/in query). Delete after use. PhantomBuster timezone confirmed as workspace-level setting (Account Settings → Workspace Settings → Timezone → America/New_York). Session cookie refresh required from Mohit to unblock LinkedIn outreach.]
- [v6 | 2026-06-06 | LinkedIn URL enrichment integrated into Phase 1 as Step 3.5 (enrich_linkedin_step.py). Serper used as fallback when Vibe/Explorium does not return linkedin_url. SERPER_API_KEY added to phase1-discovery.yml (GitHub secret — add to repo settings). social-outreach.yml remains workflow_dispatch only (standby, pending Mohit credentials). No-email lead dedup fixed: added Level 3 (name+company pair) to _deduplicate() and get_existing_name_company_pairs() to sheets_client.py.]
- [v7 | 2026-06-12 | ICP region expansion approved: TX, GA, NC, TN added alongside FL. run_vibe_api_discovery.py now reads ICP_REGIONS env var dynamically (was hardcoded to us-fl). Root cause of zero new leads: Florida market saturating + filter never reading env var. Update ICP_REGIONS GitHub Actions repo variable to: Florida,Texas,Georgia,North Carolina,Tennessee,USA]
- [v8 | 2026-06-13 | Sheets 429 quota halt fixed. Phase 2 Touch 1 aborted after ~8 leads each run (APIError 429 "Read requests per minute"), stranding the rest at status=new. Root cause in sheets_client.py: _get_sheet() re-authorized gspread + re-ran open_by_key on EVERY call, and update_lead_status re-read the whole email column per lead, with no 429 backoff. Fix: per-process cache of client/spreadsheet/worksheet handles; cached Leads email column (invalidated on append/delete); ensure_headers once per tab per run; _with_backoff() retry wrapper (429/500/503, 1s base, 3 retries); 3× update_cell per lead collapsed into one batch_update. Local run cleared the full backlog in one pass, single auth line, no 429.]
- [v9 | 2026-06-13 | Empty-env-var crash hardening. GitHub Actions injects an UNDEFINED `${{ vars.X }}` as "" (not absent), so `int(os.getenv(key, default))` hit `int("")` → ValueError at config import → entire run (any entry point) died silently. Added _int_env/_float_env helpers in config.py (`os.getenv(key) or default`) and routed all int/float env parses through them — notably the workflow-injected FOLLOWUP_DELAY_DAYS, MAX_FOLLOWUPS (phase2/social) and MAX_LEADS_PER_RUN (phase1). Defaults now apply on empty injection instead of crashing.]
- [v10 | 2026-06-15 | Scheduled runs moved off the top of the hour. Diagnosis (via gh run history): all workflows active and succeeding, but GitHub delivers `schedule` events best-effort and was firing the 07:00/10:00/11:00 UTC crons hours late (15:00–21:00 UTC) and dropped them entirely on Jun 15. Not a config bug. Mitigation: phase1 `0 7`→`17 7`, phase2 `0 10`→`17 10` to reduce top-of-hour contention. Compensated the missed Mon run via manual `gh workflow run`. NOTE: GitHub scheduling is inherently best-effort; for guaranteed timing an external trigger (cron service → workflow_dispatch API) is the only hard fix. GHA Phase 1 path confirmed fully headless via run_vibe_api_discovery — Section 2 "local-only" was stale.]
- [v12 | 2026-06-19 | BillionVerify email verification added to Phase 1 (Step 1.6). New `billionverify_client.py` (bulk endpoint, chunked 100/call, 401/402/429 handling, key never logged) + `verify_emails_step.py` (Sheets path `run_email_verification` + CSV path `verify_csv`). Wired into `phase1_runner.py` after Vibe ingest, before social enrichment; summary email gained a verification block. Sheets behavior: valid/catchall/role KEPT (flagged in notes `bv:<status>`); invalid/risky/disposable/unknown → email BLANKED (only for uncontacted leads) + logged to new "Removed Emails" audit tab, but lead ROW RETAINED for PhantomBuster social outreach (honors v11 retention rule). Idempotent + credit-safe: skips leads already `bv:`-flagged; soft-skips entirely if BV_API_KEY unset (like Serper). Added `BV_API_KEY` to config + phase1-discovery.yml secrets. Added `VERIFY_EMAILS.md` drop-in agent spec at root (manual trigger / CSV). NOTE: written against the BillionVerify v1 spec; response-shape parsing is defensive — verify field names on first live run.]
- [v11 | 2026-06-15 | Phase 1 made VIBE-ONLY. Run log showed Prospeo `/enrich-person` failing 400 "Field required" on every call, the client misreading 400 as a rate-limit, and the Tier-2 delete auto-deleting 11 good email-less Vibe leads per run. Removed Step 2 (Prospeo discovery fallback) and Step 3 (enrich_sheets_emails: Prospeo/Apify/Serper + auto-delete) from phase1_runner.py. Vibe already enriches emails at discovery (Explorium contacts_information/enrich). Email-less leads now KEPT for PhantomBuster social outreach (Phase 2 skips them safely). notify.py summary updated to reflect Vibe-only. enrich_sheets_emails.py left as dead code.]

---

## 13. Discovery Hierarchy, ICP, and Two-Phase Protocol

### Discovery Source Priority — VIBE-ONLY (v11)
Vibe Prospecting (Explorium REST) ONLY → Manual assist.
Prospeo discovery fallback retired. Vibe is the sole automated discovery source.

### Industry Filter — LINKEDIN_CATEGORY, not NAICS (v17)
Discovery filters industry on the self-labeled Explorium `linkedin_category`, NOT
`naics_category`. Explorium permits only ONE of naics/linkedin/google category per
`/v1/prospects` query (422 otherwise), and inferred NAICS misclassified the pool:
martech (TapClicks → 541613 "Marketing Consulting") and PR firms leaked in. The two
agency LinkedIn categories `marketing services` + `advertising services` are the
default (`ICP_LINKEDIN_CATEGORIES`); "public relations and communications services" is
its OWN category, so PR competitors are excluded by construction. `_build_naics_codes`
is retired (kept for reference only). Secondary credit-free `ICP_DENY_KEYWORDS` screen
rejects off-ICP company NAMES (staffing/recruiting/software development/…) BEFORE
enrichment — high-precision tokens only ("saas"/"software" are deliberately NOT denied,
"SaaS marketing agency" is in-ICP). Every written lead carries `site:<domain>` in notes
for post-hoc ICP auditing (the `/v1/prospects` payload has no industry field).

### Email Enrichment — VIBE-ONLY (v11)
Email enrichment happens INSIDE discovery: `run_vibe_api_discovery._enrich_email`
calls Explorium `/prospects/contacts_information/enrich` per prospect. There is no
separate enrichment step. Prospeo/Apify/Serper email enrichment is removed.
- Leads Vibe cannot find an email for are KEPT (not deleted) for PhantomBuster
  social outreach. Phase 2 skips email-less leads safely (skipped, not failed).
- `enrich_sheets_emails.py` is no longer called by the runner (dead code, kept for ref).

### Retired Sources (never re-add)
Apify Places, Serper discovery, SerpAPI, Apify Leads Finder.
Prospeo (discovery + email enrichment) — retired v11: `/enrich-person` returned
400 "Field required" and the delete-tier was auto-deleting good email-less Vibe leads.
Root cause (others): return business listings without verified personal emails. Yield ~3%.
NOTE: Serper is STILL used for SOCIAL URL enrichment only (Step 3.5, LinkedIn/Facebook),
never for lead discovery or email enrichment.

### ICP Configuration (v18 — strict digital/social agencies, mid-size only)
> These are the canonical values. Discovery only changes once the matching GitHub repo
> variables are updated (Rizan/Mohit) — editing this doc alone does not redirect sourcing.
> Client's full ICP (Mohit, 2026-07-08): US digital-marketing or social-media agencies,
> ~$600k+/yr revenue, 1-50 employees; EXCLUDE PR/communications firms (they run PR
> in-house — competitors, not buyers); ideal = active client roster, too small for an
> in-house PR hire (white-label PR fit). Size governed by the live repo var; narrowed
> v18 to mid-size only (Mohit explicit) — still no code change, repo-var only.
```
ICP_PERSONA            = agency owner,founder,CEO,managing director,partner,head of growth
ICP_COMPANY_SIZE       = 11-50
ICP_LINKEDIN_CATEGORIES= marketing services,advertising services   # PRIMARY industry filter (v17)
ICP_INDUSTRIES         = Social Media Marketing Agency and Digital Marketing Agency  # LEGACY (drove retired NAICS map)
ICP_REGIONS            = USA,Canada
ICP_DENY_KEYWORDS      = staffing,recruiting,recruitment,software development,web hosting  # pre-enrichment name screen
ICP_DISQUALIFY         = Not a marketing/advertising/digital/social-media agency; in-house marketing teams; companies outside the US/Canada
DAILY_EMAIL_CAP        = 300
MAX_LEADS_PER_RUN      = 50
```
> v17: `ICP_INDUSTRIES` no longer drives discovery — `ICP_LINKEDIN_CATEGORIES` does (see
> Industry Filter above). `ICP_INDUSTRIES` is retained only because the retired
> `_build_naics_codes` still reads it. Pool at the strict filter ≈ 59k US prospects.
> Industry/persona strings are tuned to the Explorium taxonomy (verified live v14/v17).
> `ICP_COMPANY_SIZE=11-50` maps to exactly the Explorium 11-50 bucket = "mid-sized only"
> per Mohit (v18; matches his written 1-50 spec minus micro). Deliberately EXCLUDES BOTH the
> 1-10 micro bucket (solo freelancers / 1-person shops — Rizan's "agencies that already make
> money, not too small") AND the 51-200 bucket (v18 dropped it; 11-200 had included it).
> Lowering the floor to 10 (e.g. `10-50`) silently re-includes the 1-10 bucket via range
> overlap (harness-proven: `10-50` → buckets ['1-10','11-50']). Floor must stay ≥11.
> `ICP_REGIONS=USA,Canada` (v22 — Mohit expanded targeting; US alone too thin on mid agencies)
> expands to all 51 US state codes + all 13 Canadian province/territory codes on the
> `company_region_country_code` filter (rejects a bare `us`/`ca`). Single states still work by
> name; the `canada`/`ca` tokens expand to Canada. Geo is filtered at BOTH levels (v19+v22):
> company region codes AND prospect-level `country_code` (bare `us`/`ca`, DERIVED from the
> resolved region-code prefixes — never hardcoded) — the company filter alone admits a
> US/Canada company whose contact lives abroad. A credit-free post-fetch screen on prospect
> `country_name` backs it up. So BOTH the agency and the person must be in-region. Verified live:
> US pool 48,327 → US+CA 52,028; Canada inherits every other ICP filter unchanged.
> Prior HVAC ICP (retired v13): persona HVAC owner/founder/CEO; industries HVAC; regions FL,TX,GA,NC,TN,USA.

### Outreach Sequence (v15 — 5-touch, PR white-label copy)
- Email series: `touch-standard-{1..MAX_FOLLOWUPS}.txt` (1-5; revised PR white-label copy, no
  breakup — all of 2-5 thread off Touch 1 via `Re:`). Length is fully `MAX_FOLLOWUPS`-driven.
- Copy sends VERBATIM: variation engine retired — `templates/variants/` deleted so the engine uses
  the flat `.txt`. Footer baked into each template; Calendly is literal `www.calendly.com/mohitdm`.
- Social series (PhantomBuster/Python reference copy): `social-linkedin-{1,2,3}.txt` + `social-facebook-1.txt`.
- Supported template tokens (email engine): `{{name}}`, `{{company}}`, `{{calendly_url}}`, `{{sender_name}}`.
  Social engine substitutes only `{{name}}` + `{{sender_name}}` (keep Calendly as a literal link there).
- Templates live in: `active/outreach/templates/`
- All US leads route to the `touch-standard` series via `config.py → REGION_TEMPLATE_MAP` (default prefix).

### Pipeline Pause / Resume
- Phase 1: create `PIPELINE_PAUSED` file at repo root → runner exits gracefully.
- Phase 2: disable `phase2-outreach.yml` from GitHub Actions tab.
- Manual Phase 2 trigger: GitHub Actions tab → phase2-outreach.yml → Run workflow.
