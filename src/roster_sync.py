"""
Roster sync from the HR Google Sheet.

Reads the sheet via a Google service account (read-only), maps the HR columns
to our roster fields, derives active/inactive from leaving info, upserts into
the employees table, and deactivates anyone no longer present in the sheet.

Only operational columns are imported. Sensitive columns (date of birth,
gender, etc.) are deliberately ignored.
"""
import os
import re
from datetime import datetime

from . import db

SA_JSON = os.environ.get("GOOGLE_SA_JSON", "")          # path to service-account json
# Preferred on Replit: paste the whole service-account JSON into a Secret so no
# credential file ever exists on disk or in the repo.
SA_JSON_CONTENT = os.environ.get("GOOGLE_SA_JSON_CONTENT", "").strip()
ROSTER_SHEET_ID = os.environ.get("ROSTER_SHEET_ID", "")  # spreadsheet id
ROSTER_WORKSHEET = os.environ.get("ROSTER_WORKSHEET", "")  # tab name (blank = first)

# True when either credential source is available.
SA_CONFIGURED = bool(SA_JSON_CONTENT or SA_JSON)


def normalize(h):
    """Normalize a header: lowercase, strip, collapse inner whitespace,
    drop trailing punctuation. Makes 'Reporting Manager Email ID ' match."""
    s = str(h or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" :\t")
    return s


# target field -> list of acceptable (normalized) header names, in priority order.
# Only the operational fields the app actually uses are read from the sheet.
ALIASES = {
    "email":           ["email address", "email", "email id", "official email", "work email"],
    "name":            ["employee name", "name", "full name"],
    "manager_name":    ["reporting manager", "manager", "manager name"],
    "manager_email":   ["reporting manager email id", "reporting manager email",
                        "manager email id", "manager email"],
    # used only to decide active/inactive (not stored as-is)
    "_leftorg":        ["leftorg", "left org", "left organisation", "left organization"],
    "_leaving_date":   ["leavingdate", "leaving date", "date of leaving", "last working day"],
}

_TRUE = {"yes", "y", "true", "1", "left", "resigned", "inactive"}


def _first(row_norm, keys):
    for k in keys:
        if k in row_norm and str(row_norm[k]).strip() != "":
            return str(row_norm[k]).strip()
    return ""


def _parse_date(s):
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _derive_active(row_norm):
    left = _first(row_norm, ALIASES["_leftorg"]).lower()
    if left in _TRUE:
        return 0
    ld = _parse_date(_first(row_norm, ALIASES["_leaving_date"]))
    if ld and ld <= datetime.now().date():
        return 0
    return 1


def values_to_norm_rows(values):
    """values: list of lists (row 0 = headers). Returns list of dicts keyed by
    normalized header (first occurrence of a duplicate header wins)."""
    if not values:
        return []
    headers = [normalize(h) for h in values[0]]
    rows = []
    for raw in values[1:]:
        d = {}
        for h, val in zip(headers, raw):
            if h not in d:
                d[h] = val
        if any(str(v).strip() for v in d.values()):
            rows.append(d)
    return rows


def map_norm_rows(norm_rows):
    """Map normalized-header rows to roster records. Skips rows with no email."""
    mapped = []
    for r in norm_rows:
        email = _first(r, ALIASES["email"]).lower()
        if not email:
            continue
        mapped.append({
            "email": email,
            "name": _first(r, ALIASES["name"]),
            "manager_name": _first(r, ALIASES["manager_name"]),
            "manager_email": _first(r, ALIASES["manager_email"]).lower(),
            "role": "user",  # app role is derived (manager = someone reports to them)
            "active": _derive_active(r),
        })
    return mapped


def fetch_values_from_sheet():
    """Read the sheet via gspread + service account. Returns list-of-lists.
    Raises if not configured or on API error."""
    if not (SA_CONFIGURED and ROSTER_SHEET_ID):
        raise RuntimeError(
            "Sheets sync not configured (set GOOGLE_SA_JSON_CONTENT or "
            "GOOGLE_SA_JSON, plus ROSTER_SHEET_ID)."
        )
    import json as _json
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    if SA_JSON_CONTENT:
        try:
            info = _json.loads(SA_JSON_CONTENT)
        except ValueError as e:
            raise RuntimeError(
                "GOOGLE_SA_JSON_CONTENT is not valid JSON. Paste the entire "
                f"service-account file contents into the secret. ({e})"
            )
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(SA_JSON, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(ROSTER_SHEET_ID)
    ws = sh.worksheet(ROSTER_WORKSHEET) if ROSTER_WORKSHEET else sh.sheet1
    return ws.get_all_values()


def sync_from_values(values, actor="system"):
    """Map + upsert + deactivate-missing. Returns a summary dict."""
    norm_rows = values_to_norm_rows(values)
    mapped = map_norm_rows(norm_rows)
    upserted = db.upsert_employees(mapped)
    active_emails = [m["email"] for m in mapped if m["active"] == 1]
    deactivated = db.deactivate_missing(active_emails)
    db.audit(actor, "roster_sync", {"rows": len(mapped), "deactivated": deactivated})
    # Report counts of UNIQUE people (matches the admin dashboard), not sheet
    # rows — the sheet can list the same person on more than one row.
    return {"rows_in_sheet": len(norm_rows), "upserted": upserted,
            "unique_people": db.count_employees(False),
            "active": db.count_employees(True), "deactivated_missing": deactivated}


def sync_now(actor="system"):
    """Full live sync from the configured Google Sheet."""
    values = fetch_values_from_sheet()
    return sync_from_values(values, actor=actor)
