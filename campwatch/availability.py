"""Pure matching logic: which months to fetch, and which stays are open. No I/O here."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .providers import AVAILABLE, Campsite


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


_SITE_RANGE = re.compile(r"^([A-Z]*)(\d+)-([A-Z]*)(\d+)$")
_SITE_NUMBER = re.compile(r"^([A-Z]*)(\d+)[A-Z]*$")


def _compact(name: str) -> str:
    return re.sub(r"\s+", "", name.upper())


def normalize_site(name: str) -> str:
    """Treat "012", "12" and " 12 " (and "A05" and "a5") as the same site."""
    return re.sub(r"\d+", lambda m: str(int(m.group())), _compact(name))


def parse_site_range(entry: str) -> tuple[str, int, int] | None:
    """("A", 1, 10) for "A1-A10" or "a1 - 10"; None if the entry isn't a range.

    Both ends must share a letter prefix, so a site literally named "A-12" isn't a range.
    """
    match = _SITE_RANGE.match(_compact(entry))
    if not match:
        return None
    prefix, start, end_prefix, end = match.groups()
    if end_prefix and end_prefix != prefix:
        return None
    return prefix, int(start), int(end)


def site_matcher(entries: Iterable[str]) -> Callable[[str], bool] | None:
    """A test for site names from entries like "12", "A5", "10-25" or "A1-A10".

    Returns None when there are no entries, meaning any site will do. A site matches if
    its name equals an entry, or its letter prefix matches a range and its number is in it
    ("12A" counts as 12).
    """
    entries = [e for e in entries if e.strip()]
    if not entries:
        return None
    exact = {normalize_site(e) for e in entries}
    ranges = [r for e in entries if (r := parse_site_range(e))]

    def matches(name: str) -> bool:
        if normalize_site(name) in exact:
            return True
        number = _SITE_NUMBER.match(_compact(name))
        if not number:
            return False
        prefix, value = number.group(1), int(number.group(2))
        return any(prefix == p and min(lo, hi) <= value <= max(lo, hi) for p, lo, hi in ranges)

    return matches


def find_stays(
    campsites: Iterable[Campsite],
    check_in: date,
    check_out: date,
    min_nights: int | None = None,
    sites: Iterable[str] = (),
    needed: set[date] | None = None,
) -> list[Stay]:
    """Open stays inside [check_in, check_out).

    ``min_nights=None`` means the whole range must be open at a single site. A smaller value
    accepts any run of at least that many consecutive nights, reported as its longest run.
    ``sites`` limits the search to those sites and ranges (see ``site_matcher``); empty
    means any site.
    ``needed``, if given, is the set of nights still wanted; other nights break a run.
    """
    window = nights_between(check_in, check_out)
    if not window:
        return []
    shortest = len(window) if min_nights is None else min(min_nights, len(window))
    wanted = site_matcher(sites)

    stays = []
    for site in campsites:
        if wanted and not wanted(site.site):
            continue
        if site.type_of_use.lower() == "day":
            continue
        run: list[date] = []
        for night in [*window, None]:
            if (
                night is not None
                and site.availability.get(night) == AVAILABLE
                and (needed is None or night in needed)
            ):
                run.append(night)
                continue
            if run and len(run) >= shortest:
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
