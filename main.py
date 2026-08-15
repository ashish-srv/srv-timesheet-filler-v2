import time
import uuid
import json
import os
import io
import csv as csvmod
import tempfile
import threading
from datetime import datetime

import pandas as pd
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from src.proofhub_client import ProofHubClient
from src.csv_processor import (
    validate_csv_rows, create_sample_csv, ACCEPTED, WARNING, REJECTED,
)
from src import db, auth, emailer, roster_sync

COMPANY_NAME = os.environ.get("COMPANY_NAME", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")
# Base URL used in notification emails' "log in to review" link.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8123").rstrip("/")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

if os.path.exists("assets"):
    app.mount("/assets", StaticFiles(directory="assets"), name="assets")


def _roster_autosync_loop(interval_seconds):
    """Re-pull the HR sheet on a schedule so the roster tracks daily edits
    without anyone clicking Sync. Runs in a daemon thread."""
    while True:
        time.sleep(interval_seconds)
        try:
            summary = roster_sync.sync_now(actor="auto")
            print("[roster] auto sync:", summary)
        except Exception as e:
            print("[roster] auto sync failed:", e)


@app.on_event("startup")
def _startup():
    db.init_db()
    # Preferred: live Google Sheets sync when configured.
    # Retention: drop finished (approved + synced) entries older than
    # ENTRY_RETENTION_DAYS. Pending and rejected entries are never aged out.
    try:
        purged = db.purge_old_entries()
        if purged:
            print(f"[retention] purged {purged} old synced entries")
    except Exception as e:
        print("[retention] purge failed:", e)

    if roster_sync.SA_CONFIGURED and roster_sync.ROSTER_SHEET_ID:
        try:
            summary = roster_sync.sync_now(actor="startup")
            print("[roster] startup sync:", summary)
        except Exception as e:
            print("[roster] startup sync failed:", e)
        # Background auto-refresh (ROSTER_SYNC_HOURS, default 6; 0 disables).
        try:
            hours = float(os.environ.get("ROSTER_SYNC_HOURS", "6") or 0)
        except ValueError:
            hours = 6
        if hours > 0:
            threading.Thread(target=_roster_autosync_loop,
                             args=(int(hours * 3600),), daemon=True).start()
            print(f"[roster] auto-sync enabled every {hours}h")
    elif os.environ.get("SEED_SAMPLE", "0") == "1" and db.count_employees(False) == 0:
        # Local/dev only: seed sample roster when explicitly requested and empty.
        roster_path = os.environ.get("ROSTER_CSV", os.path.join("data", "sample_roster.csv"))
        if os.path.exists(roster_path):
            with open(roster_path, newline="", encoding="utf-8-sig") as f:
                rows = list(csvmod.DictReader(f))
            if rows:
                db.upsert_employees(rows)


auth.register_auth(app)

# Ephemeral stores
previews = {}   # preview_id -> validated rows
jobs = {}       # job_id -> approval push progress


# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------
def require_user(request: Request):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def require_manager(request: Request):
    user = require_user(request)
    if not user.get("is_manager"):
        raise HTTPException(status_code=403, detail="Managers only")
    return user


def require_admin(request: Request):
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def _client_for(email):
    key = db.get_api_key(email)
    if not key:
        return None
    return ProofHubClient(COMPANY_NAME, key)


def require_reviewer(request: Request):
    """A manager (someone with reports) OR an admin may review entries."""
    user = require_user(request)
    if not (user.get("is_manager") or user.get("is_admin")):
        raise HTTPException(status_code=403, detail="Managers only")
    return user


def _approver_for(emp):
    """Resolve who should approve this employee's entries: their reporting
    manager, else unassigned (empty). Unassigned entries stay visible to
    admins in /pending, so nothing can silently get stuck with no approver."""
    m = (emp.get("manager_email") or "").strip().lower()
    if m and db.get_employee(m):
        return m
    return ""


def _stage(e):
    """Human-readable workflow stage for one entry, plus a short code for styling."""
    a = (e.get("approval_status") or "").lower()
    s = (e.get("sync_status") or "").lower()
    if a == "pending":
        return "pending", "Pending manager approval"
    if a == "rejected":
        return "rejected", "Rejected by manager"
    if a == "approved":
        if s == "synced":
            return "synced", "Sent to ProofHub"
        if s == "failed":
            return "failed", "Approved — ProofHub sync failed"
        return "approved", "Approved — sending to ProofHub"
    return "submitted", "Submitted"


def _attach_stage(rows):
    for e in rows:
        e["stage_code"], e["stage"] = _stage(e)
    return rows


def _entries_summary_text(entries):
    return "\n".join(
        f"- {e.get('date')}  {e.get('logged_hours')}h {e.get('logged_mins')}m  "
        f"{e.get('project_name')} / {e.get('timesheet_title')}  "
        f"({e.get('description') or 'no description'})"
        for e in entries
    )


def _notify_reviewer_of_submission(manager_email, submitter, entries):
    """Email the manager (or, when there's no manager, the admins) that new
    entries are waiting. Sent in a background thread so submit stays fast."""
    who = submitter.get("name") or submitter["email"]
    summary = _entries_summary_text(entries)
    review_url = APP_BASE_URL + "/manager"
    recipients = []
    if manager_email:
        emp = db.get_employee(manager_email) or {}
        recipients.append((manager_email, emp.get("name", "")))
    else:
        for a in sorted(auth.ADMIN_EMAILS):
            ae = db.get_employee(a) or {}
            recipients.append((a, ae.get("name", "")))

    def _run():
        for to, nm in recipients:
            try:
                emailer.send_submission_email(to, nm, who, submitter["email"],
                                              len(entries), summary, review_url,
                                              entries=entries)
            except Exception as ex:
                print("[notify] submission email failed:", ex)

    if recipients:
        threading.Thread(target=_run, daemon=True).start()


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if auth.current_user(request):
        return RedirectResponse(url="/app")
    with open("templates/login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    if not auth.current_user(request):
        return RedirectResponse(url="/")
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/manager", response_class=HTMLResponse)
async def manager_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/")
    if not (user.get("is_manager") or user.get("is_admin")):
        return HTMLResponse("<h3>Managers only.</h3>", status_code=403)
    with open("templates/manager.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/me")
async def me(request: Request):
    user = require_user(request)
    emp = db.get_employee(user["email"]) or {}
    approver_email = _approver_for(emp)
    approver_name = ""
    if approver_email:
        approver_name = (db.get_employee(approver_email) or {}).get("name", "") \
            or emp.get("manager_name", "")
    return JSONResponse({**user, "has_api_key": db.has_api_key(user["email"]),
                         "api_key_updated_at": db.get_api_key_updated(user["email"]),
                         "approver_email": approver_email, "approver_name": approver_name})


# ----------------------------------------------------------------------------
# API key + projects (key stored per user)
# ----------------------------------------------------------------------------
@app.post("/save-key")
async def save_key(request: Request, api_key: str = Form(...)):
    user = require_user(request)
    api_key = api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")
    client = ProofHubClient(COMPANY_NAME, api_key)
    connected, msg = client.test_connection()
    if not connected:
        return JSONResponse({"success": False, "message": msg})
    db.save_api_key(user["email"], api_key)
    db.audit(user["email"], "save_api_key")
    projects = client.get_projects_list()
    return JSONResponse({"success": True, "message": "Connected", "projects": projects})


@app.post("/disconnect-key")
async def disconnect_key(request: Request):
    """Forget the stored ProofHub key. Submitted entries are untouched — only
    the credential is removed, and the user is asked to connect again."""
    user = require_user(request)
    removed = db.delete_api_key(user["email"])
    db.audit(user["email"], "disconnect_api_key")
    return JSONResponse({"success": True, "removed": removed})


@app.get("/projects")
async def projects(request: Request):
    user = require_user(request)
    client = _client_for(user["email"])
    if not client:
        return JSONResponse({"success": False, "message": "No API key on file", "projects": []})
    connected, msg = client.test_connection()
    if not connected:
        return JSONResponse({"success": False, "message": msg, "projects": []})
    return JSONResponse({"success": True, "projects": client.get_projects_list()})


@app.post("/timesheets")
async def timesheets(request: Request, project_id: str = Form(...)):
    user = require_user(request)
    client = _client_for(user["email"])
    if not client:
        raise HTTPException(status_code=400, detail="No API key on file")
    return JSONResponse({"success": True, "timesheets": client.get_timesheets_ordered(project_id)})


# ----------------------------------------------------------------------------
# Submit manual entries -> pending queue (NOT pushed to ProofHub yet)
# ----------------------------------------------------------------------------
@app.post("/manual-batch")
async def manual_batch(
    request: Request,
    project_id: str = Form(...),
    project_name: str = Form(""),
    timesheet_id: str = Form(...),
    timesheet_title: str = Form(""),
    entries: str = Form(...),
):
    user = require_user(request)
    from src.csv_processor import validate_single_entry
    try:
        raw = json.loads(entries)
        assert isinstance(raw, list)
    except Exception:
        return JSONResponse({"success": False, "message": "Could not read the entry list."})
    if not raw:
        return JSONResponse({"success": False, "message": "No entries to submit."})

    emp = db.get_employee(user["email"]) or {}
    manager_email = _approver_for(emp)

    to_insert = []
    skipped = []
    for e in raw:
        ok, err, cleaned = validate_single_entry(
            project_name or project_id, e.get("date"), e.get("logged_hours"),
            e.get("logged_mins"), e.get("status", "billable"), e.get("description", ""),
        )
        if not ok:
            # Don't fail the whole batch on one bad row — skip it and report.
            skipped.append({"date": e.get("date", "?"), "error": err})
            continue
        to_insert.append({
            "project_id": project_id, "project_name": project_name,
            "timesheet_id": timesheet_id, "timesheet_title": timesheet_title, **cleaned,
        })

    if not to_insert:
        detail = "; ".join(f"{s['date']}: {s['error']}" for s in skipped) or "No valid entries."
        return JSONResponse({"success": False, "message": "Nothing submitted — " + detail})

    ids = db.insert_pending_entries(user["email"], manager_email, "manual", to_insert)
    _notify_reviewer_of_submission(manager_email, user, to_insert)
    db.audit(user["email"], "submit_manual",
             {"count": len(ids), "manager": manager_email, "skipped": len(skipped)})
    resp = {"success": True, "count": len(ids),
            "manager_email": manager_email, "skipped": skipped}
    if not manager_email:
        resp["warning"] = "No manager is set for your account, so an admin will review these."
    return JSONResponse(resp)


# ----------------------------------------------------------------------------
# CSV preview + submit -> pending queue
# ----------------------------------------------------------------------------
@app.post("/preview")
async def preview(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")
    client = _client_for(user["email"])
    if not client:
        return JSONResponse({"success": False, "message": "Connect your API key first."})

    contents = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp.write(contents)
        tmp_path = tmp.name
    try:
        ok, message, rows = validate_csv_rows(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    if not ok:
        return JSONResponse({"success": False, "message": message})

    connected, conn_msg = client.test_connection()
    if not connected:
        return JSONResponse({"success": False, "message": f"ProofHub connection failed: {conn_msg}"})
    project_map = client.get_projects()

    for row in rows:
        pinfo = project_map.get(row["project_name"].lower()) if row["project_name"] else None
        if row["row_status"] != REJECTED and not pinfo:
            row["row_status"] = REJECTED
            row["messages"].append(f"Project '{row['project_name']}' not found in ProofHub")
        row["project_id"] = pinfo["id"] if pinfo else None
        row["timesheet_id"] = None
        if pinfo and row["row_status"] != REJECTED:
            wanted = (row.get("timesheet_title") or "").strip()
            sheets = client.get_timesheets_ordered(pinfo["id"])
            if wanted:
                match = next((t for t in sheets if t["name"].lower() == wanted.lower()), None)
                if match:
                    row["timesheet_id"] = match["id"]; row["timesheet_title"] = match["name"]
                else:
                    row["row_status"] = REJECTED
                    row["messages"].append(f"Timesheet '{wanted}' not found in project '{row['project_name']}'")
            else:
                if sheets:
                    row["timesheet_id"] = sheets[0]["id"]; row["timesheet_title"] = sheets[0]["name"]
                    if len(sheets) > 1:
                        row["messages"].append(f"Timesheet blank — auto-picked '{sheets[0]['name']}' of {len(sheets)}")
                        if row["row_status"] == ACCEPTED:
                            row["row_status"] = WARNING
                else:
                    row["row_status"] = REJECTED
                    row["messages"].append(f"No timesheet found for project '{row['project_name']}'")

    preview_id = str(uuid.uuid4())
    previews[preview_id] = rows
    summary = {
        "total": len(rows),
        "accepted": sum(1 for r in rows if r["row_status"] == ACCEPTED),
        "warning": sum(1 for r in rows if r["row_status"] == WARNING),
        "rejected": sum(1 for r in rows if r["row_status"] == REJECTED),
    }
    return JSONResponse({"success": True, "preview_id": preview_id, "rows": rows, "summary": summary})


@app.post("/submit-csv")
async def submit_csv(request: Request, preview_id: str = Form(...),
                     include_warnings: str = Form("true")):
    user = require_user(request)
    if preview_id not in previews:
        raise HTTPException(status_code=404, detail="Preview expired. Please re-upload the file.")
    rows = previews.pop(preview_id)
    allowed = {ACCEPTED, WARNING} if include_warnings.lower() == "true" else {ACCEPTED}
    valid = [r for r in rows if r["row_status"] in allowed]
    if not valid:
        return JSONResponse({"success": False, "message": "No valid rows to submit."})

    emp = db.get_employee(user["email"]) or {}
    manager_email = _approver_for(emp)
    ids = db.insert_pending_entries(user["email"], manager_email, "csv", valid)
    _notify_reviewer_of_submission(manager_email, user, valid)
    db.audit(user["email"], "submit_csv", {"count": len(ids), "manager": manager_email})
    resp = {"success": True, "count": len(ids), "manager_email": manager_email}
    if not manager_email:
        resp["warning"] = "No manager is set for your account, so an admin will review these."
    return JSONResponse(resp)


@app.get("/my-entries")
async def my_entries(request: Request):
    """Only the user's most recent submissions are returned (default 3, set by
    MY_ENTRIES_SUBMISSIONS). Older batches stay in the database for reporting
    but are not shown here, so this view stays fast and readable."""
    user = require_user(request)
    rows = db.list_entries_for_user(user["email"])
    total_subs = db.count_submissions_for_user(user["email"])
    shown_subs = len({r.get("batch_id") or r.get("submitted_at") for r in rows})
    return JSONResponse({
        "entries": _attach_stage(rows),
        "submissions_shown": shown_subs,
        "submissions_total": total_subs,
        "limit": db.MY_ENTRIES_SUBMISSIONS,
    })


# ----------------------------------------------------------------------------
# Manager: list pending, approve (push), reject (email)
# ----------------------------------------------------------------------------
@app.get("/pending")
async def pending(request: Request):
    user = require_reviewer(request)
    # Admins see every pending entry (including any with no resolvable manager),
    # so stuck entries always have someone who can act on them.
    if user.get("is_admin"):
        rows = db.list_pending_all()
    else:
        rows = db.list_pending_for_manager(user["email"])
    return JSONResponse({"pending": _attach_stage(rows), "count": len(rows)})


def _approve_job(job_id, entry_ids, reviewer):
    job = jobs[job_id]
    job["status"] = "processing"
    entries = db.get_entries_by_ids(entry_ids)
    clients = {}
    results = []
    for idx, e in enumerate(entries):
        submitter = e["submitter_email"]
        if submitter not in clients:
            clients[submitter] = _client_for(submitter)
        client = clients[submitter]
        if not client:
            db.mark_approved(e["id"], reviewer, "", "failed", "Submitter has no API key on file")
            results.append({"id": e["id"], "result": "FAILED", "message": "No API key for submitter"})
        else:
            ok, msg, entry_id = client.create_time_entry(
                e["project_id"], e["timesheet_id"], e["date"],
                e["logged_hours"], e["logged_mins"], e["status"], e["description"],
            )
            db.mark_approved(e["id"], reviewer, entry_id, "synced" if ok else "failed", msg)
            results.append({"id": e["id"], "result": "SUCCESS" if ok else "FAILED", "message": msg})
            time.sleep(1)
        job["processed"] = idx + 1
        job["success_count"] = sum(1 for r in results if r["result"] == "SUCCESS")
        job["failed_count"] = sum(1 for r in results if r["result"] == "FAILED")
        job["results"] = results
    db.audit(reviewer, "approve", {"ids": entry_ids,
             "ok": job["success_count"], "failed": job["failed_count"]})
    job["status"] = "done"


@app.post("/approve")
async def approve(request: Request, ids: str = Form(...)):
    manager = require_reviewer(request)
    id_list = [int(x) for x in json.loads(ids)]
    # Entries still pending that this reviewer may act on: their own team's,
    # or (for admins) any entry — so unassigned ones are never stuck.
    owned = [e["id"] for e in db.get_entries_by_ids(id_list)
             if (e["manager_email"] == manager["email"] or manager.get("is_admin"))
             and e["approval_status"] == "pending"]
    if not owned:
        return JSONResponse({"success": False, "message": "No eligible pending entries selected."})
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "starting", "total": len(owned), "processed": 0,
                    "success_count": 0, "failed_count": 0, "results": [], "error": None}
    threading.Thread(target=_approve_job, args=(job_id, owned, manager["email"]), daemon=True).start()
    return JSONResponse({"success": True, "job_id": job_id, "total": len(owned)})


@app.post("/reject")
async def reject(request: Request, ids: str = Form(...), reason: str = Form("")):
    manager = require_reviewer(request)
    id_list = [int(x) for x in json.loads(ids)]
    entries = [e for e in db.get_entries_by_ids(id_list)
               if (e["manager_email"] == manager["email"] or manager.get("is_admin"))
               and e["approval_status"] == "pending"]
    if not entries:
        return JSONResponse({"success": False, "message": "No eligible pending entries selected."})

    # Group by submitter for a single email each.
    by_user = {}
    for e in entries:
        db.mark_rejected(e["id"], manager["email"], reason)
        by_user.setdefault(e["submitter_email"], []).append(e)

    emailed = []
    for submitter, items in by_user.items():
        emp = db.get_employee(submitter) or {}
        summary = "\n".join(
            f"- {i['date']}  {i['logged_hours']}h {i['logged_mins']}m  "
            f"{i['project_name']} / {i['timesheet_title']}  ({i['description'] or 'no description'})"
            for i in items
        )
        ok, info = emailer.send_rejection_email(
            submitter, emp.get("name"), reason, summary,
            manager_name=manager.get("name", ""), manager_email=manager.get("email", ""),
            entries=items)
        emailed.append({"submitter": submitter, "count": len(items), "email_ok": ok, "info": info})

    db.audit(manager["email"], "reject", {"ids": [e["id"] for e in entries], "reason": reason})
    return JSONResponse({"success": True, "rejected": len(entries), "emails": emailed})


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    j = jobs[job_id]
    return JSONResponse({"status": j["status"], "total": j["total"], "processed": j["processed"],
                         "success_count": j["success_count"], "failed_count": j["failed_count"],
                         "error": j["error"]})


# ----------------------------------------------------------------------------
# Sample template
# ----------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/")
    if not user.get("is_admin"):
        return HTMLResponse("<h3>Admins only.</h3>", status_code=403)
    with open("templates/admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/admin/stats")
async def admin_stats(request: Request):
    require_admin(request)
    return JSONResponse({"active": db.count_employees(True), "total": db.count_employees(False)})


@app.post("/admin/upload-roster")
async def admin_upload_roster(request: Request, file: UploadFile = File(...)):
    require_admin(request)
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv export of the sheet")
    contents = await file.read()
    text = contents.decode("utf-8-sig", errors="replace")
    values = [row for row in csvmod.reader(io.StringIO(text))]
    if len(values) < 2:
        return JSONResponse({"success": False, "message": "The CSV has no data rows."})
    summary = roster_sync.sync_from_values(values, actor=auth.current_user(request)["email"])
    return JSONResponse({"success": True, **summary})


@app.get("/admin/entries-summary")
async def admin_entries_summary(request: Request):
    require_admin(request)
    return JSONResponse(db.entries_summary())


@app.post("/admin/cleanup")
async def admin_cleanup(request: Request, scope: str = Form("synced")):
    require_admin(request)
    if scope not in ("synced", "reviewed", "all"):
        return JSONResponse({"success": False, "message": "Invalid scope"}, status_code=400)
    deleted = db.delete_entries(scope)
    db.audit(auth.current_user(request)["email"], "cleanup_entries",
             {"scope": scope, "deleted": deleted})
    return JSONResponse({"success": True, "deleted": deleted})


@app.post("/admin/sync-roster")
async def admin_sync_roster(request: Request):
    require_admin(request)
    try:
        summary = roster_sync.sync_now(actor=auth.current_user(request)["email"])
        return JSONResponse({"success": True, **summary})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)


@app.get("/sample")
async def sample_csv():
    df = create_sample_csv()
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    return StreamingResponse(io.BytesIO(csv_bytes), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=sample_timesheet.csv"})
