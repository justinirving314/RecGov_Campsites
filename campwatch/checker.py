"""The polling loop: fetch availability, find stays, record openings, alert, optionally hold."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from .availability import find_stays, merge_months, months_for
from .booker import CartHolder
from .models import Opening, Watch, utcnow
from .notify import Notification, Notifier
from .providers import Campsite, Provider, ProviderError, RateLimitedError

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 15 * 60
MAX_HOLDS_PER_CYCLE = 3  # night by night: separate pieces held in one check


@dataclass
class CycleResult:
    started_at: datetime
    watches_checked: int = 0
    requests: int = 0
    new_openings: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class CheckerState:
    last_cycle: CycleResult | None = None
    next_cycle_at: datetime | None = None
    backoff_seconds: int = 0


class Checker:
    def __init__(
        self,
        sessions: sessionmaker,
        providers: dict[str, Provider],
        notifier: Notifier,
        holder: CartHolder,
        poll_interval: int,
        request_delay: float = 1.0,
        today=date.today,
    ):
        self.sessions = sessions
        self.providers = providers
        self.notifier = notifier
        self.holder = holder
        self.poll_interval = poll_interval
        self.request_delay = request_delay
        self.today = today
        self.state = CheckerState()
        self._lock = asyncio.Lock()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check(None)
            except Exception:  # keep the loop alive no matter what
                log.exception("check cycle crashed")
            delay = self.poll_interval + self.state.backoff_seconds
            delay *= random.uniform(0.9, 1.1)  # don't hit the site on an exact beat
            self.state.next_cycle_at = datetime.now().astimezone() + timedelta(seconds=delay)
            await asyncio.sleep(delay)

    async def check(self, watch_id: int | None) -> CycleResult:
        """Check every active watch, or just one (``watch_id``) for "Check now"."""
        async with self._lock:
            result = CycleResult(started_at=datetime.now().astimezone())
            with self.sessions() as db:
                watches = self._load_watches(db, watch_id)
                db.commit()
                data = await self._fetch(watches, result)
                for watch in watches:
                    try:
                        await self._check_watch(db, watch, data, result)
                        db.commit()
                    except StaleDataError:  # watch was edited or deleted mid-cycle
                        db.rollback()
            if watch_id is None:
                self.state.last_cycle = result
            return result

    def _load_watches(self, db: Session, watch_id: int | None) -> list[Watch]:
        query = select(Watch).where(Watch.active.is_(True))
        if watch_id is not None:
            query = select(Watch).where(Watch.id == watch_id)
        watches = []
        for watch in db.scalars(query):
            if all(check_out <= self.today() for _, check_out in watch.ranges()):
                watch.active = False
                watch.status_note = "All dates have passed."
                _close_openings(watch, utcnow())
                continue
            watches.append(watch)
        return watches

    async def _fetch(
        self, watches: list[Watch], result: CycleResult
    ) -> dict[tuple[str, str, date], list[Campsite] | ProviderError]:
        """Fetch each (provider, campground, month) once per cycle, however many watches need it."""
        needed: set[tuple[str, str, date]] = set()
        for watch in watches:
            months = self._months(watch)
            for target in watch.targets:
                needed.update((_provider(target), target["campground_id"], m) for m in months)

        data: dict[tuple[str, str, date], list[Campsite] | ProviderError] = {}
        rate_limited = False
        sent = 0
        for key in sorted(needed):
            if key in data:  # skipped after that system rate limited us
                continue
            provider_key, campground_id, month = key
            provider = self.providers.get(provider_key)
            if provider is None:
                data[key] = ProviderError(f"unknown reservation system {provider_key!r}")
                result.errors.append(str(data[key]))
                continue
            if sent and self.request_delay:
                await asyncio.sleep(self.request_delay)
            sent += 1
            try:
                data[key] = await provider.month_availability(campground_id, month)
                result.requests += 1
            except RateLimitedError as exc:
                # Stop asking this system for the rest of the cycle; others carry on.
                rate_limited = True
                result.errors.append(str(exc))
                for other in needed:
                    if other[0] == provider_key and other not in data:
                        data[other] = exc
            except ProviderError as exc:
                result.errors.append(f"{provider.label} {campground_id} {month:%b %Y}: {exc}")
                data[key] = exc
        if rate_limited:
            self.state.backoff_seconds = min(
                max(self.state.backoff_seconds * 2, 60), MAX_BACKOFF_SECONDS
            )
        else:
            self.state.backoff_seconds = 0
        return data

    def _future_ranges(self, watch: Watch) -> list[tuple[date, date]]:
        today = self.today()
        return [(max(ci, today), co) for ci, co in watch.ranges() if co > today]

    def _months(self, watch: Watch) -> list[date]:
        if watch.piecemeal:  # only months that still have a night to find
            return sorted({n.replace(day=1) for n in watch.needed_nights(self.today())})
        return months_for(self._future_ranges(watch))

    async def _check_watch(
        self, db: Session, watch: Watch, data: dict, result: CycleResult
    ) -> None:
        now = utcnow()
        today = self.today()
        needed = watch.needed_nights(today) if watch.piecemeal else None
        if needed is not None and not needed:
            watch.update_completion(today)
            _close_openings(watch, now)
            watch.last_checked_at = now
            result.watches_checked += 1
            return
        ranges = self._future_ranges(watch)
        months = self._months(watch)
        min_nights = 1 if watch.piecemeal else watch.min_nights
        found: dict[tuple[str, str, date, date], tuple[dict, object]] = {}
        errors = []

        for target in watch.targets:
            provider_key = _provider(target)
            month_data = [data.get((provider_key, target["campground_id"], m)) for m in months]
            failures = [d for d in month_data if not isinstance(d, list)]
            if failures:
                errors.append(
                    f"{target.get('campground_name') or target['campground_id']}: "
                    f"{failures[0] or 'not fetched'}"
                )
                continue
            campsites = merge_months(month_data)
            for check_in, check_out in ranges:
                for stay in find_stays(
                    campsites,
                    check_in,
                    check_out,
                    min_nights,
                    target.get("sites") or [],
                    needed=needed,
                ):
                    key = (provider_key, stay.campsite_id, stay.check_in, stay.check_out)
                    found.setdefault(key, (target, stay))

        result.watches_checked += 1
        watch.last_checked_at = now
        watch.last_error = "; ".join(errors)[:500] or None

        current = {o.key: o for o in watch.openings if o.gone_at is None}
        for key, opening in current.items():
            if key in found:
                opening.last_seen_at = now
            elif not errors:  # don't mark gone when we simply couldn't fetch
                opening.gone_at = now

        new = []
        for key, (target, stay) in found.items():
            if key in current:
                continue
            opening = Opening(
                provider=key[0],
                booking_url=self.providers[key[0]].booking_url(target, stay.campsite_id),
                campground_id=target["campground_id"],
                campground_name=target.get("campground_name") or target["campground_id"],
                campsite_id=stay.campsite_id,
                site=stay.site,
                loop=stay.loop,
                check_in=stay.check_in,
                check_out=stay.check_out,
                first_seen_at=now,
                last_seen_at=now,
            )
            watch.openings.append(opening)
            new.append(opening)

        if not new:
            return
        result.new_openings += len(new)
        db.commit()
        await self.notifier.send(_alert(watch, new))
        holdable = [o for o in new if o.provider == "recgov"]  # cart holds are Recreation.gov only
        if watch.action == "cart" and holdable and self.holder.available:
            if watch.piecemeal:
                await self._hold_pieces(watch, holdable)
            else:
                await self._hold(db, watch, holdable)

    async def _hold_pieces(self, watch: Watch, candidates: list[Opening]) -> None:
        """Night by night: hold as many still-needed nights as this check's openings allow.

        Each hold covers only nights still needed, and the watch keeps going until every
        night is booked, held or skipped.
        """
        today = self.today()
        attempts = 0
        for opening in sorted(candidates, key=_piece_priority(watch)):
            run = _longest_run(opening, watch.needed_nights(today))
            if not run:
                continue  # an earlier hold this check already covered these nights
            if attempts == MAX_HOLDS_PER_CYCLE:
                break
            attempts += 1
            check_in, check_out = run[0], run[-1] + timedelta(days=1)
            hold = await self.holder.hold(opening.campsite_id, check_in, check_out)
            opening.cart_status = "held" if hold.ok else "failed"
            opening.cart_message = hold.message[:500]
            if not hold.ok:
                await self.notifier.send(
                    Notification(
                        title=f"Couldn't hold site {opening.site}. Book it yourself",
                        message=f"{opening.campground_name}, {_span(check_in, check_out)}. "
                        f"{hold.message}",
                        url=opening.book_url,
                        url_label="Book now",
                    )
                )
                continue
            watch.mark_nights(run, "held", **_night_info(opening))
            still = sorted(watch.needed_nights(today))
            await self.notifier.send(
                Notification(
                    title=f"In your cart: site {opening.site}, {opening.campground_name}",
                    message=f"{_span(check_in, check_out)}. Check out on Recreation.gov within "
                    "about 15 minutes, or the hold expires. "
                    + (
                        f"Still looking for {_night_list(still)}."
                        if still
                        else "That covers every night."
                    ),
                    url="https://www.recreation.gov/cart",
                    url_label="Go to cart",
                )
            )
        watch.update_completion(today)

    async def _hold(self, db: Session, watch: Watch, new: list[Opening]) -> None:
        best = max(new, key=lambda o: ((o.check_out - o.check_in).days, -o.check_in.toordinal()))
        hold = await self.holder.hold(best.campsite_id, best.check_in, best.check_out)
        best.cart_status = "held" if hold.ok else "failed"
        best.cart_message = hold.message[:500]
        if hold.ok:
            watch.active = False
            watch.status_note = f"Paused after holding site {best.site} in your cart."
            await self.notifier.send(
                Notification(
                    title=f"In your cart: site {best.site}, {best.campground_name}",
                    message=f"{_dates(best)}. Sign in to Recreation.gov and check out within "
                    "about 15 minutes, or the hold expires.",
                    url="https://www.recreation.gov/cart",
                    url_label="Go to cart",
                )
            )
        else:
            await self.notifier.send(
                Notification(
                    title=f"Couldn't hold site {best.site}. Book it yourself",
                    message=f"{best.campground_name}, {_dates(best)}. {hold.message}",
                    url=best.book_url,
                    url_label="Book now",
                )
            )


def _provider(target: dict) -> str:
    return target.get("provider") or "recgov"


def _night_info(opening: Opening) -> dict:
    """What a booked or held night remembers about where it is."""
    return {
        "site": opening.site,
        "campground_name": opening.campground_name,
        "campsite_id": opening.campsite_id,
        "provider": opening.provider,
        "opening_id": opening.id,
    }


def _longest_run(opening: Opening, needed: set[date]) -> list[date]:
    """The longest stretch of the opening's nights that's still needed (earliest on a tie)."""
    best: list[date] = []
    run: list[date] = []
    for i in range((opening.check_out - opening.check_in).days + 1):
        night = opening.check_in + timedelta(days=i)
        if night < opening.check_out and night in needed:
            run.append(night)
            continue
        if len(run) > len(best):
            best = run
        run = []
    return best


def _piece_priority(watch: Watch):
    """Prefer extending a stay at a site you already have, then longer, then earlier."""
    covered = watch.nights or {}

    def key(o: Opening):
        neighbours = (o.check_in - timedelta(days=1), o.check_out)
        same_site = any(
            covered.get(n.isoformat(), {}).get("campsite_id") == o.campsite_id for n in neighbours
        )
        return (not same_site, -(o.check_out - o.check_in).days, o.check_in)

    return key


def _close_openings(watch: Watch, now: datetime) -> None:
    for opening in watch.openings:
        if opening.gone_at is None:
            opening.gone_at = now


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def _span(check_in: date, check_out: date) -> str:
    nights = (check_out - check_in).days
    return f"{_day(check_in)} to {_day(check_out)} ({nights} night{'s' * (nights != 1)})"


def _dates(o: Opening) -> str:
    return _span(o.check_in, o.check_out)


def _night_list(nights: list[date]) -> str:
    shown = ", ".join(_day(n) for n in nights[:6])
    return f"{shown} and {len(nights) - 6} more" if len(nights) > 6 else shown


def _alert(watch: Watch, new: list[Opening]) -> Notification:
    by_campground: dict[str, list[Opening]] = defaultdict(list)
    for o in new:
        by_campground[o.campground_name].append(o)
    lines = []
    for name, openings in by_campground.items():
        lines.append(name)
        lines += [f"  Site {o.site}: {_dates(o)}" for o in openings[:5]]
        if len(openings) > 5:
            lines.append(f"  and {len(openings) - 5} more")
    if watch.piecemeal:
        lines.append(
            "After you book, tap “I booked it” in CampWatch so it keeps looking for the rest."
        )
    first = new[0]
    count = f"{len(new)} opening{'s' * (len(new) != 1)}"
    return Notification(
        title=f"{count}: {watch.name}",
        message="\n".join(lines),
        url=first.book_url,
        url_label=f"Book site {first.site}",
    )
