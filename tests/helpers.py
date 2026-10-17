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


def grid_payload(start: date, end: date, units: dict[str, tuple[str, set[date]]], **extra) -> dict:
    """ReserveCalifornia /rdr/search/grid response covering nights start..end inclusive.

    ``units`` maps UnitId -> (Name, nights that are free); ``extra`` maps UnitId -> fields
    to merge into that unit. Per-night overrides go in extra[UnitId]["slices"].
    """
    nights = [start.fromordinal(n) for n in range(start.toordinal(), end.toordinal() + 1)]
    out = {}
    for unit_id, (name, free) in units.items():
        unit_extra = dict(extra.get(unit_id, {}))
        slice_overrides = unit_extra.pop("slices", {})
        slices = {}
        for night in nights:
            slot = {
                "Date": night.isoformat(),
                "IsFree": night in free,
                "IsBlocked": False,
                "IsWalkin": False,
                "MinStay": 1,
                "IsReservationDraw": False,
            }
            slot.update(slice_overrides.get(night, {}))
            slices[f"{night.isoformat()}T00:00:00"] = slot
        out[unit_id] = {
            "UnitId": int(unit_id),
            "Name": name,
            "ShortName": name,
            "AllowWebBooking": True,
            "IsWebViewable": True,
            "Slices": slices,
            **unit_extra,
        }
    return {"Message": "", "Facility": {"FacilityId": 766, "Name": "Redwood Flat", "Units": out}}


CA_PLACES = [
    {"PlaceId": 718, "Name": "Example Redwoods SP", "City": "SOMEWHERE", "State": "CA"},
    {"PlaceId": 719, "Name": "Example Beach SB", "City": "Seaside", "State": "CA"},
]
CA_FACILITIES = [
    {"FacilityId": 766, "PlaceId": 718, "Name": "Redwood Flat Campground", "AllowWebBooking": True},
    {"FacilityId": 767, "PlaceId": 718, "Name": "Creekside Loop", "AllowWebBooking": True},
    {"FacilityId": 768, "PlaceId": 718, "Name": "Group Camp", "AllowWebBooking": False},
    {"FacilityId": 801, "PlaceId": 719, "Name": "Beach Campground", "AllowWebBooking": True},
]
