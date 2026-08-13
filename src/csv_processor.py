import pandas as pd
from datetime import datetime
from typing import List, Dict, Tuple
from .date_utils import parse_date_flexible

# Required columns. `timesheet_title` is OPTIONAL: if a row names a timesheet,
# the entry is logged to exactly that one; if left blank, the app auto-picks
# the project's timesheet and flags a warning.
REQUIRED_COLUMNS = ['project_name', 'date', 'logged_hours',
                    'logged_mins', 'status', 'description']
OPTIONAL_COLUMNS = ['timesheet_title']

VALID_STATUSES = ['billable', 'non-billable', 'non billable']

# Row status constants
ACCEPTED = 'accepted'   # valid, will be pushed
WARNING = 'warning'     # pushable but flagged (e.g. missing description)
REJECTED = 'rejected'   # will NOT be pushed


def _read_csv_any_encoding(file_path: str):
    for encoding in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
        try:
            return pd.read_csv(file_path, encoding=encoding, dtype=str)
        except UnicodeDecodeError:
            continue
    return None


def validate_csv_rows(file_path: str) -> Tuple[bool, str, List[Dict]]:
    """
    Validate a CSV and return EVERY row with a per-row status instead of
    failing the whole file on the first error (BRD 9.6).

    Returns (ok, message, rows) where each row is a dict:
        {
          row_num, project_name, date, logged_hours, logged_mins,
          status, description,
          row_status: 'accepted' | 'warning' | 'rejected',
          messages: [str, ...]
        }
    `ok` is False only for structural problems (unreadable file / missing
    columns) where no per-row table can be produced.
    """
    df = _read_csv_any_encoding(file_path)
    if df is None:
        return False, "Unable to read the file. Please save it as UTF-8 CSV.", []

    # Normalise column names (strip / lowercase) so header typos are forgiving.
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        return False, f"Missing required column(s): {', '.join(missing_cols)}", []

    today = datetime.now().date()
    rows: List[Dict] = []

    records = df.to_dict('records')
    for idx, raw in enumerate(records, start=1):
        messages: List[str] = []
        status_level = ACCEPTED

        def _clean(key, default=''):
            val = raw.get(key, default)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return default
            s = str(val).strip()
            return default if s.lower() in ('', 'nan', 'none') else s

        project_name = _clean('project_name')
        timesheet_title = _clean('timesheet_title')
        date_raw = _clean('date')
        hours_raw = _clean('logged_hours', '0') or '0'
        mins_raw = _clean('logged_mins', '0') or '0'
        entry_status = (_clean('status', 'billable') or 'billable')
        description = _clean('description')

        # --- project name ---
        if not project_name:
            messages.append("Project name is empty")
            status_level = REJECTED

        # --- date ---
        parsed_date = parse_date_flexible(date_raw) if date_raw else None
        if not date_raw:
            messages.append("Date is empty")
            status_level = REJECTED
        elif not parsed_date:
            messages.append(f"Invalid date '{date_raw}' (use DD-MM-YYYY)")
            status_level = REJECTED
        else:
            if datetime.strptime(parsed_date, '%Y-%m-%d').date() > today:
                messages.append("Future date not allowed")
                status_level = REJECTED
            date_raw = parsed_date  # normalised YYYY-MM-DD

        # --- hours ---
        clean_hours = '0'
        try:
            hours = float(hours_raw)
            if hours < 0:
                messages.append("Hours cannot be negative"); status_level = REJECTED
            elif hours > 12:
                messages.append("Hours must be 12 or less"); status_level = REJECTED
            elif hours != int(hours):
                messages.append("Hours must be a whole number"); status_level = REJECTED
            else:
                clean_hours = str(int(hours))
        except ValueError:
            messages.append(f"Hours '{hours_raw}' is not a number"); status_level = REJECTED

        # --- minutes ---
        clean_mins = '0'
        try:
            mins = float(mins_raw)
            if mins < 0:
                messages.append("Minutes cannot be negative"); status_level = REJECTED
            elif mins >= 60:
                messages.append("Minutes must be less than 60"); status_level = REJECTED
            elif mins != int(mins):
                messages.append("Minutes must be a whole number"); status_level = REJECTED
            else:
                clean_mins = str(int(mins))
        except ValueError:
            messages.append(f"Minutes '{mins_raw}' is not a number"); status_level = REJECTED

        # --- zero duration (warn, don't reject) ---
        if status_level != REJECTED and clean_hours == '0' and clean_mins == '0':
            messages.append("Duration is 0h 0m")
            if status_level == ACCEPTED:
                status_level = WARNING

        # --- status ---
        if entry_status.lower() not in VALID_STATUSES:
            messages.append(f"Invalid status '{entry_status}' (use billable / non-billable)")
            status_level = REJECTED

        # --- description (warn only) ---
        if not description and status_level != REJECTED:
            messages.append("Description is empty")
            if status_level == ACCEPTED:
                status_level = WARNING

        rows.append({
            'row_num': idx,
            'project_name': project_name,
            'timesheet_title': timesheet_title,
            'date': date_raw,
            'logged_hours': clean_hours,
            'logged_mins': clean_mins,
            'status': entry_status,
            'description': description,
            'row_status': status_level,
            'messages': messages,
        })

    if not rows:
        return False, "The file has no data rows.", []

    return True, f"Parsed {len(rows)} row(s).", rows


