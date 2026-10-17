"""Reservation systems CampWatch can watch, and the types they share."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

AVAILABLE = "Available"


class ProviderError(Exception):
    """A reservation system returned an error or a response we couldn't parse."""


class RateLimitedError(ProviderError):
    """The reservation system answered 429 Too Many Requests."""


@dataclass(frozen=True)
class Campground:
    provider: str
    id: str
    name: str
    parent_name: str = ""
    location: str = ""
    park_id: str = ""  # ReserveCalifornia only: the park the facility belongs to


@dataclass
class Campsite:
    campsite_id: str
    site: str
    loop: str = ""
    campsite_type: str = ""
    type_of_use: str = ""
    # Night (check-in date) -> status string; only "Available" is bookable.
    availability: dict[date, str] = field(default_factory=dict)
    # Night -> minimum nights for a stay starting that night, where the system reports it.
    min_stay: dict[date, int] = field(default_factory=dict)


class Provider(Protocol):
    key: str
    label: str

    async def month_availability(self, campground_id: str, month: date) -> list[Campsite]: ...

    async def campground(self, campground_id: str) -> Campground: ...

    async def search_campgrounds(self, query: str, limit: int = 10) -> list[Campground]: ...

    def booking_url(self, target: dict, campsite_id: str) -> str: ...


def looks_like_link(text: str) -> bool:
    """A pasted URL; each provider only resolves its own links."""
    lowered = text.strip().lower()
    return (
        "://" in lowered or lowered.startswith("www.") or ".com/" in lowered or ".gov/" in lowered
    )
