"""
ctr_reporter.py — Weekly CTR / CTA engagement report.
Pulls aggregated stats from Brevo /smtp/statistics/reports, builds an HTML
summary email, and sends it to NOTIFY_EMAIL via Gmail SMTP.

Run manually: python active/execution/ctr_reporter.py
Scheduled:   .github/workflows/ctr-report.yml (Sundays 14:00 UTC)

Env vars:
  BREVO_API_KEY, GMAIL_SENDER, GMAIL_APP_PASSWORD, NOTIFY_EMAIL (required)
  CTR_REPORT_DAYS  — report window in days (default 7)
"""

import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

BREVO_BASE = "https://api.brevo.com/v3"
GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 587
REQUEST_TIMEOUT = 30


def _env(key: str, default: str = "") -> str:
    return os.getenv(key) or default


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key) or default)
    except (TypeError, ValueError):
        return default


def _fetch_reports(start: str, end: str) -> list[dict]:
    """Call Brevo /smtp/statistics/reports and return daily rows."""
    url = f"{BREVO_BASE}/smtp/statistics/reports"
    headers = {"api-key": _env("BREVO_API_KEY"), "Content-Type": "application/json"}
    params = {"startDate": start, "endDate": end, "sort": "asc", "limit": 500}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            logger.error(f"[CTR] Brevo API {resp.status_code}: {resp.text[:400]}")
            return []
        return resp.json().get("reports", [])
    except Exception as exc:
        logger.error(f"[CTR] Request failed: {exc}")
        return []


def _aggregate(rows: list[dict]) -> dict:
    keys = ["requests", "delivered", "hardBounces", "softBounces",
            "clicks", "uniqueClicks", "opens", "uniqueOpens", "unsubscribes", "spamReports"]
    totals = {k: 0 for k in keys}
    for row in rows:
        for k in keys:
            totals[k] += row.get(k, 0)
    return totals


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "0.0%"
    return f"{num / denom * 100:.1f}%"


def _build_html(stats: dict, start: str, end: str, days: int, run_ts: str) -> str:
    sent = stats["requests"]
    delivered = stats["delivered"]
    hard_b = stats["hardBounces"]
    soft_b = stats["softBounces"]
    bounces = hard_b + soft_b
    unique_opens = stats["uniqueOpens"]
    unique_clicks = stats["uniqueClicks"]
    unsubscribes = stats["unsubscribes"]
    spam = stats["spamReports"]

    delivery_rate = _pct(delivered, sent)
    open_rate = _pct(unique_opens, delivered)
    ctr = _pct(unique_clicks, delivered)

    def row(label: str, value, bold: bool = False) -> str:
        style = "font-weight:bold" if bold else ""
        return (
            f"<tr><td style='padding:10px 20px;color:#555;border-bottom:1px solid #eee'>{label}</td>"
            f"<td style='padding:10px 20px;{style};border-bottom:1px solid #eee'>{value}</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4">
<div style="max-width:600px;margin:30px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">

  <div style="background:#2ECC71;padding:24px 28px">
    <div style="font-size:18px;font-weight:bold;color:#fff">CTA / CTR Report ({start} to {end})</div>
    <div style="font-size:13px;color:rgba(255,255,255,.8);margin-top:4px">{run_ts} UTC &nbsp;|&nbsp; <span style="font-weight:bold">OK</span></div>
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px">
    {row("Window", f"{start} &rarr; {end} UTC")}
    {row("Sent (requests)", sent)}
    {row("Delivered", delivered)}
    {row("Delivery rate", delivery_rate)}
    {row("Unique opens", unique_opens)}
    {row("Open rate", open_rate, bold=True)}
    {row("Unique clicks", unique_clicks)}
    {row("CTR (clicks / delivered)", ctr, bold=True)}
    {row("Bounces (hard + soft)", f"{bounces} ({hard_b} hard, {soft_b} soft)")}
    {row("Unsubscribes", unsubscribes)}
    {row("Spam reports", spam)}
  </table>

  <div style="margin:20px 28px;padding:14px 18px;background:#fffde7;border-left:4px solid #f9a825;font-size:13px;color:#555;line-height:1.5">
    Open rate may be undercounted by Apple Mail Privacy Protection and image blocking.
    Clicks accrue over 24–48 h after each send.
  </div>

  <div style="padding:16px 28px;font-size:12px;color:#aaa;border-top:1px solid #eee">
    Check <code>logs/outreach.log</code> and <code>active/leads/pipeline_errors.jsonl</code> for details.
  </div>

</div>
</body>
</html>"""


def _send(subject: str, html: str) -> None:
    sender = _env("GMAIL_SENDER")
    password = _env("GMAIL_APP_PASSWORD")
    recipient = _env("NOTIFY_EMAIL")

    if not all([sender, password, recipient]):
        logger.error("[CTR] Missing GMAIL_SENDER / GMAIL_APP_PASSWORD / NOTIFY_EMAIL — cannot send.")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(GMAIL_HOST, GMAIL_PORT, timeout=30) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(sender, password)
            srv.sendmail(sender, [recipient], msg.as_string())
        logger.info(f"[CTR] Report sent to {recipient}: {subject}")
    except Exception as exc:
        logger.error(f"[CTR] Gmail send failed: {exc}")
        sys.exit(1)


def main() -> None:
    if not _env("BREVO_API_KEY"):
        logger.error("[CTR] BREVO_API_KEY not set — aborting.")
        sys.exit(1)

    days = _int_env("CTR_REPORT_DAYS", 7)
    now = datetime.now(timezone.utc)
    end_date = now.strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    run_ts = now.strftime("%Y-%m-%d %H:%M")

    logger.info(f"[CTR] Fetching Brevo stats: {start_date} → {end_date}")
    rows = _fetch_reports(start_date, end_date)
    stats = _aggregate(rows)

    open_pct = _pct(stats["uniqueOpens"], stats["delivered"])
    ctr_pct = _pct(stats["uniqueClicks"], stats["delivered"])

    subject = f"[Lead Manager] CTR Report {start_date} to {end_date} — CTR {ctr_pct}, Open {open_pct}"
    html = _build_html(stats, start_date, end_date, days, run_ts)
    _send(subject, html)


if __name__ == "__main__":
    main()
