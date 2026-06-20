# SCHEMA.md — Data Models
> Source of truth for all Google Sheets column definitions.
> Update here whenever columns are added or reordered.

---

## Leads Tab

| Index | Field Name       | Type    | Notes                                              |
|-------|------------------|---------|----------------------------------------------------|
| 0     | name             | string  | Full name. Never overwritten by Apify.             |
| 1     | email            | string  | Lowercase. Primary dedup key. Mandatory.           |
| 2     | company          | string  | Company name.                                      |
| 3     | region           | string  | Lowercase (au, nz, us, uk, ca, ie). Routes template series. |
| 4     | warmth_score     | integer | 0–10. Set at discovery. Not updated by pipeline.   |
| 5     | status           | string  | State machine value (see below).                   |
| 6     | last_contacted   | string  | ISO 8601 UTC timestamp. Set on each send.          |
| 7     | followup_count   | integer | Increments on each successful send.                |
| 8     | notes            | string  | Free text. Manual use only.                        |
| 9     | facebook_url     | string  | Facebook profile/page URL. Auto-filled by Step 3.5 (Serper). |
| 10    | linkedin_url     | string  | LinkedIn /in/ profile URL. Auto-filled by Step 3.5 (Serper). |

**Status values:**
```
new           Phase 1 write: dedup passed, ready for Touch 1
outreach_sent Phase 2 write: Touch 1 sent
followup_sent Phase 2 write: a follow-up touch sent (Touch 2..MAX_FOLLOWUPS)
replied       Reply logger or manual: lead responded
closed        followup_count >= MAX_FOLLOWUPS or manually closed
failed        Send error — auto-reset on next Phase 2 run
```

---

## outreach_log Tab

| Index | Field Name     | Type    | Notes                                             |
|-------|----------------|---------|---------------------------------------------------|
| 0     | lead_email     | string  | Foreign key → Leads.email                         |
| 1     | lead_name      | string  | Snapshot at send time.                            |
| 2     | sequence_type  | string  | Template prefix (e.g. touch-aunz, touch-standard) |
| 3     | stage_number   | integer | Touch number: 1..MAX_FOLLOWUPS (currently 1-4).   |
| 4     | email_subject  | string  | Rendered subject line.                            |
| 5     | sent_date      | string  | ISO 8601 UTC timestamp.                           |
| 6     | status         | string  | "sent" or "failed".                               |

**Dedup key:** `(lead_email, stage_number)` — never write duplicate pairs.

---

## Outreach Reply Log Tab

| Index | Field Name | Type   | Notes                             |
|-------|------------|--------|-----------------------------------|
| 0     | lead_email | string | Matched from inbound reply.       |
| 1     | lead_name  | string | From Leads tab lookup.            |
| 2     | reply_date | string | ISO 8601 UTC timestamp.           |
| 3     | subject    | string | Reply email subject line.         |
| 4     | snippet    | string | First 200 chars of reply body.    |

**Written by:** `reply_logger.py` only. Read-only from pipeline perspective.

---

## Region → Template Series Routing

```python
REGION_TEMPLATE_MAP = {
    "au": "touch-aunz",
    "nz": "touch-aunz",
    "us": "touch-standard",
    "uk": "touch-standard",
    "ca": "touch-standard",
    "ie": "touch-standard",
}
DEFAULT_TEMPLATE_PREFIX = "touch-standard"
```

Template file format: `{prefix}-{touch_number}.txt`
Line 1 = subject. Lines 2+ = body. Placeholders: `{{name}}`, `{{company}}`.
