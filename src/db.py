"""
SQLite persistence layer for the ProofHub Time Compliance app (Tier B).

Holds the employee roster (for manager routing), the pending time-entry queue
(entries wait here until a manager approves/rejects), stored ProofHub API keys,
sent-email log, and an audit trail.

Pure stdlib sqlite3 — no external DB service — so it runs anywhere the Docker
container runs and migrates cleanly to Postgres later.
"""
import os
import json
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


LEGACY_EMP_COLS = ["dept_head_email", "employee_id", "team",
                   "sub_department", "cluster", "location"]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                email           TEXT PRIMARY KEY,
                name            TEXT,           -- shown to manager + used in emails
                manager_name    TEXT,           -- approver's name (for display)
                manager_email   TEXT,           -- approval routing
                role            TEXT DEFAULT 'user',   -- access role (defaults to user)
                active          INTEGER DEFAULT 1      -- derived from leaving info
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                email       TEXT PRIMARY KEY,
                api_key     TEXT NOT NULL,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                submitter_email TEXT NOT NULL,
                manager_email   TEXT,
                project_id      TEXT,
                project_name    TEXT,
                timesheet_id    TEXT,
                timesheet_title TEXT,
                date            TEXT,
                logged_hours    TEXT,
                logged_mins     TEXT,
                status          TEXT,          -- billable / non-billable
                description     TEXT,
                source          TEXT,          -- manual / csv
                approval_status TEXT DEFAULT 'pending',  -- pending / approved / rejected
                reject_reason   TEXT,
                reviewed_by     TEXT,
                reviewed_at     TEXT,
                proofhub_entry_id TEXT,
                sync_status     TEXT DEFAULT 'not_synced', -- not_synced / synced / failed
                sync_message    TEXT,
                submitted_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS email_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                to_email    TEXT,
                subject     TEXT,
                body        TEXT,
                sent_ok     INTEGER,
                error       TEXT,
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                actor       TEXT,
                action      TEXT,
                detail      TEXT,
                created_at  TEXT
            );
            """
        )
        # Migration: drop legacy roster columns we no longer collect, so old
        # databases stop holding fields (employee id, department, etc.) the app
        # doesn't use. Best-effort; harmless once already dropped.
        try:
            existing = [r["name"] for r in c.execute("PRAGMA table_info(employees)").fetchall()]
            for col in LEGACY_EMP_COLS:
                if col in existing:
                    try:
                        c.execute(f"ALTER TABLE employees DROP COLUMN {col}")
                    except Exception:
                        pass
            # Add newer columns to older databases.
            if "manager_name" not in existing:
                try:
                    c.execute("ALTER TABLE employees ADD COLUMN manager_name TEXT")
                except Exception:
                    pass
        except Exception:
            pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- roster
def upsert_employees(rows):
    """rows: list of dicts (already mapped) with keys email, name, manager_name,
    manager_email, role, active. Only these operational fields are stored —
    no employee id, department, cluster, location, etc."""
    n = 0
    with _lock, _conn() as c:
        for r in rows:
            email = (r.get("email") or "").strip().lower()
            if not email:
                continue
            c.execute(
                """INSERT INTO employees
                   (email, name, manager_name, manager_email, role, active)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(email) DO UPDATE SET
                     name=excluded.name, manager_name=excluded.manager_name,
                     manager_email=excluded.manager_email, role=excluded.role,
                     active=excluded.active""",
                (email, r.get("name", ""), r.get("manager_name", ""),
                 (r.get("manager_email") or "").strip().lower(),
                 r.get("role", "user"), int(r.get("active", 1))),
            )
            n += 1
    return n


def deactivate_missing(active_emails):
    """Set active=0 for anyone in the DB not present in the latest sheet."""
    active_set = {e.strip().lower() for e in active_emails if e}
    changed = 0
    with _lock, _conn() as c:
        rows = c.execute("SELECT email FROM employees WHERE active=1").fetchall()
        for row in rows:
            if row["email"] not in active_set:
                c.execute("UPDATE employees SET active=0 WHERE email=?", (row["email"],))
                changed += 1
    return changed


# Back-compat alias used by the CSV seed path.
def seed_roster(rows):
    return upsert_employees(rows)


def count_employees(active_only=True):
    with _conn() as c:
        q = "SELECT COUNT(*) n FROM employees" + (" WHERE active=1" if active_only else "")
        return c.execute(q).fetchone()["n"]


def get_employee(email):
    with _conn() as c:
        row = c.execute("SELECT * FROM employees WHERE email=?",
                        (email.strip().lower(),)).fetchone()
        return dict(row) if row else None


def is_manager(email):
    """A person is a manager if anyone reports to them (or role says so)."""
    email = email.strip().lower()
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM employees WHERE manager_email=?",
                        (email,)).fetchone()
        if row and row["n"] > 0:
            return True
        emp = c.execute("SELECT role FROM employees WHERE email=?", (email,)).fetchone()
        return bool(emp and emp["role"] in ("manager", "hr", "admin"))


def reports_of(manager_email):
    with _conn() as c:
        rows = c.execute("SELECT email FROM employees WHERE manager_email=?",
                         (manager_email.strip().lower(),)).fetchall()
        return [r["email"] for r in rows]


# ---------------------------------------------------------------- api keys
def save_api_key(email, api_key):
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO api_keys (email, api_key, updated_at) VALUES (?,?,?)
               ON CONFLICT(email) DO UPDATE SET api_key=excluded.api_key,
               updated_at=excluded.updated_at""",
            (email.strip().lower(), api_key, _now()),
        )


