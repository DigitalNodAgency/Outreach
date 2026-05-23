# tasks/lessons.md — Self-Improvement Protocol
> Created by Claude Code. Updated after every correction or mistake.
> Review this file at the start of every session before doing anything.

---

## How This File Works

After ANY correction from the user, Claude Code must:
1. Identify the pattern behind the mistake
2. Write a rule that prevents the same mistake
3. Add it below under the relevant category
4. Ruthlessly iterate until mistake rate drops

Rules must be specific. "Be more careful" is not a rule. "Never do X without checking Y first" is.

---

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Minimal code impact.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes touch only what is necessary. Avoid introducing bugs.
- **No Hand-holding**: When given a bug, fix it. Point at logs, resolve autonomously.

---

## Pipeline-Specific Rules

**Enrichment discipline:**
- Never write a lead row to Sheets without a verified email unless the source is a manual assist record explicitly flagged for enrichment.
- Never overwrite discovery-sourced contact names (from Vibe or Prospeo). Only Serper-extracted names may be written, and only if _is_person_name() passes.

**Deduplication discipline:**
- Always fetch existing emails via get_existing_emails() before any write. Never trust in-memory state alone.
- Log every skipped duplicate with reason to pipeline_errors.jsonl. Never silently drop.

**Sheets quota discipline:**
- Never write rows one at a time in a loop. Always batch via append_rows() single API call.
- Per-row fallback exists, but only triggers if the batch call fails. Not a first-choice path.

**Source health discipline:**
- Always check source_health.json before attempting discovery. Skip sources with 0 leads_returned in last 2 consecutive runs.
- Log every skipped source as a warning to pipeline_errors.jsonl.

**Outreach discipline:**
- Never send Touch 2 or 3 unless last_contacted is at least FOLLOWUP_DELAY_DAYS ago.
- Never send to a lead with status=replied or status=closed.
- Always check outreach_log cache before every append_outreach_log() call. No duplicate (email, stage_number) pairs.

**Error discipline:**
- Never halt the pipeline on a single enrichment failure. Log to pipeline_errors.jsonl, continue with remaining batch.
- Never fabricate enrichment results. Zero results is preferable to invented data.
- Always surface errors in the Phase 1 and Phase 2 summary emails to the operator.

---

## Learned Rules
> Claude Code adds entries here as the project runs. Start empty, grows with the project.

**[2026-05-23] Vibe MCP `export-to-csv` is not a programmable download endpoint**
Returns `app.vibeprospecting.ai/lists?dataset_id=...` — a React SPA portal URL. No auth header
combination fixes it. Use `api.explorium.ai/v1` REST API directly for any programmatic access.
Rule: Never call Vibe MCP export-to-csv expecting a file. Use REST API only.

**[2026-05-23] Explorium REST API request format**
`mode: "full"` is required at root level. All filter params must be nested under a `"filters"` key.
`has_email` filter uses `{"value": True}`, not `{"exists": True}`.
422 "field required: mode" = missing root `mode` field.
Rule: Always include `mode:"full"` and `"filters":{}` wrapper before adding any filter params.

**[2026-05-23] Credit conservation during API debugging**
Set `target=1` for every diagnostic run. Revert to `target=MAX_LEADS_PER_RUN` only after
confirming a working lead is written to Sheets. Never test filter changes at full volume.
Rule: First working run at target=1, then scale.

**[2026-05-23] Prospeo `/search-person` is enrichment-only, not discovery**
Requires specific identifiers (name + company domain), not ICP-style filter params.
Sending ICP filters (seniority, industry, location) to it returns 400 "Field required".
Rule: Never use Prospeo for discovery. It is T0 enrichment only.

---

## Mistake Patterns to Watch
> Claude Code updates this when a pattern repeats more than once.

[No patterns recorded yet]

---
