"""
PostgreSQL persistence layer for the ProofHub Time Compliance app.

Holds the employee roster (for manager routing), the pending time-entry queue
(entries wait here until a manager approves/rejects), stored ProofHub API keys,
sent-email log, and an audit trail.

Connects via DATABASE_URL (Replit provisions this automatically for its built-in
Postgres). Uses a small connection pool so concurrent submissions don't block
each other the way the old single-file SQLite database did.

Timestamps are stored as TEXT in 'YYYY-MM-DD HH:MM:SS' form, rendered in the
company timezone (APP_TIMEZONE, default Asia/Kolkata) rather than the server's
UTC clock. That format sorts lexicographically, so ordering and age-based
retention both work on plain string comparison.
"""
import os
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")

# How many recent submission batches a user sees in "My Entries".
MY_ENTRIES_SUBMISSIONS = int(os.environ.get("MY_ENTRIES_SUBMISSIONS", "3"))
# Auto-delete approved+synced entries older than this many days (0 = never).
RETENTION_DAYS = int(os.environ.get("ENTRY_RETENTION_DAYS", "365"))

_pool_obj = None


def _pool():
    global _pool_obj
    if _pool_obj is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. On Replit, add the PostgreSQL "
                "integration; it provides this automatically."
            )
        _pool_obj = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "10")),
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool_obj


@contextmanager
def _cur():
    """Yield a cursor inside a transaction that commits on clean exit."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur


def _now():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(APP_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _days_ago(n):
    if ZoneInfo:
        try:
            base = datetime.now(ZoneInfo(APP_TIMEZONE))
        except Exception:
            base = datetime.now()
    else:
        base = datetime.now()
    return (base - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with _cur() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS employees (
                email           TEXT PRIMARY KEY,
                name            TEXT,
                manager_name    TEXT,
                manager_email   TEXT,
                role            TEXT DEFAULT 'user',
                active          SMALLINT DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                email       TEXT PRIMARY KEY,
                api_key     TEXT NOT NULL,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS entries (
                id              BIGSERIAL PRIMARY KEY,
                batch_id        TEXT,
                submitter_email TEXT NOT NULL,
                manager_email   TEXT,
                project_id      TEXT,
                project_name    TEXT,
                timesheet_id    TEXT,
                timesheet_title TEXT,
                date            TEXT,
                logged_hours    TEXT,
                logged_mins     TEXT,
                status          TEXT,
                description     TEXT,
                source          TEXT,
                approval_status TEXT DEFAULT 'pending',
                reject_reason   TEXT,
                reviewed_by     TEXT,
                reviewed_at     TEXT,
                proofhub_entry_id TEXT,
                sync_status     TEXT DEFAULT 'not_synced',
                sync_message    TEXT,
                submitted_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS email_log (
                id          BIGSERIAL PRIMARY KEY,
                to_email    TEXT,
                subject     TEXT,
                body        TEXT,
                sent_ok     SMALLINT,
                error       TEXT,
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id          BIGSERIAL PRIMARY KEY,
                actor       TEXT,
                action      TEXT,
                detail      TEXT,
                created_at  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_entries_submitter
                ON entries (submitter_email, submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_entries_manager_pending
                ON entries (manager_email, approval_status);
            CREATE INDEX IF NOT EXISTS idx_entries_batch
                ON entries (batch_id);
            CREATE INDEX IF NOT EXISTS idx_employees_manager
                ON employees (manager_email);
            """
        )
        # Older databases created before batch_id existed.
        c.execute("ALTER TABLE entries ADD COLUMN IF NOT EXISTS batch_id TEXT")


# ---------------------------------------------------------------- roster
def upsert_employees(rows):
    """rows: list of dicts with keys email, name, manager_name, manager_email,
    role, active. Only these operational fields are stored."""
    payload = []
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if not email:
            continue
        payload.append((
            email, r.get("name", ""), r.get("manager_name", ""),
            (r.get("manager_email") or "").strip().lower(),
            r.get("role", "user"), int(r.get("active", 1)),
        ))
    if not payload:
        return 0
    with _cur() as c:
        c.executemany(
            """INSERT INTO employees
               (email, name, manager_name, manager_email, role, active)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (email) DO UPDATE SET
                 name=EXCLUDED.name, manager_name=EXCLUDED.manager_name,
                 manager_email=EXCLUDED.manager_email, role=EXCLUDED.role,
                 active=EXCLUDED.active""",
            payload,
        )
    return len(payload)


def deactivate_missing(active_emails):
    """Set active=0 for anyone in the DB not present in the latest sheet."""
    active_set = sorted({e.strip().lower() for e in active_emails if e})
    with _cur() as c:
        if active_set:
            c.execute(
                "UPDATE employees SET active=0 "
                "WHERE active=1 AND NOT (email = ANY(%s))",
                (active_set,),
            )
        else:
            c.execute("UPDATE employees SET active=0 WHERE active=1")
        return c.rowcount


# Back-compat alias used by the CSV seed path.
def seed_roster(rows):
    return upsert_employees(rows)


