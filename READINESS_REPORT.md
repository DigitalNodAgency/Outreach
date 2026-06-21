# Cold Outbound Pre-Flight Readiness Report

**Domain:** digitalnod.net  **Platform:** Brevo (direct API/SMTP relay)  **Date:** 2026-06-21
**Targets:** >95% delivered · <2% bounce · <1% blocked · <0.1% complaints

## GATE DECISION: 🔴 HOLD — DO NOT SEND

The send job has **not** been triggered (schedules remain paused). Code is hardened and the
variation engine is live, but **3 compulsory items require manual action** before any lead enters
the queue. Clear them, then re-run this report.

| # | Section | Verdict |
|---|---------|---------|
| 1 | Infrastructure audit | ✅ PASS |
| 2 | Volume & cadence | 🟡 CODE READY — needs `WARMUP_START_DATE` |
| 3 | List hygiene (primary gate) | 🟡 CODE READY — **needs `BV_API_KEY`** |
| 4 | Copy / content | 🔴 FAIL — **CAN-SPAM footer missing** (spam words resolved) |
| 5 | Warm-up decision | ✅ RECOMMENDATION ISSUED (warm up) |
| 6 | Template variation engine | ✅ PASS (built) |
| 7 | Final gate | 🔴 HOLD until §2/§3/§4 manual items clear |

---

## 1 — Infrastructure Audit ✅ PASS

