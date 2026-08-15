"""
Email sending for submission + rejection notices.

Two delivery paths:
  1. EMAIL_WEBHOOK_URL set  -> POST JSON to n8n, which does the actual sending.
     The payload carries both a plain-text and an HTML body, plus an optional
     base64 attachment. (The n8n workflow must be configured to use them —
     see REPLIT_SETUP.md.)
  2. Otherwise               -> send directly over SMTP as a multipart message
     (plain + HTML alternative, with the CSV attached).

When neither is configured (EMAIL_MODE=capture) nothing is sent; the message is
recorded in the email_log table so content can still be verified.
"""
import os
import csv
import io
import base64
import smtplib
from email.message import EmailMessage
from html import escape

from . import db

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")      # e.g. you@srvmedia.com
SMTP_PASS = os.environ.get("SMTP_PASS", "")      # Gmail app password
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER or "no-reply@srvmedia.com")
EMAIL_WEBHOOK_URL = os.environ.get("EMAIL_WEBHOOK_URL", "").strip()
EMAIL_MODE = os.environ.get("EMAIL_MODE", "send" if SMTP_USER and SMTP_PASS else "capture")

# Columns match the upload template, so a rejected-entries CSV can be corrected
# and re-uploaded as-is. The trailing rejection_* columns are ignored by the
# validator (it only requires the template columns to be present).
CSV_COLUMNS = ["project_name", "timesheet_title", "date", "logged_hours",
               "logged_mins", "status", "description",
               "rejected_by", "rejection_reason"]