def count_employees(active_only=True):
    with _cur() as c:
        q = "SELECT COUNT(*) n FROM employees" + (" WHERE active=1" if active_only else "")
        c.execute(q)
        return c.fetchone()["n"]


def get_employee(email):
    with _cur() as c:
        c.execute("SELECT * FROM employees WHERE email=%s", (email.strip().lower(),))
        row = c.fetchone()
        return dict(row) if row else None


def is_manager(email):
    """A person is a manager if anyone reports to them (or role says so)."""
    email = email.strip().lower()
    with _cur() as c:
        c.execute("SELECT COUNT(*) n FROM employees WHERE manager_email=%s", (email,))
        row = c.fetchone()
        if row and row["n"] > 0:
            return True
        c.execute("SELECT role FROM employees WHERE email=%s", (email,))
        emp = c.fetchone()
        return bool(emp and emp["role"] in ("manager", "hr", "admin"))


def reports_of(manager_email):
    with _cur() as c:
        c.execute("SELECT email FROM employees WHERE manager_email=%s",
                  (manager_email.strip().lower(),))
        return [r["email"] for r in c.fetchall()]


# ---------------------------------------------------------------- api keys
def save_api_key(email, api_key):
    with _cur() as c:
        c.execute(
            """INSERT INTO api_keys (email, api_key, updated_at) VALUES (%s,%s,%s)
               ON CONFLICT (email) DO UPDATE SET api_key=EXCLUDED.api_key,
               updated_at=EXCLUDED.updated_at""",
            (email.strip().lower(), api_key, _now()),
        )


def get_api_key(email):
    with _cur() as c:
        c.execute("SELECT api_key FROM api_keys WHERE email=%s", (email.strip().lower(),))
        row = c.fetchone()
        return row["api_key"] if row else None


def has_api_key(email):
    return get_api_key(email) is not None


def get_api_key_updated(email):
    """When this user last saved their ProofHub key — shown in Settings so they
    can tell whether the stored key is the one they think it is."""
    with _cur() as c:
        c.execute("SELECT updated_at FROM api_keys WHERE email=%s",
                  (email.strip().lower(),))
        row = c.fetchone()
        return row["updated_at"] if row else None


def delete_api_key(email):
    """Remove a stored key (used by the 'Disconnect' action in Settings)."""
    with _cur() as c:
        c.execute("DELETE FROM api_keys WHERE email=%s", (email.strip().lower(),))
        return c.rowcount


# ---------------------------------------------------------------- entries
def insert_pending_entries(submitter_email, manager_email, source, entries):
    """One call = one 'submission'. All rows share a batch_id so 'My Entries'
    can show the last N submissions rather than every row ever created."""
    if not entries:
        return []
    batch_id = str(uuid.uuid4())
    submitted_at = _now()
    submitter = submitter_email.strip().lower()
    manager = (manager_email or "").strip().lower()
    ids = []
    with _cur() as c:
        for e in entries:
            c.execute(
                """INSERT INTO entries
                   (batch_id, submitter_email, manager_email, project_id, project_name,
                    timesheet_id, timesheet_title, date, logged_hours, logged_mins,
                    status, description, source, approval_status, submitted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                   RETURNING id""",
                (batch_id, submitter, manager,
                 e.get("project_id"), e.get("project_name"), e.get("timesheet_id"),
                 e.get("timesheet_title"), e.get("date"), e.get("logged_hours"),
                 e.get("logged_mins"), e.get("status"), e.get("description"),
                 source, submitted_at),
            )
            ids.append(c.fetchone()["id"])
    return ids


def list_pending_for_manager(manager_email):
    with _cur() as c:
        c.execute(
            """SELECT e.*, emp.name AS submitter_name
               FROM entries e LEFT JOIN employees emp ON emp.email = e.submitter_email
               WHERE e.manager_email=%s AND e.approval_status='pending'
               ORDER BY e.submitter_email, e.date""",
            (manager_email.strip().lower(),),
        )
        return [dict(r) for r in c.fetchall()]


def list_pending_all():
    """Every pending entry, regardless of manager — used by admins so entries
    with a blank/unresolvable manager can never get stuck with no approver."""
    with _cur() as c:
        c.execute(
            """SELECT e.*, emp.name AS submitter_name
               FROM entries e LEFT JOIN employees emp ON emp.email = e.submitter_email
               WHERE e.approval_status='pending'
               ORDER BY e.submitter_email, e.date"""
        )
        return [dict(r) for r in c.fetchall()]