| Check | Result | Detail |
|-------|--------|--------|
| DKIM | ✅ | `brevo1/brevo2._domainkey` CNAMEs resolve to `b1/b2.digitalnod-net.dkim.brevo.com`; `brevo-code` TXT confirms the domain is authenticated in Brevo. DMARC aligns via DKIM. |
| DMARC | ✅ | `_dmarc.digitalnod.net` = `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com`. Meets "≥ p=none with rua reporting active." |
| SPF | ℹ️ Not flagged | `v=spf1 include:transmail.net.in include:spf.protection.outlook.com -all` (ZeptoMail + Microsoft 365; Brevo not included). Per platform rule: **not an error** — Brevo's API/relay uses its own envelope-from, so SPF passes on Brevo's side and DKIM+DMARC carry authentication. Optional belt-and-suspenders: add `include:spf.brevo.com` only if you later send via Brevo SMTP with envelope alignment. |
| Custom tracking domain | ✅ by design | No tracking subdomain configured — and the engine sends **plain-text with open/click tracking disabled** (`smtp_client.py`), so no ESP shared-tracking domain is exposed. **Manual gate:** if you ever enable open/click tracking in Brevo, configure a custom tracking CNAME *first*. |
| Blacklists | ✅ Clean | Spamhaus DBL: not listed. SURBL: not listed. (Barracuda/SORBS are IP-reputation lists; on Brevo's shared IP pool the IP isn't yours to control and Brevo manages pool reputation — domain-level lists are the relevant baseline, and they're clean.) |

---

## 2 — Volume & Cadence 🟡 CODE READY (one manual switch)

**Auto-applied fixes:**
- **Randomized 5-min-minimum send gaps** — was a *fixed 5-second* gap. Now each lead send sleeps a
  random `uniform(MIN_SEND_GAP_SECONDS=300, MAX_SEND_GAP_SECONDS=480)` = **5–8 min, no fixed interval**
  (`smtp_client.py`). Operator notifications skip this (`pace=False`).
- **Warm-up ramp aligned** to the runbook: default `WARMUP_SCHEDULE = 25,35,50,75,100,150,200`
  (1 rung / 7 days) → **wk1 ≈ 25/day, wk3 = 50/day**, then climbs gradually before settling at
  `DAILY_EMAIL_CAP`. Verified: with the ramp on, day-0 effective cap = **25**.
- **Workflow timeout raised** 90 → 300 min (paced sends are long-running).

**🔴 COMPULSORY MANUAL:** set repo/secret **`WARMUP_START_DATE=2026-06-XX`** (the day you go live).
Until it's set, the ramp is OFF and the cap is `DAILY_EMAIL_CAP` (300) — i.e. it would jump straight
to volume. Also confirm `DAILY_EMAIL_CAP` is *not* used to bypass the ramp during warm-up.

**⚠️ Architectural note (decide before scaling past ~40/day):** 5-min gaps × volume don't fit one
GitHub Actions job — a single job caps at 6 h, and ~200 idle-sleep minutes/day burns GHA minutes
(~6k/mo, over the free tier). At warm-up volume (≤30/day, ~4 h) it's fine. Before ramping higher,
split the daily send into multiple smaller cron runs, or move sending to an external worker.
Single mailbox in use (one `SMTP_FROM`); add mailboxes and distribute horizontally before scaling.

---

## 3 — List Hygiene Pipeline 🟡 CODE READY (needs API key) — PRIMARY GATE

**Auto-applied fixes** (`billionverify_client.classify` is now the single source of truth):
- **Role-based aliases → QUARANTINE by default** (`info@/admin@/support@…`) — was *kept*. Off via
  `BV_QUARANTINE_ROLE=false`. Stops warm-up volume burning on alias inboxes.
- **Catch-all → confidence-gated**, not auto-included — kept only if score ≥ `BV_CATCHALL_MIN_SCORE`
  (default 85; 0–1 scores normalized ×100; missing score → quarantine). Was *kept unconditionally*.
- **Reject invalid/disposable/risky/unknown** → REMOVE (confirmed).
- **Per-lead logging** — KEEP leads flagged `bv:<status>:<score>` in notes; QUARANTINE/REMOVE logged
  to the **"Removed Emails"** audit tab with status + reason + score (traceable: data vs infra).
- **Suppression / do-not-contact cross-check** — NEW. `get_suppression_set()` reads a **"Suppression"**
  tab (col A = email, col B = domain); `outreach_engine` skips + closes any suppressed lead **before
  the send queue**, on both Touch 1 and follow-ups. Absent tab = no-op (safe).

Hard-block behavior: bad addresses are blanked at the Sheets layer so Phase 2 skips the email touch;
the row is retained for social outreach (honors the v11 retention rule).

**🔴 COMPULSORY MANUAL:**
1. **Set `BV_API_KEY`** (secret). Without it verification is skipped gracefully — meaning **leads would
   enter the queue unverified**. The entire primary gate hinges on this key.
2. **MCP vs direct API:** no BillionVerify MCP server is available in this environment. The hardened
   direct-API client implements the full hard-block. If a BV MCP Server/Agent Skill exists, provide its
   endpoint/creds to wire it; otherwise confirm the direct-API client is acceptable.

---

## 4 — Copy / Content Audit 🔴 FAIL (CAN-SPAM) + ⚠️ spam words

| Check | Result |
|-------|--------|
| Inline images | ✅ None (plain-text only). |
| Link count (cap 1) | ✅ **0 links** in all 4 email touches (reply-based CTAs). |
| Spam trigger words | ✅ **RESOLVED** — "guaranteed" → "real" and "free [call/consultation]" → "[call/consultation]" applied across all 3 flat templates + 3 variant JSONs (touch-standard-1/2/3). "77%" stat in subjects retained (data-driven, not a spam pattern). |
| CAN-SPAM (unsubscribe + physical address) | 🔴 **FAIL** — **absent from all templates.** No working unsubscribe, no postal address. |
| Variation engine wired & producing variants | ✅ Built & wired (see §6). |

**🔴 COMPULSORY MANUAL:** add a **CAN-SPAM footer** (working unsubscribe + real physical postal
address). Recommended at the Brevo sender/account level so it auto-appends to every send. (This is
already on the Mohit action list in `UPDATE_FOR_MOHIT.md` — it's now a hard send-gate.)

---

## 5 — Warm-Up Decision ✅ RECOMMENDATION: WARM UP FROM SCRATCH

The domain has prior sends, but to a **different ICP and a different offer**. Reputation tracks the
*current sending pattern and recipient engagement*, not just domain age — a new audience + new copy
resets the signals that matter (opens/replies/complaints against this content). **Recommendation:**

- Run a **standard from-scratch warm-up** on the §2 cadence regardless of list-verification status.
- **Tracking disabled** during week 1 (already the default — plain-text, no pixels/links).
- **Do not greenlight full cold volume until ≥ 2 weeks of clean sending history** exist on this domain
  for the new ICP. Watch bounce <2% / blocked <1% / complaints <0.1% before each rung up.

---

## 6 — Phase 2 Template Variation Engine ✅ PASS (BUILT)

`active/outreach/variation_engine.py` + `active/outreach/templates/variants/touch-standard-{1..4}.json`.

- **Rotates** subject line, opening hook, proof point, and CTA **per send**, *within the proven
  structure* of each touch — language varies inside the formula, no unrelated copy invented. Variants
  are faithful paraphrases of the existing approved copy (no new claims/numbers, no em dashes, only
  engine-supported `{{tokens}}`).
- **Source/formula tagged** per variant (e.g. Touch 1 = "Social-Proof + PAS + Reply-CTA"; Touch 4 =
  "Breakup") and recorded with a `variant_id`.
- **No two leads in a batch get identical copy** — a unique slot-combination is assigned per lead
  (Touch 1 = 192 combos: 4×4×3×4). Verified: 6 leads → 6 unique bodies.
- **Threading preserved** — follow-ups thread off each lead's *exact* Touch-1 subject (`Re:`); the
  breakup keeps its own fresh subject.
- **Rule-based, no runtime LLM** (respects architecture decision #3).
- **Variant log** at `logs/variant_log.jsonl` (timestamp, email, touch, source, formula, variant_id,
  indices, subject), uploaded alongside the send log as a GitHub artifact.
- Graceful fallback: if a touch has no variant file, the flat `.txt` template is used.

---

## 7 — Final Gate & Action List

**Compulsory before send (all must be green):**
1. 🔴 **`BV_API_KEY`** set — §3 (verification gate is inert without it).
2. 🔴 **CAN-SPAM footer** (unsubscribe + physical address) on the Brevo sender — §4.
3. 🔴 **`WARMUP_START_DATE`** set so the ramp is active and volume doesn't jump — §2.

**Strongly recommended (not hard-blocking):**
- Decide BillionVerify MCP vs direct API (§3).
- Register for feedback loops / Google Postmaster Tools + add the client's own `rua` to DMARC.
- Populate the **Suppression** tab with any known do-not-contact addresses/domains (§3).
- Plan the multi-run / external-worker split before scaling past ~40/day (§2).

**Auto-applied code changes (this pass):** `config.py`, `smtp_client.py`, `outreach_engine.py`,
`sheets_client.py`, `main.py`, `billionverify_client.py`, `verify_emails_step.py`,
`phase2-outreach.yml`, new `variation_engine.py` + 4 variant JSONs, `VERIFY_EMAILS.md` policy update.
All 8 Python files byte-compile; variation/classify/warm-up/suppression verified by smoke test.
**No send was triggered.**
