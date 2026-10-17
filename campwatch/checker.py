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
from .recgov import Campsite, RateLimitedError, RecGovClient, RecGovError, campsite_url

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 15 * 60


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
        client: RecGovClient,
        notifier: Notifier,
        holder: CartHolder,
        poll_interval: int,
        request_delay: float = 1.0,
        today=date.today,
    ):
        self.sessions = sessions
        self.client = client
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
    ) -> dict[tuple[str, date], list[Campsite] | RecGovError]:
        """Fetch each (campground, month) once per cycle, however many watches need it."""
        needed: set[tuple[str, date]] = set()
        for watch in watches:
            months = months_for(self._future_ranges(watch))
            for target in watch.targets:
                needed.update((target["campground_id"], m) for m in months)

        data: dict[tuple[str, date], list[Campsite] | RecGovError] = {}
        for i, (campground_id, month) in enumerate(sorted(needed)):
            if i and self.request_delay:
                await asyncio.sleep(self.request_delay)
            try:
                data[(campground_id, month)] = await self.client.month_availability(
                    campground_id, month
                )
                result.requests += 1
            except RateLimitedError as exc:
                self.state.backoff_seconds = min(
                    max(self.state.backoff_seconds * 2, 60), MAX_BACKOFF_SECONDS
                )
                result.errors.append(str(exc))
                for key in needed - data.keys():
                    data[key] = exc
                return data
            except RecGovError as exc:
                result.errors.append(f"{campground_id} {month:%b %Y}: {exc}")
                data[(campground_id, month)] = exc
        self.state.backoff_seconds = 0
        return data

    def _future_ranges(self, watch: Watch) -> list[tuple[date, date]]:
        today = self.today()
        return [(max(ci, today), co) for ci, co in watch.ranges() if co > today]

    async def _check_watch(
        self, db: Session, watch: Watch, data: dict, result: CycleResult
    ) -> None:
        now = utcnow()
        ranges = self._future_ranges(watch)
        months = months_for(ranges)
        found: dict[tuple[str, date, date], tuple[dict, object]] = {}
        errors = []

        for target in watch.targets:
            month_data = [data.get((target["campground_id"], m)) for m in months]
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
                    campsites, check_in, check_out, watch.min_nights, target.get("sites") or []
                ):
                    key = (stay.campsite_id, stay.check_in, stay.check_out)
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
        if watch.action == "cart" and self.holder.available:
            await self._hold(db, watch, new)

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
                    url=campsite_url(best.campsite_id),
                    url_label="Book now",
                )
            )


def _close_openings(watch: Watch, now: datetime) -> None:
    for opening in watch.openings:
        if opening.gone_at is None:
            opening.gone_at = now


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def _dates(o: Opening) -> str:
    nights = (o.check_out - o.check_in).days
    return f"{_day(o.check_in)} to {_day(o.check_out)} ({nights} night{'s' * (nights != 1)})"


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
    first = new[0]
    count = f"{len(new)} opening{'s' * (len(new) != 1)}"
    return Notification(
        title=f"{count}: {watch.name}",
        message="\n".join(lines),
        url=campsite_url(first.campsite_id),
        url_label=f"Book site {first.site}",
    )