def list_entries_for_user(email, submissions=None):
    """Return entries from the user's most recent N submission batches.

    A 'submission' is one click of Submit (manual batch or CSV upload), so the
    user sees whole batches rather than an arbitrary row count. Rows predating
    batch_id (if any) are grouped by their submitted_at timestamp instead.
    """
    n = MY_ENTRIES_SUBMISSIONS if submissions is None else submissions
    email = email.strip().lower()
    with _cur() as c:
        if n and n > 0:
            c.execute(
                """WITH recent AS (
                       SELECT COALESCE(batch_id, submitted_at) AS grp,
                              MAX(submitted_at) AS last_at,
                              MAX(id) AS last_id
                       FROM entries
                       WHERE submitter_email=%s
                       GROUP BY COALESCE(batch_id, submitted_at)
                       ORDER BY last_at DESC, last_id DESC
                       LIMIT %s
                   )
                   SELECT e.* FROM entries e
                   JOIN recent r ON COALESCE(e.batch_id, e.submitted_at) = r.grp
                   WHERE e.submitter_email=%s
                   ORDER BY e.submitted_at DESC, e.id DESC""",
                (email, n, email),
            )
        else:
            c.execute(
                "SELECT * FROM entries WHERE submitter_email=%s "
                "ORDER BY submitted_at DESC, id DESC",
                (email,),
            )
        return [dict(r) for r in c.fetchall()]


def count_submissions_for_user(email):
    """Total number of submission batches this user has ever made — lets the UI
    say 'showing 3 of 12 submissions' instead of silently hiding history."""
    with _cur() as c:
        c.execute(
            """SELECT COUNT(*) n FROM (
                   SELECT COALESCE(batch_id, submitted_at) AS grp
                   FROM entries WHERE submitter_email=%s
                   GROUP BY COALESCE(batch_id, submitted_at)
               ) t""",
            (email.strip().lower(),),
        )
        return c.fetchone()["n"]


def entries_summary():
    """Aggregate counts across all entries, for the admin dashboard/cleanup."""
    with _cur() as c:
        c.execute(
            """SELECT
                 COUNT(*) AS total,
                 COUNT(*) FILTER (WHERE approval_status='pending')  AS pending,
                 COUNT(*) FILTER (WHERE approval_status='approved') AS approved,
                 COUNT(*) FILTER (WHERE approval_status='rejected') AS rejected,
                 COUNT(*) FILTER (WHERE approval_status='approved'
                                  AND sync_status='synced') AS synced,
                 COUNT(*) FILTER (WHERE approval_status='approved'
                                  AND sync_status='failed') AS failed
               FROM entries"""
        )
        return {k: int(v) for k, v in c.fetchone().items()}


def delete_entries(scope):
    """Remove processed entries once they are done. scope:
      'synced'   -> approved AND pushed to ProofHub (safe default)
      'reviewed' -> everything approved or rejected (keeps only pending)
      'all'      -> wipe the entire entry queue
    Returns the number of rows deleted."""
    where = {
        "synced": "approval_status='approved' AND sync_status='synced'",
        "reviewed": "approval_status IN ('approved','rejected')",
        "all": "TRUE",
    }.get(scope)
    if not where:
        return 0
    with _cur() as c:
        c.execute(f"DELETE FROM entries WHERE {where}")
        return c.rowcount


def purge_old_entries(days=None):
    """Retention: drop entries that are finished AND older than `days`.

    Only touches approved+synced rows — work that is done and already logged in
    ProofHub. Pending and rejected entries are never removed by age, because
    they represent work the employee still has to act on.
    """
    d = RETENTION_DAYS if days is None else days
    if not d or d <= 0:
        return 0
    cutoff = _days_ago(d)
    with _cur() as c:
        c.execute(
            "DELETE FROM entries WHERE approval_status='approved' "
            "AND sync_status='synced' AND submitted_at < %s",
            (cutoff,),
        )
        return c.rowcount


def get_entries_by_ids(ids):
    if not ids:
        return []
    with _cur() as c:
        c.execute("SELECT * FROM entries WHERE id = ANY(%s)", ([int(i) for i in ids],))
        return [dict(r) for r in c.fetchall()]


def mark_approved(entry_id, reviewer, proofhub_entry_id, sync_status, sync_message):
    with _cur() as c:
        c.execute(
            """UPDATE entries SET approval_status='approved', reviewed_by=%s,
               reviewed_at=%s, proofhub_entry_id=%s, sync_status=%s, sync_message=%s
               WHERE id=%s""",
            (reviewer, _now(), proofhub_entry_id, sync_status, sync_message, entry_id),
        )


def mark_rejected(entry_id, reviewer, reason):
    with _cur() as c:
        c.execute(
            """UPDATE entries SET approval_status='rejected', reviewed_by=%s,
               reviewed_at=%s, reject_reason=%s WHERE id=%s""",
            (reviewer, _now(), reason, entry_id),
        )


# ---------------------------------------------------------------- logs
def log_email(to_email, subject, body, sent_ok, error=""):
    with _cur() as c:
        c.execute(
            """INSERT INTO email_log (to_email, subject, body, sent_ok, error, created_at)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (to_email, subject, body, 1 if sent_ok else 0, error, _now()),
        )


def audit(actor, action, detail=""):
    with _cur() as c:
        c.execute(
            "INSERT INTO audit_log (actor, action, detail, created_at) VALUES (%s,%s,%s,%s)",
            (actor, action, json.dumps(detail) if not isinstance(detail, str) else detail, _now()),
        )
