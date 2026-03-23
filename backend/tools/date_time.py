"""
Date/time tool implementation.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def current_datetime(timezone_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Return current date and time details.

    Args:
        timezone_name: Optional IANA timezone (for example, "Asia/Kolkata")
    """
    tz = timezone.utc
    resolved_timezone = "UTC"

    if timezone_name:
        tz_name = str(timezone_name).strip()
        if tz_name and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tz_name)
                resolved_timezone = tz_name
            except Exception:
                resolved_timezone = "UTC"

    now = datetime.now(tz)
    utc_now = datetime.now(timezone.utc)

    return {
        "timezone": resolved_timezone,
        "iso_datetime": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "day_name": now.strftime("%A"),
        "month_name": now.strftime("%B"),
        "year": now.year,
        "utc_iso_datetime": utc_now.isoformat(),
    }

