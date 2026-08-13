from datetime import datetime
from typing import Optional
import re

def parse_date_flexible(date_string: str) -> Optional[str]:
    """
    Parse dates in accepted formats and return YYYY-MM-DD format.
    Accepted formats:
        DD-MM-YYYY  (20-05-2026)  ← preferred
        DD/MM/YYYY  (20/05/2026)
        YYYY-MM-DD  (2026-05-20)
        DD-Mon-YYYY (20-May-2026)
    
    Rejected formats:
        MM/DD/YYYY  (05/20/2026)  ← not accepted
        MM-DD-YYYY  (05-20-2026)  ← not accepted
    """
    if not date_string or not isinstance(date_string, str):
        return None

    date_string = date_string.strip()

    # Pattern 1: DD-MM-YYYY or DD/MM/YYYY (with dashes or slashes)
    # Always treat as DD-MM-YYYY — never MM-DD-YYYY
    match = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$', date_string)
    if match:
        day, month, year = match.groups()
        try:
            parsed_date = datetime(int(year), int(month), int(day))
            return parsed_date.strftime('%Y-%m-%d')
        except ValueError:
            return None  # invalid date, reject it

    # Pattern 2: YYYY-MM-DD (ISO format)
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', date_string)
    if match:
        year, month, day = match.groups()
        try:
            parsed_date = datetime(int(year), int(month), int(day))
            return parsed_date.strftime('%Y-%m-%d')
        except ValueError:
            return None

    # Pattern 3: DD-Mon-YYYY or DD Mon YYYY (text month — unambiguous)
    text_month_formats = [
        '%d-%b-%Y',   # 20-May-2026
        '%d %b %Y',   # 20 May 2026
        '%d-%B-%Y',   # 20-May-2026 (full month)
        '%d %B %Y',   # 20 May 2026 (full month)
    ]

    for fmt in text_month_formats:
        try:
            parsed_date = datetime.strptime(date_string, fmt)
            return parsed_date.strftime('%Y-%m-%d')
        except:
            continue

    return None