def get_api_key(email):
    with _conn() as c:
        row = c.execute("SELECT api_key FROM api_keys WHERE email=?",
                        (email.strip().lower(),)).fetchone()
        return row["api_key"] if row else None


def has_api_key(email):
    return get_api_key(email) is not None


# ---------------------------------------------------------------- entries
def insert_pending_entries(submitter_email, manager_email, source, entries):
    """entries: list of dicts (project_id, project_name, timesheet_id,
    timesheet_title, date, logged_hours, logged_mins, status, description)."""
    ids = []
    with _lock, _conn() as c:
        for e in entries:
            cur = c.execute(
                """INSERT INTO entries
                   (submitter_email, manager_email, project_id, project_name,
                    timesheet_id, timesheet_title, date, logged_hours, logged_mins,
                    status, description, source, approval_status, submitted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                (submitter_email.strip().lower(), (manager_email or "").strip().lower(),
                 e.get("project_id"), e.get("project_name"), e.get("timesheet_id"),
                 e.get("timesheet_title"), e.get("date"), e.get("logged_hours"),
                 e.get("logged_mins"), e.get("status"), e.get("description"),
                 source, _now()),
            )
            ids.append(cur.lastrowid)
    return ids


def list_pending_for_manager(manager_email):
    with _conn() as c:
        rows = c.execute(
            """SELECT e.*, emp.name AS submitter_name
               FROM entries e LEFT JOIN employees emp ON emp.email = e.submitter_email
               WHERE e.manager_email=? AND e.approval_status='pending'
               ORDER BY e.submitter_email, e.date""",
            (manager_email.strip().lower(),),
        ).fetchall()
        return [dict(r) for r in rows]


def list_pending_all():
    """Every pending entry, regardless of manager — used by admins so entries
    with a blank/unresolvable manager can never get stuck with no approver."""
    with _conn() as c:
        rows = c.execute(
            """SELECT e.*, emp.name AS submitter_name
               FROM entries e LEFT JOIN employees emp ON emp.email = e.submitter_email
               WHERE e.approval_status='pending'
               ORDER BY e.submitter_email, e.date"""
        ).fetchall()
        return [dict(r) for r in rows]


def list_entries_for_user(email):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM entries WHERE submitter_email=? ORDER BY submitted_at DESC, id DESC",
            (email.strip().lower(),),
        ).fetchall()
        return [dict(r) for r in rows]


def entries_summary():
    """Aggregate counts across all entries, for the admin dashboard/cleanup."""
    with _conn() as c:
        def n(q):
            return c.execute(q).fetchone()["n"]
        return {
            "total": n("SELECT COUNT(*) n FROM entries"),
            "pending": n("SELECT COUNT(*) n FROM entries WHERE approval_status='pending'"),
            "approved": n("SELECT COUNT(*) n FROM entries WHERE approval_status='approved'"),
            "rejected": n("SELECT COUNT(*) n FROM entries WHERE approval_status='rejected'"),
            "synced": n("SELECT COUNT(*) n FROM entries WHERE approval_status='approved' AND sync_status='synced'"),
            "failed": n("SELECT COUNT(*) n FROM entries WHERE approval_status='approved' AND sync_status='failed'"),
        }


def delete_entries(scope):
    """Remove processed entries once they are done. scope:
      'synced'   -> approved AND pushed to ProofHub (safe default)
      'reviewed' -> everything approved or rejected (keeps only pending)
      'all'      -> wipe the entire entry queue
    Returns the number of rows deleted."""
    where = {
        "synced": "approval_status='approved' AND sync_status='synced'",
        "reviewed": "approval_status IN ('approved','rejected')",
        "all": "1=1",
    }.get(scope)
    if not where:
        return 0
    with _lock, _conn() as c:
        cur = c.execute(f"DELETE FROM entries WHERE {where}")
        return cur.rowcount


def get_entries_by_ids(ids):
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM entries WHERE id IN ({q})", ids).fetchall()
        return [dict(r) for r in rows]


def mark_approved(entry_id, reviewer, proofhub_entry_id, sync_status, sync_message):
    with _lock, _conn() as c:
        c.execute(
            """UPDATE entries SET approval_status='approved', reviewed_by=?,
               reviewed_at=?, proofhub_entry_id=?, sync_status=?, sync_message=?
               WHERE id=?""",
            (reviewer, _now(), proofhub_entry_id, sync_status, sync_message, entry_id),
        )


def mark_rejected(entry_id, reviewer, reason):
    with _lock, _conn() as c:
        c.execute(
            """UPDATE entries SET approval_status='rejected', reviewed_by=?,
               reviewed_at=?, reject_reason=? WHERE id=?""",
            (reviewer, _now(), reason, entry_id),
        )


# ---------------------------------------------------------------- logs
def log_email(to_email, subject, body, sent_ok, error=""):
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO email_log (to_email, subject, body, sent_ok, error, created_at)
               VALUES (?,?,?,?,?,?)""",
            (to_email, subject, body, 1 if sent_ok else 0, error, _now()),
        )


def audit(actor, action, detail=""):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?,?,?,?)",
            (actor, action, json.dumps(detail) if not isinstance(detail, str) else detail, _now()),
        )
