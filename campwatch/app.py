"""FastAPI app: JSON API under /api, the web app (an installable PWA) at /."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .auth import COOKIE_NAME, SESSION_DAYS, make_token, password_ok, require_login, token_valid
from .availability import nights_between
from .booker import CartHolder
from .checker import Checker
from .config import Settings
from .models import Opening, Watch, make_session_factory, utcnow
from .notify import Notification, Notifier
from .providers import Provider, ProviderError
from .providers.recgov import RecGovClient
from .providers.reservecalifornia import ReserveCaliforniaClient
from .schemas import (
    CampgroundOut,
    CampgroundSearchOut,
    LoginIn,
    NightsPatch,
    OpeningOut,
    SiteOut,
    WatchIn,
    WatchOut,
    WatchPatch,
)

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    today: Callable[[], date] = date.today,
) -> FastAPI:
    """``transport`` and ``today`` exist so tests can fake the network and the calendar."""
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not settings.auth_required:
        log.warning("CAMPWATCH_APP_PASSWORD is not set: anyone who can reach this app can use it")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with (
            RecGovClient.create_http(transport=transport) as recgov_http,
            ReserveCaliforniaClient.create_http(transport=transport) as reservecalifornia_http,
            httpx.AsyncClient(timeout=20.0, transport=transport) as http,
        ):
            providers: list[Provider] = [
                RecGovClient(recgov_http),
                ReserveCaliforniaClient(reservecalifornia_http, today=today),
            ]
            app.state.providers = {p.key: p for p in providers}
            app.state.notifier = Notifier(settings, http)
            app.state.holder = CartHolder(settings)
            app.state.checker = Checker(
                app.state.sessions,
                app.state.providers,
                app.state.notifier,
                app.state.holder,
                poll_interval=settings.poll_interval_seconds,
                request_delay=settings.request_delay_seconds,
                today=today,
            )
            task = None
            if settings.run_scheduler:
                task = asyncio.create_task(app.state.checker.run_forever())
            yield
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="CampWatch", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = make_session_factory(settings.db_url)
    app.state.today = today

    app.include_router(public)
    app.include_router(api, dependencies=[Depends(require_login)])
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
    return app


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as db:
        yield db


DB = Annotated[Session, Depends(get_db)]

public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api")


# --- Session -------------------------------------------------------------------------------


@public.get("/session")
def session_info(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    logged_in = not settings.auth_required or token_valid(
        settings.session_secret, request.cookies.get(COOKIE_NAME)
    )
    return {"auth_required": settings.auth_required, "logged_in": logged_in}


@public.post("/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(body: LoginIn, request: Request, response: Response) -> None:
    settings: Settings = request.app.state.settings
    if settings.auth_required and not password_ok(
        settings.app_password.get_secret_value(), body.password
    ):
        await asyncio.sleep(1)  # slow down password guessing
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    response.set_cookie(
        COOKIE_NAME,
        make_token(settings.session_secret),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


@public.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


# --- Watches -------------------------------------------------------------------------------


def _watch_out(watch: Watch) -> WatchOut:
    out = WatchOut.model_validate(watch)
    out.openings = [o for o in out.openings if o.gone_at is None]
    return out


def _get_watch(db: Session, watch_id: int) -> Watch:
    watch = db.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Watch not found")
    return watch


def _default_name(body: WatchIn) -> str:
    first = body.targets[0]
    name = first.campground_name or f"Campground {first.campground_id}"
    if len(body.targets) > 1:
        name += f" +{len(body.targets) - 1}"
    start = min(r.check_in for r in body.date_ranges)
    return f"{name}, {start:%b} {start.day}"[:100]


def _apply(watch: Watch, body: WatchIn) -> None:
    watch.name = body.name.strip() or _default_name(body)
    watch.targets = [t.model_dump() for t in body.targets]
    watch.date_ranges = [r.model_dump(mode="json") for r in body.date_ranges]
    watch.min_nights = body.min_nights
    watch.piecemeal = body.piecemeal
    watch.action = body.action
    watch.active = body.active
    if body.active:
        watch.status_note = None
    watch.prune_nights()  # keep progress for nights still in the dates


@api.get("/watches")
def list_watches(db: DB) -> list[WatchOut]:
    watches = db.scalars(select(Watch).order_by(Watch.active.desc(), Watch.created_at.desc()))
    return [_watch_out(w) for w in watches]


@api.post("/watches", status_code=status.HTTP_201_CREATED)
def create_watch(body: WatchIn, db: DB) -> WatchOut:
    watch = Watch(openings=[])
    _apply(watch, body)
    db.add(watch)
    db.commit()
    return _watch_out(watch)


@api.get("/watches/{watch_id}")
def get_watch(watch_id: int, db: DB) -> WatchOut:
    return _watch_out(_get_watch(db, watch_id))


@api.put("/watches/{watch_id}")
def update_watch(watch_id: int, body: WatchIn, request: Request, db: DB) -> WatchOut:
    watch = _get_watch(db, watch_id)
    criteria = (watch.targets, watch.date_ranges, watch.min_nights, watch.piecemeal)
    _apply(watch, body)
    if criteria != (watch.targets, watch.date_ranges, watch.min_nights, watch.piecemeal):
        # Openings found under the old criteria may not match the new ones. Close them
        # (keeping the history) so the next check starts fresh and can alert again.
        now = utcnow()
        for opening in watch.openings:
            if opening.gone_at is None:
                opening.gone_at = now
        watch.last_error = None
    watch.update_completion(request.app.state.today())  # new dates restart a finished watch
    db.commit()
    return _watch_out(watch)


@api.patch("/watches/{watch_id}/nights")
def set_nights(watch_id: int, body: NightsPatch, request: Request, db: DB) -> WatchOut:
    """Mark nights booked, held or skipped (or needed again) on a night-by-night watch."""
    watch = _get_watch(db, watch_id)
    if not watch.piecemeal:
        raise HTTPException(422, "Only night-by-night watches track individual nights")
    outside = set(body.nights) - set(watch.all_nights())
    if outside:
        raise HTTPException(422, f"{min(outside)} isn't one of this watch's nights")
    watch.mark_nights(body.nights, None if body.status == "needed" else body.status)
    watch.update_completion(request.app.state.today())
    db.commit()
    return _watch_out(watch)


@api.patch("/watches/{watch_id}")
def pause_or_resume(watch_id: int, body: WatchPatch, db: DB) -> WatchOut:
    watch = _get_watch(db, watch_id)
    watch.active = body.active
    if body.active:
        watch.status_note = None
    db.commit()
    return _watch_out(watch)


@api.delete("/watches/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watch(watch_id: int, db: DB) -> None:
    db.delete(_get_watch(db, watch_id))
    db.commit()


@api.post("/watches/{watch_id}/check")
async def check_now(watch_id: int, request: Request) -> WatchOut:
    with request.app.state.sessions() as db:
        _get_watch(db, watch_id)
    await request.app.state.checker.check(watch_id)
    with request.app.state.sessions() as db:
        return _watch_out(_get_watch(db, watch_id))


@api.post("/openings/{opening_id}/booked")
def mark_booked(opening_id: int, request: Request, db: DB) -> WatchOut:
    """You booked this opening. Night by night: those nights are done, keep looking for the
    rest. Other watches: the watch has done its job, so it pauses."""
    opening = db.get(Opening, opening_id)
    if opening is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Opening not found")
    watch = opening.watch
    opening.cart_status = "booked"
    nights = nights_between(opening.check_in, opening.check_out)
    if watch.piecemeal:
        in_plan = set(watch.all_nights())
        watch.mark_nights(
            [n for n in nights if n in in_plan],
            "booked",
            site=opening.site,
            campground_name=opening.campground_name,
            campsite_id=opening.campsite_id,
            provider=opening.provider,
            opening_id=opening.id,
        )
        watch.update_completion(request.app.state.today())
    else:
        watch.active = False
        watch.status_note = f"Booked site {opening.site}, {opening.campground_name}."
    db.commit()
    return _watch_out(watch)


@api.get("/openings")
def list_openings(db: DB, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[OpeningOut]:
    openings = db.scalars(select(Opening).order_by(Opening.first_seen_at.desc()).limit(limit))
    return [OpeningOut.model_validate(o) for o in openings]


# --- Campground lookup ---------------------------------------------------------------------


ProviderKey = Annotated[str, Query(pattern=r"^[a-z]+$")]


@api.get("/campgrounds/search")
async def search_campgrounds(
    request: Request, q: Annotated[str, Query(min_length=2, max_length=200)]
) -> CampgroundSearchOut:
    """Search every reservation system at once. One failing doesn't hide the others' results."""
    providers: list[Provider] = list(request.app.state.providers.values())
    outcomes = await asyncio.gather(
        *(p.search_campgrounds(q) for p in providers), return_exceptions=True
    )
    results, errors = [], []
    for provider, outcome in zip(providers, outcomes, strict=True):
        if isinstance(outcome, ProviderError):
            errors.append(f"{provider.label}: {outcome}")
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results += [CampgroundOut(**vars(c)) for c in outcome]
    return CampgroundSearchOut(results=results, errors=errors)


@api.get("/campgrounds/{campground_id}")
async def get_campground(
    campground_id: str, request: Request, provider: ProviderKey = "recgov"
) -> CampgroundOut:
    _check_id(campground_id)
    try:
        found = await _get_provider(request, provider).campground(campground_id)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return CampgroundOut(**vars(found))


@api.get("/campgrounds/{campground_id}/sites")
async def list_sites(
    campground_id: str,
    request: Request,
    month: date | None = None,
    provider: ProviderKey = "recgov",
) -> list[SiteOut]:
    _check_id(campground_id)
    try:
        sites = await _get_provider(request, provider).month_availability(
            campground_id, month or date.today()
        )
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    sites.sort(key=lambda s: (_natural(s.loop), _natural(s.site)))
    return [
        SiteOut(campsite_id=s.campsite_id, site=s.site, loop=s.loop, campsite_type=s.campsite_type)
        for s in sites
        if s.type_of_use.lower() != "day"
    ]


def _get_provider(request: Request, key: str) -> Provider:
    try:
        return request.app.state.providers[key]
    except KeyError:
        raise HTTPException(422, "Unknown provider") from None


def _check_id(campground_id: str) -> None:
    if not campground_id.isdigit():
        raise HTTPException(422, "Campground IDs are numbers")


def _natural(text: str) -> list[tuple[int, int | str]]:
    """Sort "2" before "10" and keep mixed names like "A5" comparable."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.findall(r"\d+|\D+", text)]


# --- Status and notifications --------------------------------------------------------------


@api.get("/status")
def app_status(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    checker: Checker = request.app.state.checker
    holder: CartHolder = request.app.state.holder
    last = checker.state.last_cycle
    return {
        "version": __version__,
        "auth_required": settings.auth_required,
        "channels": request.app.state.notifier.channels,
        "cart": {"available": holder.available, "reason": holder.unavailable_reason},
        "poll_interval_seconds": settings.poll_interval_seconds,
        "scheduler_running": settings.run_scheduler,
        "backoff_seconds": checker.state.backoff_seconds,
        "next_cycle_at": checker.state.next_cycle_at,
        "last_cycle": None
        if last is None
        else {
            "started_at": last.started_at,
            "watches_checked": last.watches_checked,
            "requests": last.requests,
            "new_openings": last.new_openings,
            "errors": last.errors,
        },
    }


@api.post("/notifications/test")
async def test_notification(request: Request) -> dict:
    notifier: Notifier = request.app.state.notifier
    if not notifier.channels:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No notification channel is configured")
    results = await notifier.send(
        Notification(
            title="CampWatch test",
            message="Notifications are working. You'll get one like this when a site opens up.",
            urgent=False,
        )
    )
    return {"results": results}
