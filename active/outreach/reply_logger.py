"""
reply_logger.py — IMAP inbound reply poller.
Polls the sender inbox for replies from leads.
Matches replies to known lead emails from outreach_log.
Logs matches to Outreach Reply Log sheet and updates lead status to replied.
"""

import email
import email.message
import imaplib
import logging
import os
from datetime import datetime, timezone
from email.header import decode_header

logger = logging.getLogger(__name__)

from config import FOLLOWUP_DELAY_DAYS

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT") or "993")
IMAP_USER = os.getenv("IMAP_USER", os.getenv("GMAIL_SENDER", ""))
IMAP_PASS = os.getenv("IMAP_PASS", os.getenv("SMTP_PASS", ""))
POLL_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
# Poll one day past the configured follow-up gap so any reply that arrived anywhere
# within it always halts the sequence before the next touch. Derived from the repo's
# FOLLOWUP_DELAY_DAYS variable (not hardcoded). `or` guards an empty-string env
# injection (v9 lesson).
_DEFAULT_POLL_DAYS = FOLLOWUP_DELAY_DAYS + 1
POLL_DAYS_BACK = int(os.getenv("REPLY_POLL_DAYS_BACK") or _DEFAULT_POLL_DAYS)


def _decode_header_value(raw: str) -> str:
    parts = decode_header(raw or "")
    decoded = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _get_snippet(msg: email.message.Message, max_chars: int = 200) -> str:
    """Extract plain text snippet from email body."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")[:max_chars]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")[:max_chars]
    return ""


def run_reply_logger() -> dict:
    """
    Poll IMAP inbox for replies from leads.
    Matches sender email against outreach_log.
    Logs matches to Outreach Reply Log and updates lead status.
    Returns stats dict.
    """
    from sheets_client import (
        get_all_lead_emails_from_log,
        append_reply_log,
        update_lead_status,
    )
    from config import STATUS_REPLIED

    stats = {"replies_found": 0, "matched": 0, "errors": 0}

    known_emails = get_all_lead_emails_from_log()
    if not known_emails:
        logger.info("[REPLY LOGGER] No outreach log emails to match against.")
        return stats

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select(POLL_FOLDER)
    except Exception as e:
        logger.error(f"[REPLY LOGGER] IMAP connection failed: {e}")
        stats["errors"] += 1
        return stats

    try:
        # Search for unseen messages in the last POLL_DAYS_BACK days
        from datetime import timedelta
        since_date = (datetime.now(timezone.utc) - timedelta(days=POLL_DAYS_BACK)).strftime("%d-%b-%Y")
        result, data = mail.search(None, f'(SINCE "{since_date}")')
        if result != "OK":
            logger.warning("[REPLY LOGGER] IMAP search returned non-OK status.")
            return stats

        message_ids = data[0].split()
        logger.info(f"[REPLY LOGGER] Found {len(message_ids)} messages since {since_date}.")

        for msg_id in message_ids:
            try:
                result, msg_data = mail.fetch(msg_id, "(RFC822)")
                if result != "OK":
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                from_raw = msg.get("From", "")
                from_email = ""
                if "<" in from_raw and ">" in from_raw:
                    from_email = from_raw.split("<")[-1].rstrip(">").strip().lower()
                else:
                    from_email = from_raw.strip().lower()

                if from_email not in known_emails:
                    continue

                stats["replies_found"] += 1
                subject = _decode_header_value(msg.get("Subject", ""))
                date_str = msg.get("Date", "")
                snippet = _get_snippet(msg)

                append_reply_log({
                    "lead_email": from_email,
                    "lead_name": "",
                    "reply_date": date_str,
                    "subject": subject,
                    "snippet": snippet,
                })
                update_lead_status(from_email, STATUS_REPLIED)
                stats["matched"] += 1
                logger.info(f"[REPLY LOGGER] Reply logged from: {from_email}")

            except Exception as e:
                logger.error(f"[REPLY LOGGER] Error processing message {msg_id}: {e}")
                stats["errors"] += 1

    finally:
        try:
            mail.logout()
        except Exception:
            pass

    logger.info(f"[REPLY LOGGER] Done. Found: {stats['replies_found']}, Matched: {stats['matched']}, Errors: {stats['errors']}")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_reply_logger()
    print(result)