def validate_single_entry(project_name: str, date: str, logged_hours,
                          logged_mins, status: str, description: str) -> Tuple[bool, str, Dict]:
    """
    Validate one manually-entered time entry. Returns (ok, error, cleaned).
    Used by the manual entry form (BRD 9.5).
    """
    today = datetime.now().date()

    project_name = (project_name or '').strip()
    if not project_name:
        return False, "Project is required", {}

    parsed_date = parse_date_flexible(str(date).strip()) if date else None
    if not parsed_date:
        return False, "Invalid or missing date (use DD-MM-YYYY)", {}
    if datetime.strptime(parsed_date, '%Y-%m-%d').date() > today:
        return False, "Future date not allowed", {}

    try:
        hours = int(float(logged_hours or 0))
        if not (0 <= hours <= 12):
            return False, "Hours must be between 0 and 12", {}
    except (ValueError, TypeError):
        return False, "Hours is not a valid number", {}

    try:
        mins = int(float(logged_mins or 0))
        if not (0 <= mins < 60):
            return False, "Minutes must be between 0 and 59", {}
    except (ValueError, TypeError):
        return False, "Minutes is not a valid number", {}

    if hours == 0 and mins == 0:
        return False, "Duration cannot be 0h 0m", {}

    status = (status or 'billable').strip()
    if status.lower() not in VALID_STATUSES:
        return False, "Status must be billable or non-billable", {}

    return True, "", {
        'project_name': project_name,
        'date': parsed_date,
        'logged_hours': str(hours),
        'logged_mins': str(mins),
        'status': status,
        'description': (description or '').strip(),
    }


def create_sample_csv():
    """Sample CSV template — note there is NO timesheet_title column anymore."""
    sample_data = [
        {
            'project_name': 'Sample Project',
            'timesheet_title': 'Development Log',
            'date': '20-05-2026',
            'logged_hours': '2',
            'logged_mins': '30',
            'status': 'billable',
            'description': 'Worked on API integration',
        },
        {
            'project_name': 'Sample Project',
            'timesheet_title': 'Development Log',
            'date': '20-05-2026',
            'logged_hours': '1',
            'logged_mins': '15',
            'status': 'non-billable',
            'description': 'Code review and testing',
        },
        {
            'project_name': 'Another Project',
            'timesheet_title': '',
            'date': '19-05-2026',
            'logged_hours': '0',
            'logged_mins': '45',
            'status': 'billable',
            'description': 'Sprint planning meeting (timesheet blank = auto-pick)',
        },
    ]
    cols = ['project_name', 'timesheet_title', 'date', 'logged_hours',
            'logged_mins', 'status', 'description']
    return pd.DataFrame(sample_data)[cols]
