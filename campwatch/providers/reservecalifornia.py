"""Client for ReserveCalifornia, the booking system for California State Parks.

ReserveCalifornia runs on Tyler Technologies' "UseDirect" platform. Like Recreation.gov's,
these JSON endpoints are the ones the website calls; they're undocumented and can change.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import date

import httpx

from . import (
    AVAILABLE,
    Campground,
    Campsite,
    ProviderError,
    RateLimitedError,
    looks_like_link,
)

API_URL = "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com"
SITE_URL = "https://www.reservecalifornia.com"
METADATA_TTL_SECONDS = 24 * 3600

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": SITE_URL,
    "Referer": f"{SITE_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

# reservecalifornia.com/park/718/766 (and the older Default.aspx#!park/718/766)
_PARK_LINK = re.compile(r"reservecalifornia\.com/.*?park/(\d+)(?:/(\d+))?", re.IGNORECASE)
_SITE_PREFIX = re.compile(r"^(camp)?site\s*#?\s*", re.IGNORECASE)


def park_url(park_id: str, facility_id: str) -> str:
    if not park_id:
        return SITE_URL
    return f"{SITE_URL}/park/{park_id}/{facility_id}"


def parse_link(text: str) -> tuple[str, str | None] | None:
    """(park_id, facility_id or None) from a pasted ReserveCalifornia link."""
    match = _PARK_LINK.search(text)
    return (match.group(1), match.group(2)) if match else None


def parse_grid(payload: dict, month: date) -> list[Campsite]:
    """Sites and nightly status for one month from a /rdr/search/grid response."""
    facility = payload.get("Facility") if isinstance(payload, dict) else None
    if not isinstance(facility, dict):
        message = payload.get("Message") if isinstance(payload, dict) else None
        raise ProviderError(message or "availability response has no 'Facility'")

    sites = []
    for unit_id, unit in (facility.get("Units") or {}).items():
        if unit.get("IsWebViewable") is False or unit.get("AllowWebBooking") is False:
            continue  # can't be booked online
        availability: dict[date, str] = {}
        min_stay: dict[date, int] = {}
        for key, slot in (unit.get("Slices") or {}).items():
            night = date.fromisoformat(str(slot.get("Date") or key)[:10])
            if (night.year, night.month) != (month.year, month.month):
                continue
            availability[night] = _status(slot)
            if slot.get("MinStay"):
                min_stay[night] = int(slot["MinStay"])
        name = str(unit.get("ShortName") or unit.get("Name") or unit_id)
        sites.append(
            Campsite(
                campsite_id=str(unit.get("UnitId") or unit_id),
                site=_SITE_PREFIX.sub("", name).strip() or name,
                availability=availability,
                min_stay=min_stay,
            )
        )
    return sites


def _status(slot: dict) -> str:
    if slot.get("IsReservationDraw"):
        return "Lottery"
    if slot.get("IsWalkin"):
        return "Walk-up"
    if slot.get("IsBlocked"):
        return "Not Available"
    return AVAILABLE if slot.get("IsFree") else "Reserved"


class ReserveCaliforniaClient:
    key = "reservecalifornia"
    label = "CA State Parks"

    def __init__(self, http: httpx.AsyncClient, today: Callable[[], date] = date.today):
        self._http = http
        self._today = today
        self._places: dict[str, dict] = {}
        self._facilities: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._metadata_lock = asyncio.Lock()

    @classmethod
    def create_http(cls, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_URL, headers=HEADERS, timeout=30.0, follow_redirects=True, **kwargs
        )

    async def _request(self, method: str, path: str, **kwargs):
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"request to {path} failed: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimitedError("ReserveCalifornia is rate limiting requests")
        if resp.status_code >= 400:
            raise ProviderError(f"{path} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{path} did not return JSON") from exc

    async def month_availability(self, campground_id: str, month: date) -> list[Campsite]:
        first = month.replace(day=1)
        next_month = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        start = max(first, self._today())  # the grid rejects dates in the past
        if start >= next_month:
            return []
        payload = await self._request(
            "POST",
            "/rdr/search/grid",
            json={
                "FacilityId": int(campground_id),
                "StartDate": start.strftime("%m-%d-%Y"),
                "EndDate": next_month.strftime("%m-%d-%Y"),
                "WebOnly": True,
                "InSeasonOnly": True,
                "UnitSort": "orderby",
            },
        )
        return parse_grid(payload, first)

    async def _metadata(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """Every park ("place") and facility, cached for a day. Search runs on this list."""
        async with self._metadata_lock:
            if not self._facilities or time.monotonic() - self._loaded_at > METADATA_TTL_SECONDS:
                places = await self._request("GET", "/rdr/fd/places")
                facilities = await self._request("GET", "/rdr/fd/facilities")
                self._places = {str(p["PlaceId"]): p for p in _rows(places) if p.get("PlaceId")}
                self._facilities = {
                    str(f["FacilityId"]): f
                    for f in _rows(facilities)
                    if f.get("FacilityId") and f.get("AllowWebBooking") is not False
                }
                self._loaded_at = time.monotonic()
        return self._places, self._facilities

    def _campground(self, facility: dict, places: dict[str, dict]) -> Campground:
        park_id = str(facility.get("PlaceId") or "")
        place = places.get(park_id, {})
        city = (place.get("City") or "").strip()
        location = ", ".join(p for p in (city.title() if city.isupper() else city, "CA") if p)
        return Campground(
            provider=self.key,
            id=str(facility["FacilityId"]),
            name=(facility.get("Name") or "").strip() or f"Facility {facility['FacilityId']}",
            parent_name=(place.get("Name") or "").strip(),
            location=location,
            park_id=park_id,
        )

    async def campground(self, campground_id: str) -> Campground:
        places, facilities = await self._metadata()
        facility = facilities.get(str(campground_id))
        if facility is None:
            raise ProviderError(f"facility {campground_id} not found")
        return self._campground(facility, places)

    async def search_campgrounds(self, query: str, limit: int = 10) -> list[Campground]:
        """Match park or facility names. Also accepts a ReserveCalifornia link or facility ID."""
        query = query.strip()
        link = parse_link(query)
        if link is None and looks_like_link(query):
            return []
        places, facilities = await self._metadata()

        if link:
            park_id, facility_id = link
            if facility_id:
                matches = [facilities[facility_id]] if facility_id in facilities else []
            else:
                matches = [f for f in facilities.values() if str(f.get("PlaceId")) == park_id]
        elif query.isdigit():
            matches = [facilities[query]] if query in facilities else []
        else:
            words = query.lower().split()
            scored = []
            for facility in facilities.values():
                park = places.get(str(facility.get("PlaceId")), {})
                park_name = (park.get("Name") or "").lower()
                text = f"{(facility.get('Name') or '').lower()} {park_name}"
                if all(word in text for word in words):
                    # Park-name hits first: "big basin" should list Big Basin's campgrounds.
                    scored.append((0 if words[0] in park_name else 1, text, facility))
            scored.sort(key=lambda item: (item[0], item[1]))
            matches = [facility for _, _, facility in scored]
        cap = 50 if link else limit  # a park link lists all of that park's campgrounds
        return [self._campground(f, places) for f in matches[:cap]]

    def booking_url(self, target: dict, campsite_id: str) -> str:
        return park_url(str(target.get("park_id") or ""), str(target["campground_id"]))


def _rows(data) -> list[dict]:
    """The metadata endpoints return a list; tolerate an id-keyed dict too."""
    rows = data.values() if isinstance(data, dict) else data or []
    return [row for row in rows if isinstance(row, dict)]
