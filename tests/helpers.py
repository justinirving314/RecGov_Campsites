"""Synthetic Recreation.gov payloads for tests. IDs and names are made up."""

from __future__ import annotations

import calendar
from datetime import date


def month_payload(month: date, sites: dict[str, tuple[str, set[date]]], **extra) -> dict:
    """Availability response for one month.

    ``sites`` maps campsite_id -> (site name, nights that are "Available"); every other night
    in the month is "Reserved".
    """
    days = calendar.monthrange(month.year, month.month)[1]
    campsites = {}
    for campsite_id, (name, open_nights) in sites.items():
        availabilities = {}
        for day in range(1, days + 1):
            night = month.replace(day=day)
            status = "Available" if night in open_nights else "Reserved"
            availabilities[f"{night.isoformat()}T00:00:00Z"] = status
        campsites[campsite_id] = {
            "campsite_id": campsite_id,
            "site": name,
            "loop": "Loop A",
            "campsite_type": "STANDARD NONELECTRIC",
            "type_of_use": "Overnight",
            "availabilities": availabilities,
            **extra.get(campsite_id, {}),
        }
    return {"campsites": campsites, "count": len(campsites)}
