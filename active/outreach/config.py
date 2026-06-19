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
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")

# ── Notifications ──────────────────────────────────────────────────────────────
GMAIL_SENDER = _require("GMAIL_SENDER")
GMAIL_APP_PASSWORD = _require("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL = _require("NOTIFY_EMAIL")

# ── Outreach limits ────────────────────────────────────────────────────────────
DAILY_EMAIL_CAP = _int_env("DAILY_EMAIL_CAP", 300)
FOLLOWUP_DELAY_DAYS = _int_env("FOLLOWUP_DELAY_DAYS", 3)
MAX_FOLLOWUPS = _int_env("MAX_FOLLOWUPS", 3)
SEND_DELAY_SECONDS = _float_env("SEND_DELAY_SECONDS", 5)
SMTP_HEALTH_MIN_SENDS = _int_env("SMTP_HEALTH_MIN_SENDS", 5)
SMTP_HEALTH_FAIL_THRESHOLD = _float_env("SMTP_HEALTH_FAIL_THRESHOLD", 0.5)

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

# Social log tab
SOCIAL_LOG_HEADERS = [
    "lead_email", "lead_name", "platform", "profile_url", "sent_date", "status", "notes", "touch_number"
]

# Removed Emails tab — audit trail for BillionVerify-rejected addresses.
REMOVED_EMAILS_HEADERS = [
    "email", "name", "company", "bv_status", "bv_reason", "removed_date"
]

# ── Region → template series routing ──────────────────────────────────────────
# Format: region_value_in_sheet (lowercase) → template prefix
REGION_TEMPLATE_MAP = {
    "au": "touch-aunz",
    "nz": "touch-aunz",
    "us": "touch-standard",
    "uk": "touch-standard",
    "ca": "touch-standard",
    "ie": "touch-standard",
}
DEFAULT_TEMPLATE_PREFIX = "touch-standard"

# ── Lead status values ─────────────────────────────────────────────────────────
STATUS_NEW = "new"
STATUS_OUTREACH_SENT = "outreach_sent"
STATUS_FOLLOWUP_SENT = "followup_sent"
STATUS_REPLIED = "replied"
STATUS_CLOSED = "closed"
STATUS_FAILED = "failed"

# ── Discovery ─────────────────────────────────────────────────────────────────
MAX_LEADS_PER_RUN = _int_env("MAX_LEADS_PER_RUN", 100)
STRUCTURING_BATCH_SIZE = 10

# ── PhantomBuster (social outreach) ───────────────────────────────────────────
PHANTOMBUSTER_API_KEY = os.getenv("PHANTOMBUSTER_API_KEY", "")
PHANTOMBUSTER_LI_PHANTOM_ID = os.getenv("PHANTOMBUSTER_LI_PHANTOM_ID", "")
PHANTOMBUSTER_LI_SESSION_COOKIE = os.getenv("PHANTOMBUSTER_LI_SESSION_COOKIE", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ── ICP filters (filled after client onboarding) ───────────────────────────────
ICP_PERSONA = os.getenv("ICP_PERSONA", "[CLIENT_ICP_PERSONA]")
ICP_COMPANY_SIZE = os.getenv("ICP_COMPANY_SIZE", "[CLIENT_COMPANY_SIZE]")
ICP_INDUSTRIES = os.getenv("ICP_INDUSTRIES", "[CLIENT_INDUSTRIES]")
ICP_REGIONS = os.getenv("ICP_REGIONS", "[CLIENT_REGIONS]")
ICP_DISQUALIFY = os.getenv("ICP_DISQUALIFY", "[CLIENT_DISQUALIFY_CONDITIONS]")

# ── File paths (absolute — safe regardless of working directory) ───────────────
VIBE_EXPORT_CSV = str(_ROOT / "active" / "leads" / "vibe_export.csv")
RUN_METRICS_TSV = str(_ROOT / "active" / "leads" / "run_metrics.tsv")
SOURCE_HEALTH_JSON = str(_ROOT / "active" / "leads" / "source_health.json")
FAILED_RECORDS_JSONL = str(_ROOT / "active" / "leads" / "failed_records.jsonl")
PIPELINE_ERRORS_JSONL = str(_ROOT / "active" / "leads" / "pipeline_errors.jsonl")
TEMPLATE_METRICS_TSV = str(_ROOT / "active" / "leads" / "template_metrics.tsv")
PIPELINE_PAUSED_FLAG = str(_ROOT / "PIPELINE_PAUSED")
