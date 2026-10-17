"""Client for Recreation.gov's public JSON endpoints (the ones the website itself calls).

These endpoints are undocumented and can change without notice. Everything that depends on
their response shape is kept in this module.
"""

from __future__ import annotations

import re
from datetime import date

import httpx

from . import Campground, Campsite, ProviderError, RateLimitedError, looks_like_link

BASE_URL = "https://www.recreation.gov"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

_CAMPGROUND_URL = re.compile(r"/camping/campgrounds/(\d+)")


def campsite_url(campsite_id: str) -> str:
    return f"{BASE_URL}/camping/campsites/{campsite_id}"


def campground_url(campground_id: str) -> str:
    return f"{BASE_URL}/camping/campgrounds/{campground_id}"


def parse_campground_id(text: str) -> str | None:
    """Accept a bare ID or a pasted recreation.gov campground URL."""
    text = text.strip()
    if text.isdigit():
        return text
    match = _CAMPGROUND_URL.search(text)
    return match.group(1) if match else None


def parse_month(payload: dict) -> list[Campsite]:
    try:
        raw_sites = payload["campsites"]
    except (KeyError, TypeError) as exc:
        raise ProviderError("availability response has no 'campsites'") from exc

    sites = []
    for campsite_id, raw in raw_sites.items():
        availability = {
            date.fromisoformat(day[:10]): status
            for day, status in (raw.get("availabilities") or {}).items()
        }
        sites.append(
            Campsite(
                campsite_id=str(raw.get("campsite_id") or campsite_id),
                site=str(raw.get("site") or campsite_id),
                loop=str(raw.get("loop") or ""),
                campsite_type=str(raw.get("campsite_type") or ""),
                type_of_use=str(raw.get("type_of_use") or ""),
                availability=availability,
            )
        )
    return sites


class RecGovClient:
    key = "recgov"
    label = "Recreation.gov"

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    @classmethod
    def create_http(cls, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL, headers=HEADERS, timeout=20.0, follow_redirects=True, **kwargs
        )

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"request to {path} failed: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimitedError("Recreation.gov is rate limiting requests")
        if resp.status_code >= 400:
            raise ProviderError(f"{path} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{path} did not return JSON") from exc

    async def month_availability(self, campground_id: str, month: date) -> list[Campsite]:
        """All sites in a campground with their status for every night of ``month``.

        The endpoint only accepts the first day of a month.
        """
        start = month.replace(day=1).strftime("%Y-%m-%dT00:00:00.000Z")
        payload = await self._get_json(
            f"/api/camps/availability/campground/{campground_id}/month",
            params={"start_date": start},
        )
        return parse_month(payload)

    async def campground(self, campground_id: str) -> Campground:
        payload = await self._get_json(f"/api/camps/campgrounds/{campground_id}")
        info = payload.get("campground") or {}
        name = info.get("facility_name")
        if not name:
            raise ProviderError(f"campground {campground_id} not found")
        address = (info.get("addresses") or [{}])[0]
        location = _location(address.get("city"), address.get("state_code"))
        return Campground(
            provider=self.key,
            id=str(campground_id),
            name=_title(name),
            parent_name=info.get("parent_name") or "",
            location=location,
        )

    async def search_campgrounds(self, query: str, limit: int = 10) -> list[Campground]:
        """Search by name. Also accepts a pasted campground link or a bare ID."""
        query = query.strip()
        if campground_id := parse_campground_id(query):
            try:
                return [await self.campground(campground_id)]
            except RateLimitedError:
                raise
            except ProviderError:
                if query.isdigit():  # may be another system's ID
                    return []
                raise
        if looks_like_link(query):
            return []
        payload = await self._get_json(
            "/api/search",
            params={"q": query, "fq": "entity_type:campground", "size": limit, "start": 0},
        )
        results = []
        for item in payload.get("results") or []:
            if item.get("entity_type") not in (None, "campground"):
                continue
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            location = _location(item.get("city"), item.get("state_code"))
            results.append(
                Campground(
                    provider=self.key,
                    id=str(entity_id),
                    name=_title(item.get("name") or str(entity_id)),
                    parent_name=item.get("parent_name") or "",
                    location=location,
                )
            )
        return results[:limit]

    def booking_url(self, target: dict, campsite_id: str) -> str:
        return campsite_url(campsite_id)


def _title(name: str) -> str:
    """Recreation.gov stores many names in ALL CAPS."""
    return name.title() if name.isupper() else name


def _location(city: str | None, state_code: str | None) -> str:
    return ", ".join(part for part in (_title(city or ""), state_code or "") if part)
