"""
config.py — Central configuration and environment loader.
Loads from .env (local) or environment variables (GitHub Actions secrets).
Hard fails on missing critical credentials.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

_ROOT = Path(__file__).resolve().parents[2]


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"[CONFIG] Missing required env var: {key}")
    return val


def _int_env(key: str, default: int) -> int:
    """int() of an env var, treating an empty string the same as absent.
    GitHub Actions injects an *undefined* `${{ vars.X }}` as "" (not missing),
    so `int(os.getenv(key, default))` would hit `int("")` and crash the whole
    run at import. `or default` falls back safely. See CLAUDE.md §12."""
    return int(os.getenv(key) or default)


def _float_env(key: str, default: float) -> float:
    """float() of an env var, empty-string-safe (see _int_env)."""
    return float(os.getenv(key) or default)


# ── Google Sheets ──────────────────────────────────────────────────────────────
SPREADSHEET_ID = _require("SPREADSHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = _require("GOOGLE_SERVICE_ACCOUNT_JSON")

# ── BillionVerify (email verification, Phase 1) ────────────────────────────────
# Soft dependency: if BV_API_KEY is unset the verification step is skipped
# gracefully (mirrors the Serper social-enrichment soft-dependency).
BV_API_KEY = os.getenv("BV_API_KEY", "").strip()

# ── Brevo SMTP ─────────────────────────────────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = _int_env("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
# Optional Reply-To header for outgoing touches. Empty = header omitted (replies go to
# SMTP_FROM's inbox). INVARIANT: whichever address ends up receiving replies — REPLY_TO
# if set, else SMTP_FROM — must be the SAME mailbox the reply poll logs into (IMAP_USER),
# or replies are never detected and follow-ups keep going out after a lead answers.
REPLY_TO_EMAIL = (os.getenv("REPLY_TO") or "").strip()
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")

# ── Notifications ──────────────────────────────────────────────────────────────
GMAIL_SENDER = _require("GMAIL_SENDER")
GMAIL_APP_PASSWORD = _require("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL = _require("NOTIFY_EMAIL")

# ── Outreach limits ────────────────────────────────────────────────────────────
DAILY_EMAIL_CAP = _int_env("DAILY_EMAIL_CAP", 300)
FOLLOWUP_DELAY_DAYS = _int_env("FOLLOWUP_DELAY_DAYS", 3)
# Total touches per lead = highest touch-standard-{N}.txt. Touch 1 counts as
# followup_count=1, so MAX_FOLLOWUPS=5 ⇒ touches 1-5 (v15 PR white-label sequence,
# all threaded off Touch 1 — no breakup touch). GitHub repo variable overrides this
# default. Raising it only needs a matching touch-standard-{N}.txt; a missing file ends
# the sequence gracefully (engine closes the lead).
MAX_FOLLOWUPS = _int_env("MAX_FOLLOWUPS", 5)
SEND_DELAY_SECONDS = _float_env("SEND_DELAY_SECONDS", 5)
SMTP_HEALTH_MIN_SENDS = _int_env("SMTP_HEALTH_MIN_SENDS", 5)
SMTP_HEALTH_FAIL_THRESHOLD = _float_env("SMTP_HEALTH_FAIL_THRESHOLD", 0.5)

# ── Inter-send pacing (cold-outbound anti-spam cadence) ────────────────────────
# Cold sends must NOT go out on a fixed metronome. After each lead send the engine
# sleeps a RANDOM duration in [MIN_SEND_GAP_SECONDS, MAX_SEND_GAP_SECONDS]. The
# minimum is 5 minutes (300s) per the cold-outbound runbook; the max adds jitter so
# the spacing is irregular. These apply to lead outreach only — operator
# notifications (send_plain) use the short SEND_DELAY_SECONDS instead.
# NOTE: at 5-8 min/send a daily batch is long-running. Keep the warm-up cap and the
# phase2 workflow `timeout-minutes` in sync (≈ cap × MAX_SEND_GAP_SECONDS ÷ 60).
MIN_SEND_GAP_SECONDS = _int_env("MIN_SEND_GAP_SECONDS", 300)
MAX_SEND_GAP_SECONDS = _int_env("MAX_SEND_GAP_SECONDS", 480)

# ── Run time budget (graceful stop before the workflow's hard timeout) ─────────
# PHASE2_TIMEOUT_MINUTES drives BOTH the phase2 workflow's `timeout-minutes` and
# this soft budget (one repo variable, one knob — keep the 180 fallback here in
# sync with the YAML's `|| 180`). The engine stops STARTING sends
# TIME_BUDGET_SAFETY_MINUTES before the hard kill so cleanup, the summary email,
# and the Brevo post-sync always run and the job exits 0; unsent leads simply go
# out on the next daily run. The budget also bounds effective_daily_cap()
# (smtp_client._budget_send_ceiling), so the warm-up ramp / DAILY_EMAIL_CAP can
# never outgrow the send window — no recurring timeout bumps needed as the ramp
# climbs.
PHASE2_TIMEOUT_MINUTES = _int_env("PHASE2_TIMEOUT_MINUTES", 180)
# Covers job overhead outside Python (checkout + pip install), one in-flight send
# iteration (send + pacing gap + Sheet writes), and all post-loop steps.
TIME_BUDGET_SAFETY_MINUTES = 15
RUN_TIME_BUDGET_SECONDS = max(PHASE2_TIMEOUT_MINUTES - TIME_BUDGET_SAFETY_MINUTES, 10) * 60

# ── Cold-send warm-up ramp ─────────────────────────────────────────────────────
# Protects domain reputation by ramping the daily send cap up gradually, then
# settling at DAILY_EMAIL_CAP. The ramp is OFF (effective cap == DAILY_EMAIL_CAP)
# unless WARMUP_START_DATE is set to an ISO date (yyyy-mm-dd). WARMUP_SCHEDULE is
# the per-rung daily limit; each rung lasts WARMUP_STEP_DAYS days. All env-
# overridable so the numbers track repo variables, never hardcoded at the call site.
WARMUP_START_DATE = os.getenv("WARMUP_START_DATE", "").strip()
WARMUP_STEP_DAYS = _int_env("WARMUP_STEP_DAYS", 7)
# Per-rung daily cap, one rung per WARMUP_STEP_DAYS days. Default ramp matches the
# pre-flight runbook: week 1 ≈ 20-30/day, scaling toward 40-50/day by week 3, then
# climbing gradually before settling at DAILY_EMAIL_CAP. NEVER jump straight to volume.
WARMUP_SCHEDULE = [
    int(x) for x in (os.getenv("WARMUP_SCHEDULE") or "25,35,50,75,100,150,200").split(",")
    if x.strip().isdigit()
]

# ── BillionVerify decision policy (list-hygiene hard gate) ─────────────────────
# Catch-all addresses are NOT auto-included; they are kept only when BillionVerify's
# confidence score clears this threshold (0-100 scale; a 0-1 score is normalised ×100).
# A missing/unparseable score on a catch-all is treated as below threshold (quarantine).
BV_CATCHALL_MIN_SCORE = _float_env("BV_CATCHALL_MIN_SCORE", 85.0)
# Role-based / alias inboxes (info@, admin@, support@…) are quarantined by default so
# warm-up volume isn't burned on shared aliases. Set BV_QUARANTINE_ROLE=false to send them.
BV_QUARANTINE_ROLE = (os.getenv("BV_QUARANTINE_ROLE", "true").strip().lower() == "true")

# ── Landing page / booking ─────────────────────────────────────────────────────
CALENDLY_URL = os.getenv("CALENDLY_URL", "")
SENDER_NAME = os.getenv("SENDER_NAME", "")

# ── Template directory ─────────────────────────────────────────────────────────
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR") or str(_ROOT / "active" / "outreach" / "templates")

# ── Google Sheets column mappings ──────────────────────────────────────────────
# Leads tab (0-indexed)
COL_NAME = 0
COL_EMAIL = 1
COL_COMPANY = 2
COL_REGION = 3
COL_WARMTH_SCORE = 4
COL_STATUS = 5
COL_LAST_CONTACTED = 6
COL_FOLLOWUP_COUNT = 7
COL_NOTES = 8
COL_FACEBOOK_URL = 9
COL_LINKEDIN_URL = 10

LEADS_HEADERS = [
    "name", "email", "company", "region", "warmth_score",
    "status", "last_contacted", "followup_count", "notes",
    "facebook_url", "linkedin_url",
]

# outreach_log tab (0-indexed)
OLOG_LEAD_EMAIL = 0
OLOG_LEAD_NAME = 1
OLOG_SEQUENCE_TYPE = 2
OLOG_STAGE_NUMBER = 3
OLOG_EMAIL_SUBJECT = 4
OLOG_SENT_DATE = 5
OLOG_STATUS = 6

OUTREACH_LOG_HEADERS = [
    "lead_email", "lead_name", "sequence_type",
    "stage_number", "email_subject", "sent_date", "status"
]

# Outreach Reply Log tab
REPLY_LOG_HEADERS = [
    "lead_email", "lead_name", "reply_date", "subject", "snippet"
]

# Removed Emails tab — audit trail for BillionVerify-rejected addresses.
REMOVED_EMAILS_HEADERS = [
    "email", "name", "company", "bv_status", "bv_reason", "removed_date"
]

# Discovery State tab — durable per-ICP pagination cursor. Lives in the Sheet (not a
# local file) so it survives the ephemeral GitHub Actions filesystem and is shared
# across every run host. One row per filter_key (a hash of the resolved ICP filters).
DISCOVERY_STATE_HEADERS = [
    "filter_key", "offset", "total_results", "updated"
]

# ── Region → template series routing ──────────────────────────────────────────
# Format: region_value_in_sheet (lowercase) → template prefix.
# Client targets the USA only, so every region routes to the standard US series.
# Sheet region values are state/country names (e.g. "FLORIDA", "USA"); any value
# not listed here falls back to DEFAULT_TEMPLATE_PREFIX in _resolve_template_prefix.
REGION_TEMPLATE_MAP = {
    "us": "touch-standard",
    "usa": "touch-standard",
}
DEFAULT_TEMPLATE_PREFIX = "touch-standard"

# ── Lead status values ─────────────────────────────────────────────────────────
STATUS_NEW = "new"
STATUS_OUTREACH_SENT = "outreach_sent"
STATUS_FOLLOWUP_SENT = "followup_sent"
STATUS_REPLIED = "replied"
STATUS_CLOSED = "closed"
STATUS_FAILED = "failed"
# Terminal do-not-contact states set by the Brevo suppression sync (brevo_reconcile
# .sync_brevo_suppression). A lead in either never gets another touch. `unsubscribed`
# = recipient unsubscribed / spam-flagged / admin-blocked at Brevo; `bounced` = hard
# bounce. Both are ALSO written to the Suppression tab (durable, cross-run gate).
STATUS_UNSUBSCRIBED = "unsubscribed"
STATUS_BOUNCED = "bounced"

# ── Discovery ─────────────────────────────────────────────────────────────────
MAX_LEADS_PER_RUN = _int_env("MAX_LEADS_PER_RUN", 100)
STRUCTURING_BATCH_SIZE = 10

# ── ICP filters (filled after client onboarding) ───────────────────────────────
ICP_PERSONA = os.getenv("ICP_PERSONA", "[CLIENT_ICP_PERSONA]")
ICP_COMPANY_SIZE = os.getenv("ICP_COMPANY_SIZE", "[CLIENT_COMPANY_SIZE]")
ICP_INDUSTRIES = os.getenv("ICP_INDUSTRIES", "[CLIENT_INDUSTRIES]")
ICP_REGIONS = os.getenv("ICP_REGIONS", "[CLIENT_REGIONS]")
ICP_DISQUALIFY = os.getenv("ICP_DISQUALIFY", "[CLIENT_DISQUALIFY_CONDITIONS]")

# LinkedIn industry categories — the PRIMARY discovery industry filter (v17).
# Explorium permits only ONE of naics_category / linkedin_category / google_category
# per query, and the self-labeled LinkedIn category classifies agencies far more
# accurately than the inferred NAICS codes did: NAICS misfiled martech (e.g. TapClicks
# → 541613 "Marketing Consulting") and PR firms into the agency pool. "public relations
# and communications services" is its OWN LinkedIn category, so filtering on the two
# agency categories below naturally excludes PR competitors. Comma-separated; an empty
# injection (GitHub sends an undefined `${{ vars.X }}` as "") falls back to the default
# via `or` — see _int_env note. Editing this repo var re-scopes discovery with no code
# change (and auto-resets the discovery cursor, since the filter hash changes).
ICP_LINKEDIN_CATEGORIES = os.getenv("ICP_LINKEDIN_CATEGORIES") or "marketing services,advertising services"

# Secondary, CREDIT-FREE deny screen. Each keyword is matched (case-insensitive
# substring) against BOTH a prospect's COMPANY NAME and their JOB TITLE before any
# enrichment call; a match rejects the prospect (logged to failed_records.jsonl, never
# silently dropped). This is a backstop for the off-ICP record (staffing/recruiting/
# pure-tech company, or a solo consultant/fractional exec) that self-labeled into a
# marketing/advertising LinkedIn category — the linkedin_category filter above is the
# primary gate. The consultant/fractional/freelance tokens catch the individual buyer
# who is NOT an agency (e.g. a "Fractional CMO" or "Marketing Consultant" — an off-ICP
# consultant lead that reached the sheet, the failure this screen was hardened for).
# Keep tokens HIGH-PRECISION: they must not appear in legitimate digital/social agency
# names or agency-owner titles (e.g. "saas"/"software" are intentionally NOT defaults —
# "SaaS marketing agency" is in-ICP). Empty disables the screen.
ICP_DENY_KEYWORDS = os.getenv("ICP_DENY_KEYWORDS") or (
    "staffing,recruiting,recruitment,software development,web hosting,"
    "consultant,consulting,consultancy,fractional,freelance"
)

# ── File paths (absolute — safe regardless of working directory) ───────────────
VIBE_EXPORT_CSV = str(_ROOT / "active" / "leads" / "vibe_export.csv")
RUN_METRICS_TSV = str(_ROOT / "active" / "leads" / "run_metrics.tsv")
SOURCE_HEALTH_JSON = str(_ROOT / "active" / "leads" / "source_health.json")
FAILED_RECORDS_JSONL = str(_ROOT / "active" / "leads" / "failed_records.jsonl")
PIPELINE_ERRORS_JSONL = str(_ROOT / "active" / "leads" / "pipeline_errors.jsonl")
TEMPLATE_METRICS_TSV = str(_ROOT / "active" / "leads" / "template_metrics.tsv")
PIPELINE_PAUSED_FLAG = str(_ROOT / "PIPELINE_PAUSED")
