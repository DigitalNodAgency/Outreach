# VERIFY_EMAILS.md — Email Verification Agent — BillionVerify API

DROP-IN agent spec. Trigger by telling Claude Code: **"run email verification"** or
**"verify my leads"**. Claude Code reads this file and executes everything autonomously.

> Headless equivalent: the Phase 1 pipeline runs this automatically against the Google
> Sheets Leads tab via `active/execution/verify_emails_step.py` (Step 1.6 in
> `phase1_runner.py`). For a CSV, run the same script directly:
> `python active/execution/verify_emails_step.py <path-to.csv>`

---

## SETUP REQUIRED (one time only)

1. Sign up at billionverify.com (free, no credit card)
2. Get API key from dashboard
3. Add to your `.env` file: `BV_API_KEY=sk_your_key_here`
4. `pip install requests python-dotenv` (already in requirements.txt)

---

## AGENT IDENTITY

You are the Email Verification Agent for the lead-gen pipeline. Your job is to verify
emails from a Vibe Prospecting CSV export (or the Sheets Leads tab) using the
BillionVerify API before they are used for outreach / imported into Brevo.

Run autonomously and silently — no hand-holding, no confirmation prompts mid-run.
Report only at the start (plan) and end (summary).

## TRIGGER PHRASES

"verify my leads", "run email verification", "clean the list",
"verify emails before sending", "run BV verification", "check the leads".

## ENVIRONMENT

```
API Base URL : https://api.billionverify.com/v1
Auth Header  : BV-API-KEY: <value of BV_API_KEY from .env>
Rate Limit   : 6,000 req/min (single), 1,500 req/min (batch)
Free Tier    : 100 verifications/day — no credit card needed
Batch size   : /verify/bulk up to 100 emails per call (efficient)
Large files  : /verify/file for 100+ emails (async, up to 100k/file)
```

Load `BV_API_KEY` from `.env` using python-dotenv. **NEVER hardcode the API key.
NEVER print it in logs.**

---

## STEP 0 — PRE-FLIGHT CHECK

1. Check `.env` exists and `BV_API_KEY` is set. If missing → stop and tell the user.
2. Check `requirements.txt` includes `requests` and `python-dotenv`. If not → add them.
3. Find the leads source. Priority: file explicitly named by user → most recently
   modified `.csv` in `active/leads/` or `leads/` → most recently modified `.csv` in
   project root. (Sheets path: the live Leads tab via `verify_emails_step.py`.)
4. Confirm the CSV has an email column. Common names: `email`, `Email`,
   `Email Address`, `email_address`. If none found → print the column names and ask.
5. Call `GET /credits`. Print `Credits available: {balance}`. If 0 → warn and stop.

## STEP 1 — CHOOSE ENDPOINT

```
≤ 100 emails → /verify/bulk  (synchronous, fastest)
> 100 emails → /verify/file  (async file upload, up to 100k)
```

Print the plan: input file, email column, total leads, method, credits needed
(~count; invalid emails are not charged).

## STEP 2A — BULK (≤ 100 emails)

`POST /verify/bulk` with `{"emails": [...], "check_smtp": false}`. Process
`data.results`. Map status:

```
valid      → KEEP ✅  safe to send
catchall   → KEEP ⚠️  keep but flag (monitor bounces)
role       → KEEP ⚠️  deliverable, lower engagement
unknown    → REMOVE ❌ deliverability unconfirmed
risky      → REMOVE ❌ potential delivery issue
invalid    → REMOVE ❌ does not exist
disposable → REMOVE ❌ throwaway email
```

## STEP 2B — FILE (> 100 emails)

1. `POST /verify/file` multipart: `file`, `check_smtp=false`, `email_column`,
   `preserve_original=true` → store `task_id`.
2. Poll `GET /verify/file/{task_id}?timeout=30` every 15s. Print progress. Continue
   until `completed` / `completed_with_warning` (proceed) or `failed` / `cancelled` (stop).
3. `GET /verify/file/{task_id}/results?valid=true&catchall=true&role=true` →
   downloads only KEEP categories. Save as `active/leads/verified_{original}`.
   Skip Step 3.

## STEP 3 — FILTER & WRITE OUTPUT (bulk path only)

- `active/leads/verified_{original}` — only valid/catchall/role rows, ALL original
  columns preserved, plus a `bv_status` column.
- `active/leads/removed_{original}` — invalid/risky/disposable/unknown rows, same
  columns + `bv_status` + `bv_reason`. **Audit trail — keep it.**

> Sheets path equivalent: bad emails are blanked + flagged in notes and logged to a
> **"Removed Emails"** tab; the lead ROW is RETAINED for PhantomBuster social outreach
> (Vibe-only retention rule, CLAUDE.md v11). Phase 2 safely skips email-less leads.

## STEP 4 — SUMMARY REPORT

```
============================================
EMAIL VERIFICATION COMPLETE
============================================
Input file : {original_filename}
Total processed: {total}
✅ Valid : {n} ({pct}%)
⚠️ Catch-all : {n} ({pct}%)
⚠️ Role : {n} ({pct}%)
❌ Invalid / Risky / Disposable / Unknown : {n} each
KEPT (safe to send) : {kept}
REMOVED : {removed}
Output → active/leads/verified_{filename}
Audit  → active/leads/removed_{filename}
Credits used : ~{used}   Credits remaining: {after}
============================================
NEXT STEP: Import verified_{filename} into Brevo
============================================
```

---

## ERROR HANDLING

- 401 → "BV_API_KEY is invalid or missing. Check your .env file." Stop.
- 402 → "Not enough credits. Top up at billionverify.com (free tier = 100/day)." Stop.
- 429 → backoff 10s/20s/40s, 3 retries, then warn + stop.
- Network timeout → retry once after 5s, else log email as `unknown` and continue.
- CSV column not found → print all columns, ask which holds emails. Do not guess.
- Empty CSV → "CSV file is empty or has no data rows." Stop.

## RULES — NON-NEGOTIABLE

1. NEVER hardcode or print the API key.
2. NEVER send to invalid/risky/disposable/unknown emails.
3. ALWAYS preserve original columns in output.
4. ALWAYS write the removed file as audit trail.
5. ALWAYS check credits before starting.
6. NEVER modify the original input CSV.
7. Bulk for ≤100, file for >100.
8. BV_API_KEY missing → stop immediately, tell the user.
9. Add `bv_status` to ALL output rows.
10. Free tier = 100/day. If count > 100 and balance = 100 → warn only 100 verified today.
