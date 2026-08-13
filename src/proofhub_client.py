import html
import requests
from typing import Dict, Tuple, List, Optional


class ProofHubClient:
    def __init__(self, company_name: str, api_key: str):
        self.company_name = company_name
        self.api_key = api_key
        self.base_url = f"https://{company_name}.proofhub.com/api/v3"
        self.headers = {
            "X-API-KEY": api_key,
            "User-Agent": "SRVMedia-TimesheetApp (ashish.kate@srvmedia.com)",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        # Per-instance cache so a bulk push doesn't re-fetch timesheets for the
        # same project on every row.
        self._timesheet_cache: Dict[str, List[Dict]] = {}

    def test_connection(self) -> Tuple[bool, str]:
        """Test API connection"""
        try:
            response = requests.get(f"{self.base_url}/projects", headers=self.headers)
            if response.status_code == 200:
                return True, "Connection successful!"
            elif response.status_code in (401, 403):
                return False, "Invalid API key or access denied"
            else:
                return False, f"Connection failed: HTTP {response.status_code}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def get_projects(self) -> Dict[str, Dict]:
        """Fetch all projects and create name-to-ID mapping (lowercase key)."""
        try:
            response = requests.get(f"{self.base_url}/projects", headers=self.headers)
            if response.status_code != 200 or not response.text.strip():
                return {}

            data = response.json()
            all_projects = data if isinstance(data, list) else data.get('projects') or data.get('data') or []

            project_map = {}
            for p in all_projects:
                name = p.get('title') or p.get('name') or ''
                name = html.unescape(name)  # fixes &amp; → & and other HTML entities
                pid = p.get('id')
                if name and pid is not None:
                    project_map[name.strip().lower()] = {'id': pid, 'name': name.strip()}

            return project_map
        except Exception:
            return {}

    def get_projects_list(self) -> List[Dict]:
        """Projects as an alphabetically sorted list for a dropdown."""
        project_map = self.get_projects()
        projects = [{'id': v['id'], 'name': v['name']} for v in project_map.values()]
        projects.sort(key=lambda x: x['name'].lower())
        return projects

    def get_timesheets_ordered(self, project_id: str) -> List[Dict]:
        """Fetch timesheets for a project as an ordered list [{id, name}]."""
        pid = str(project_id)
        if pid in self._timesheet_cache:
            return self._timesheet_cache[pid]
        try:
            response = requests.get(
                f"{self.base_url}/projects/{project_id}/timesheets",
                headers=self.headers
            )
            if response.status_code != 200 or not response.text.strip():
                self._timesheet_cache[pid] = []
                return []

            ts_data = response.json()
            timesheets = ts_data if isinstance(ts_data, list) else ts_data.get('data') or ts_data.get('timesheets') or []

            result = []
            for ts in timesheets:
                name = html.unescape(ts.get('title') or ts.get('name') or '').strip()
                ts_id = ts.get('id')
                if ts_id is not None:
                    result.append({'id': ts_id, 'name': name})
            self._timesheet_cache[pid] = result
            return result
        except Exception:
            self._timesheet_cache[pid] = []
            return []

    def resolve_timesheet(self, project_id: str) -> Tuple[Optional[str], str, str]:
        """
        Automatically pick the timesheet to log against (BRD 9.8 / BR09).

        Returns (timesheet_id, timesheet_name, note):
          - exactly one timesheet  -> that one, note ''
          - multiple timesheets    -> the first one, note 'multiple' (flag)
          - none                   -> (None, '', 'none')
        The 'multiple' case is where an admin-controlled default-timesheet
        mapping (Tier B) will later override this automatic choice.
        """
        timesheets = self.get_timesheets_ordered(project_id)
        if not timesheets:
            return None, '', 'none'
        chosen = timesheets[0]
        note = 'multiple' if len(timesheets) > 1 else ''
        return chosen['id'], chosen['name'], note

    def create_time_entry(self, project_id: str, timesheet_id: str,
                          date: str, hours: str, mins: str,
                          status: str, description: str) -> Tuple[bool, str, str]:
        """Create a time entry"""
        payload = {
            "project": project_id,
            "timesheet_id": timesheet_id,
            "date": date,
            "logged_hours": hours,
            "logged_mins": mins,
            "status": status,
            "description": description
        }

        try:
            url = f"{self.base_url}/projects/{project_id}/timesheets/{timesheet_id}/time"
            response = requests.post(url, headers=self.headers, json=payload)

            if response.status_code in [200, 201]:
                if not response.text.strip():
                    return True, "Time entry created", ""
                response_data = response.json()
                return True, "Time entry created", response_data.get('id', '')
            else:
                if not response.text.strip():
                    return False, f"HTTP {response.status_code}: Empty response", ""
                error = response.json() if response.text else {}
                msg = error.get('message') or response.text[:200]
                return False, f"HTTP {response.status_code}: {msg}", ""
        except Exception as e:
            return False, f"Error: {str(e)}", ""