def build_entries_csv(entries, reason="", rejected_by=""):
    """Render rejected entries as CSV text the employee can fix and re-upload."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for e in entries:
        w.writerow({
            "project_name": e.get("project_name") or "",
            "timesheet_title": e.get("timesheet_title") or "",
            "date": e.get("date") or "",
            "logged_hours": e.get("logged_hours") or "",
            "logged_mins": e.get("logged_mins") or "",
            "status": e.get("status") or "",
            "description": e.get("description") or "",
            "rejected_by": rejected_by or e.get("reviewed_by") or "",
            "rejection_reason": reason or e.get("reject_reason") or "",
        })
    # utf-8-sig so Excel opens it cleanly on Windows.
    return buf.getvalue()


def _entries_table_html(entries):
    head = ("<tr>" + "".join(
        f'<th style="padding:6px 10px;border:1px solid #E5E7EB;background:#F3F4F6;'
        f'text-align:left;font-size:13px;">{h}</th>'
        for h in ["Date", "Project", "Timesheet", "Hours", "Description"]) + "</tr>")
    body = ""
    for e in entries:
        cells = [
            e.get("date") or "",
            e.get("project_name") or "",
            e.get("timesheet_title") or "",
            f"{e.get('logged_hours') or 0}h {e.get('logged_mins') or 0}m",
            e.get("description") or "—",
        ]
        body += "<tr>" + "".join(
            f'<td style="padding:6px 10px;border:1px solid #E5E7EB;font-size:13px;">'
            f'{escape(str(v))}</td>' for v in cells) + "</tr>"
    return ('<table style="border-collapse:collapse;margin:12px 0;">'
            f'{head}{body}</table>')


def _entries_text(entries):
    return "\n".join(
        f"- {e.get('date')}  {e.get('logged_hours')}h {e.get('logged_mins')}m  "
        f"{e.get('project_name')} / {e.get('timesheet_title')}  "
        f"({e.get('description') or 'no description'})"
        for e in entries
    )


def _wrap_html(inner):
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
        'sans-serif;font-size:14px;line-height:1.6;color:#111827;">'
        f'{inner}'
        '<p style="color:#6B7280;font-size:12px;margin-top:24px;">'
        '— SRV Timesheet Compliance</p></div>'
    )


def send_rejection_email(to_email, submitter_name, reason, entries_summary,
                         manager_name="", manager_email="", entries=None):
    """Notify an employee their entries were rejected.

    The manager's name is bolded in the HTML body, and the rejected rows are
    attached as a CSV they can correct and re-upload.
    """
    subject = "Your timesheet entries were not approved"
    who = manager_name or manager_email or ""
    by_text = f" by {who}" if who else ""
    reply_line = (f"Questions? Just reply to this email — it goes to {who}.\n"
                  if manager_email else "")

    text = (
        f"Hi {submitter_name or to_email},\n\n"
        f"The following time entries were reviewed and NOT approved{by_text}:\n\n"
        f"{entries_summary}\n\n"
        f"Reason:\n{reason or '(no reason provided)'}\n\n"
        f"The rejected entries are attached as a CSV. Correct them in that file "
        f"and upload it again in the Timesheet app.\n"
        f"{reply_line}\n"
        f"— SRV Timesheet Compliance"
    )

    by_html = (f' by <strong>{escape(who)}</strong>') if who else ""
    table = _entries_table_html(entries) if entries else \
        f"<pre style='font-size:13px;'>{escape(entries_summary)}</pre>"
    inner = (
        f"<p>Hi {escape(submitter_name or to_email)},</p>"
        f"<p>The following time entries were reviewed and "
        f"<strong>not approved</strong>{by_html}:</p>"
        f"{table}"
        f"<p><strong>Reason:</strong><br>{escape(reason or '(no reason provided)')}</p>"
        f"<p>The rejected entries are attached as a CSV "
        f"(<code>rejected_entries.csv</code>). Correct them in that file and "
        f"upload it again in the Timesheet app.</p>"
        + (f"<p style='color:#6B7280;'>Questions? Just reply to this email — "
           f"it goes to <strong>{escape(who)}</strong>.</p>" if manager_email else "")
    )

    attachment = None
    if entries:
        attachment = {
            "filename": "rejected_entries.csv",
            "content": build_entries_csv(entries, reason=reason, rejected_by=who),
            "mime": "text/csv",
        }

    return _send(to_email, subject, text, html=_wrap_html(inner),
                 reply_to=manager_email or None, attachment=attachment)


def send_submission_email(to_email, manager_name, submitter_name, submitter_email,
                          count, entries_summary, review_url, entries=None):
    """Tell a manager that entries are waiting for review."""
    plural = "entries" if count != 1 else "entry"
    who = submitter_name or submitter_email
    subject = f"{who} submitted {count} timesheet {plural} for approval"

    text = (
        f"Hi {manager_name or to_email},\n\n"
        f"{who} has submitted {count} time {plural} that need your approval:\n\n"
        f"{entries_summary}\n\n"
        f"Please log in and review (approve or reject):\n{review_url}\n\n"
        f"— SRV Timesheet Compliance"
    )

    table = _entries_table_html(entries) if entries else \
        f"<pre style='font-size:13px;'>{escape(entries_summary)}</pre>"
    inner = (
        f"<p>Hi <strong>{escape(manager_name or to_email)}</strong>,</p>"
        f"<p><strong>{escape(who)}</strong> has submitted <strong>{count}</strong> "
        f"time {plural} that need your approval:</p>"
        f"{table}"
        f'<p><a href="{escape(review_url)}" style="display:inline-block;'
        f'background:#1E3A5F;color:#fff;padding:10px 20px;border-radius:6px;'
        f'text-decoration:none;font-weight:600;">Review entries</a></p>'
        f'<p style="color:#6B7280;font-size:12px;">Or paste this link: '
        f'{escape(review_url)}</p>'
    )
    return _send(to_email, subject, text, html=_wrap_html(inner),
                 reply_to=submitter_email or None)


def _send(to_email, subject, body, html=None, reply_to=None, attachment=None):
    """attachment: {"filename": str, "content": str, "mime": "text/csv"} or None."""
    # Preferred path: hand off to an n8n webhook that does the sending.
    if EMAIL_WEBHOOK_URL:
        import httpx
        payload = {
            "to": to_email,
            "subject": subject,
            "message": body,                 # plain text (unchanged key)
            "html": html or "",              # NEW: rich body with bold names
            "from": EMAIL_FROM,
            "reply_to": reply_to or "",
        }
        if attachment:
            raw = attachment["content"].encode("utf-8-sig")
            payload["attachment_filename"] = attachment["filename"]
            payload["attachment_mime"] = attachment.get("mime", "text/csv")
            payload["attachment_base64"] = base64.b64encode(raw).decode("ascii")
        try:
            r = httpx.post(EMAIL_WEBHOOK_URL, json=payload, timeout=30)
            ok = 200 <= r.status_code < 300
            db.log_email(to_email, subject, body, sent_ok=ok,
                         error="" if ok else f"webhook HTTP {r.status_code}: {r.text[:200]}")
            return ok, ("webhook" if ok else f"HTTP {r.status_code}")
        except Exception as e:
            db.log_email(to_email, subject, body, sent_ok=False, error=f"webhook error: {e}")
            return False, str(e)

    if EMAIL_MODE == "capture":
        note = "(capture mode - not sent)" + (f" reply-to={reply_to}" if reply_to else "")
        if attachment:
            note += f" attachment={attachment['filename']}"
        db.log_email(to_email, subject, body, sent_ok=True, error=note)
        return True, "captured"

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        if attachment:
            maintype, _, subtype = attachment.get("mime", "text/csv").partition("/")
            msg.add_attachment(
                attachment["content"].encode("utf-8-sig"),
                maintype=maintype or "text",
                subtype=subtype or "csv",
                filename=attachment["filename"],
            )
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        db.log_email(to_email, subject, body, sent_ok=True)
        return True, "sent"
    except Exception as e:
        db.log_email(to_email, subject, body, sent_ok=False, error=str(e))
        return False, str(e)
