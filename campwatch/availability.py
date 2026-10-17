"""Pure matching logic: which months to fetch, and which stays are open. No I/O here."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .recgov import AVAILABLE, Campsite


@dataclass(frozen=True)
class Stay:
    """An open run of consecutive nights at one site. ``check_out`` is the departure day."""

    campsite_id: str
    site: str
    loop: str
    check_in: date
    check_out: date

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


def nights_between(check_in: date, check_out: date) -> list[date]:
    return [check_in + timedelta(days=i) for i in range((check_out - check_in).days)]


def months_for(ranges: Iterable[tuple[date, date]]) -> list[date]:
    """First-of-month dates covering every night in the given (check_in, check_out) ranges."""
    months = {night.replace(day=1) for start, end in ranges for night in nights_between(start, end)}
    return sorted(months)


def merge_months(months: Iterable[list[Campsite]]) -> list[Campsite]:
    """Combine month-by-month responses for one campground into one entry per site."""
    merged: dict[str, Campsite] = {}
    for month in months:
        for site in month:
            if site.campsite_id in merged:
                merged[site.campsite_id].availability.update(site.availability)
            else:
                merged[site.campsite_id] = Campsite(
                    campsite_id=site.campsite_id,
                    site=site.site,
                    loop=site.loop,
                    campsite_type=site.campsite_type,
                    type_of_use=site.type_of_use,
                    availability=dict(site.availability),
                )
    return list(merged.values())


def normalize_site(name: str) -> str:
    """Treat "012", "12" and " 12 " as the same site."""
    return name.strip().upper().lstrip("0") or "0"


def find_stays(
    campsites: Iterable[Campsite],
    check_in: date,
    check_out: date,
    min_nights: int | None = None,
    sites: Iterable[str] = (),
) -> list[Stay]:
    """Open stays inside [check_in, check_out).

    ``min_nights=None`` means the whole range must be open at a single site. A smaller value
    accepts any run of at least that many consecutive nights, reported as its longest run.
    ``sites`` limits the search to those site names; empty means any site.
    """
    window = nights_between(check_in, check_out)
    if not window:
        return []
    needed = len(window) if min_nights is None else min(min_nights, len(window))
    wanted = {normalize_site(s) for s in sites}

    stays = []
    for site in campsites:
        if wanted and normalize_site(site.site) not in wanted:
            continue
        if site.type_of_use.lower() == "day":
            continue
        run: list[date] = []
        for night in [*window, None]:
            if night is not None and site.availability.get(night) == AVAILABLE:
                run.append(night)
                continue
            if len(run) >= needed:
                stays.append(
                    Stay(
                        campsite_id=site.campsite_id,
                        site=site.site,
                        loop=site.loop,
                        check_in=run[0],
                        check_out=run[-1] + timedelta(days=1),
                    )
                )
            run = []
    stays.sort(key=lambda s: (-s.nights, s.check_in, s.site))
    return stays
