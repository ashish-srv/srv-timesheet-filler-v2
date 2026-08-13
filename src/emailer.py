"""
Email sending for rejection notices.

In production it uses SMTP (e.g. Google Workspace) via env vars. In test/dev
(no SMTP configured, or EMAIL_MODE=capture) it does NOT send — it records the
message in the email_log table so the content can be verified.
"""
import os
import smtplib
from email.mime.text import MIMEText

from . import db

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")      # e.g. you@srvmedia.com
SMTP_PASS = os.environ.get("SMTP_PASS", "")      # Gmail app password
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER or "no-reply@srvmedia.com")
# If set, emails are handed to an n8n (or any) webhook that does the actual
# sending — the app just POSTs JSON, so no SMTP creds live in the app and the
# sender address is whatever the webhook is allowed to send as.
EMAIL_WEBHOOK_URL = os.environ.get("EMAIL_WEBHOOK_URL", "").strip()
# 'capture' = don't send, just log (default when nothing configured); 'send' = SMTP
EMAIL_MODE = os.environ.get("EMAIL_MODE", "send" if SMTP_USER and SMTP_PASS else "capture")


def send_rejection_email(to_email, submitter_name, reason, entries_summary,
                         manager_name="", manager_email=""):
    subject = "Your timesheet entries were not approved"
    by = f" by {manager_name}" if manager_name else ""
    reply_line = (f"Questions? Just reply to this email — it goes to "
                  f"{manager_name or manager_email}.\n") if manager_email else ""
    body = (
        f"Hi {submitter_name or to_email},\n\n"
        f"The following time entries were reviewed and NOT approved{by}:\n\n"
        f"{entries_summary}\n\n"
        f"Reason:\n{reason or '(no reason provided)'}\n\n"
        f"Please correct and resubmit in the Timesheet app.\n"
        f"{reply_line}\n"
        f"— SRV Timesheet Compliance"
    )
    return _send(to_email, subject, body, reply_to=manager_email or None)


def send_submission_email(to_email, manager_name, submitter_name, submitter_email,
                          count, entries_summary, review_url):
    plural = "entries" if count != 1 else "entry"
    subject = f"{submitter_name or submitter_email} submitted {count} timesheet {plural} for approval"
    body = (
        f"Hi {manager_name or to_email},\n\n"
        f"{submitter_name or submitter_email} has submitted {count} time {plural} "
        f"that need your approval:\n\n"
        f"{entries_summary}\n\n"
        f"Please log in and review (approve or reject):\n{review_url}\n\n"
        f"— SRV Timesheet Compliance"
    )
    # Reply goes to the employee who submitted.
    return _send(to_email, subject, body, reply_to=submitter_email or None)


def _send(to_email, subject, body, reply_to=None):
    # Preferred path: hand off to an n8n webhook that does the sending.
    if EMAIL_WEBHOOK_URL:
        import httpx
        payload = {"to": to_email, "subject": subject, "message": body,
                   "from": EMAIL_FROM, "reply_to": reply_to or ""}
        try:
            r = httpx.post(EMAIL_WEBHOOK_URL, json=payload, timeout=20)
            ok = 200 <= r.status_code < 300
            db.log_email(to_email, subject, body, sent_ok=ok,
                         error="" if ok else f"webhook HTTP {r.status_code}: {r.text[:200]}")
            return ok, ("webhook" if ok else f"HTTP {r.status_code}")
        except Exception as e:
            db.log_email(to_email, subject, body, sent_ok=False, error=f"webhook error: {e}")
            return False, str(e)
    if EMAIL_MODE == "capture":
        note = "(capture mode - not sent)" + (f" reply-to={reply_to}" if reply_to else "")
        db.log_email(to_email, subject, body, sent_ok=True, error=note)
        return True, "captured"
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(EMAIL_FROM, [to_email], msg.as_string())
        db.log_email(to_email, subject, body, sent_ok=True)
        return True, "sent"
    except Exception as e:
        db.log_email(to_email, subject, body, sent_ok=False, error=str(e))
        return False, str(e)
