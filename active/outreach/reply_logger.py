"""
reply_logger.py — IMAP inbound reply poller + Reply Log reconcile sweep.
Polls the sender inbox for replies from leads.
Matches replies to known lead emails from outreach_log.
Logs matches to Outreach Reply Log sheet and updates lead status to replied.

The 'Outreach Reply Log' tab doubles as the operator kill-switch: the reconcile sweep
(run first, independent of IMAP) flips ANY still-active lead whose email appears in
column A to status=replied — so pasting a lead's email into the tab stops their
sequence on the next run, no code required.

INVARIANT: IMAP_USER must be the mailbox replies actually land in — the SMTP_FROM
inbox (or REPLY_TO's, if set). Polling any other mailbox means replies are never seen.
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
# Poll Gmail's All Mail by default so a reply that was archived, tab-filtered, or
# auto-labeled out of the Inbox is still caught (a reply in any non-INBOX folder used to be
# invisible). Falls back to INBOX at select() time if the folder can't be opened
# (non-Gmail host or a localized label). Override with IMAP_FOLDER.
POLL_FOLDER = os.getenv("IMAP_FOLDER", "[Gmail]/All Mail")
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
        get_reply_log_index,
        mark_leads_replied,
        append_reply_log,
        update_lead_status,
    )
    from config import STATUS_REPLIED

    stats = {
        "replies_found": 0, "matched": 0, "errors": 0,
        "reconciled": 0, "duplicates_skipped": 0, "status_update_failures": 0,
    }

    # Reconcile sweep FIRST, before any IMAP dependency: every address already in the
    # 'Outreach Reply Log' tab (auto-logged earlier, or pasted in manually — the tab is
    # the operator's kill-switch) gets its Leads row flipped to replied if still active.
    # This self-heals a silently failed status write, catches replies older than the
    # IMAP poll window, and must run even when IMAP auth is down. A sweep failure counts
    # as an error so main.py's fail-safe skips follow-ups (we could not honor the
    # kill-switch, so we must not chase Touch 2+).
    reply_log_dedup_keys: set[tuple[str, str, str]] = set()
    try:
        logged_emails, reply_log_dedup_keys = get_reply_log_index()
        if logged_emails:
            stats["reconciled"] = mark_leads_replied(logged_emails)
            if stats["reconciled"]:
                logger.info(
                    f"[REPLY LOGGER] Reconcile sweep: flipped {stats['reconciled']} still-active "
                    f"lead(s) to replied from the Reply Log tab."
                )
    except Exception as e:
        logger.error(f"[REPLY LOGGER] Reply Log reconcile sweep failed: {e}")
        stats["errors"] += 1

    known_emails = get_all_lead_emails_from_log()
    if not known_emails:
        logger.info("[REPLY LOGGER] No outreach log emails to match against.")
        return stats

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
    except Exception as e:
        logger.error(f"[REPLY LOGGER] IMAP connection failed: {e}")
        stats["errors"] += 1
        return stats

    # Open the poll folder read-only (never flip messages to seen). Fall back to INBOX if
    # the configured folder (default "[Gmail]/All Mail") can't be opened.
    folder_arg = POLL_FOLDER if POLL_FOLDER.upper() == "INBOX" else f'"{POLL_FOLDER}"'
    status, _ = mail.select(folder_arg, readonly=True)
    if status != "OK":
        logger.warning(f"[REPLY LOGGER] Could not open '{POLL_FOLDER}'; falling back to INBOX.")
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            logger.error("[REPLY LOGGER] Could not open INBOX either.")
            stats["errors"] += 1
            try:
                mail.logout()
            except Exception:
                pass
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

        unmatched_senders: set[str] = set()
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
                    if from_email:
                        unmatched_senders.add(from_email)
                    continue

                stats["replies_found"] += 1
                subject = _decode_header_value(msg.get("Subject", ""))
                date_str = msg.get("Date", "")
                snippet = _get_snippet(msg)

                # Dedup across runs: the SINCE window re-scans the same messages every
                # day, which used to re-append the same reply each run. Key matches what
                # append_reply_log writes (get_reply_log_index strips these columns).
                # A duplicate needs no status write either — the reconcile sweep above
                # already flipped any still-active lead listed in the tab.
                dedup_key = (from_email, date_str.strip(), subject.strip())
                if dedup_key in reply_log_dedup_keys:
                    stats["duplicates_skipped"] += 1
                    continue

                append_reply_log({
                    "lead_email": from_email,
                    "lead_name": "",
                    "reply_date": date_str,
                    "subject": subject,
                    "snippet": snippet,
                })
                reply_log_dedup_keys.add(dedup_key)
                if not update_lead_status(from_email, STATUS_REPLIED):
                    # Reply matched outreach_log but no Leads row has this email (e.g. the
                    # address was corrected after Touch 1). Not fatal — a lead absent from
                    # the Leads tab can't be sent follow-ups anyway — but surface it loudly:
                    # it means the Reply Log and Leads tab disagree about this address.
                    stats["status_update_failures"] += 1
                    logger.error(
                        f"[REPLY LOGGER] Reply logged but NO Leads row matched '{from_email}' — "
                        f"status not flipped. Check the Leads email column vs outreach_log."
                    )
                stats["matched"] += 1
                logger.info(f"[REPLY LOGGER] Reply logged from: {from_email}")

            except Exception as e:
                logger.error(f"[REPLY LOGGER] Error processing message {msg_id}: {e}")
                stats["errors"] += 1

        # Diagnostic: if nothing matched, show who IS in this mailbox. If your lead
        # addresses aren't among these, the poll is reading the wrong mailbox — check
        # GMAIL_SENDER / IMAP_USER against the address that receives replies (SMTP_FROM).
        if stats["matched"] == 0 and unmatched_senders:
            sample = sorted(unmatched_senders)[:15]
            logger.info(
                f"[REPLY LOGGER] 0 replies matched. {len(unmatched_senders)} distinct "
                f"senders seen in '{POLL_FOLDER}' (sample): {sample}"
            )

    finally:
        try:
            mail.logout()
        except Exception:
            pass

    logger.info(
        f"[REPLY LOGGER] Done. Found: {stats['replies_found']}, Matched: {stats['matched']}, "
        f"Reconciled: {stats['reconciled']}, Dupes skipped: {stats['duplicates_skipped']}, "
        f"Status-update failures: {stats['status_update_failures']}, Errors: {stats['errors']}"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_reply_logger()
    print(result)
