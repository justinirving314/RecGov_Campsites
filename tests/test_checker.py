import asyncio
import copy
from datetime import date

import pytest

from campwatch.booker import HoldResult
from campwatch.checker import Checker
from campwatch.models import Opening, Watch, make_session_factory
from campwatch.providers import Campsite, ProviderError, RateLimitedError

TODAY = date(2027, 6, 1)
JULY = date(2027, 7, 1)
CAMPGROUND = "100001"


def d(day: int) -> date:
    return date(2027, 7, day)


class FakeProvider:
    def __init__(self, key="recgov", label="Recreation.gov"):
        self.key = key
        self.label = label
        self.months: dict = {}
        self.calls: list = []

    def booking_url(self, target, campsite_id):
        return f"https://{self.key}.example/{target['campground_id']}/{campsite_id}"

    def set_open(self, campground_id: str, month: date, sites: dict[str, list[date]]):
        self.months[(campground_id, month)] = [
            Campsite(
                campsite_id=cid,
                site=f"{int(cid) % 1000:03d}",
                loop="A",
                availability={n: "Available" for n in nights},
            )
            for cid, nights in sites.items()
        ]

    async def month_availability(self, campground_id, month):
        self.calls.append((campground_id, month))
        value = self.months.get((campground_id, month), [])
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, note):
        self.sent.append(note)
        return {}


class FakeHolder:
    def __init__(self, ok=True, available=True):
        self.ok = ok
        self.available = available
        self.calls = []

    async def hold(self, campsite_id, check_in, check_out):
        self.calls.append((campsite_id, check_in, check_out))
        return HoldResult(self.ok, "held" if self.ok else "couldn't find Add to Cart")


@pytest.fixture
def env(tmp_path):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    recgov, notifier, holder = FakeProvider(), FakeNotifier(), FakeHolder()
    providers = {"recgov": recgov, "reservecalifornia": FakeProvider("reservecalifornia", "CA")}
    checker = Checker(
        sessions,
        providers,
        notifier,
        holder,
        poll_interval=60,
        request_delay=0,
        today=lambda: TODAY,
    )
    return sessions, recgov, notifier, holder, checker


def add_watch(sessions, **kw) -> int:
    defaults = dict(
        name="Test watch",
        targets=[{"campground_id": CAMPGROUND, "campground_name": "Pine Hollow", "sites": []}],
        date_ranges=[{"check_in": "2027-07-10", "check_out": "2027-07-12"}],
        min_nights=None,
        action="alert",
        active=True,
    )
    with sessions() as db:
        watch = Watch(**{**defaults, **kw})
        db.add(watch)
        db.commit()
        return watch.id


def openings(sessions, watch_id):
    with sessions() as db:
        return db.query(Opening).filter_by(watch_id=watch_id).order_by(Opening.id).all()


def run(coro):
    return asyncio.run(coro)


def test_new_opening_alerts_once_then_tracks_until_gone(env):
    sessions, recgov, notifier, _, checker = env
    watch_id = add_watch(sessions)
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})

    result = run(checker.check(None))
    assert result.new_openings == 1
    assert len(notifier.sent) == 1
    assert "Site 012" in notifier.sent[0].message
    assert notifier.sent[0].url == "https://recgov.example/100001/5012"

    run(checker.check(None))
    assert len(notifier.sent) == 1, "same opening must not alert again"

    recgov.set_open(CAMPGROUND, JULY, {})
    run(checker.check(None))
    [gone] = openings(sessions, watch_id)
    assert gone.gone_at is not None

    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert len(notifier.sent) == 2, "an opening that comes back alerts again"
    assert [o.gone_at is None for o in openings(sessions, watch_id)] == [False, True]


def test_partial_availability_does_not_alert_for_whole_stay(env):
    sessions, recgov, notifier, _, checker = env
    add_watch(sessions)
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10)]})
    run(checker.check(None))
    assert notifier.sent == []


def test_each_campground_month_fetched_once_per_cycle(env):
    sessions, recgov, _, _, checker = env
    add_watch(sessions)
    add_watch(sessions, date_ranges=[{"check_in": "2027-07-20", "check_out": "2027-07-22"}])
    run(checker.check(None))
    assert recgov.calls == [(CAMPGROUND, JULY)]


def test_multiple_campgrounds_and_ranges(env):
    sessions, recgov, notifier, _, checker = env
    watch_id = add_watch(
        sessions,
        targets=[
            {"campground_id": "100001", "campground_name": "Pine Hollow", "sites": []},
            {"campground_id": "100002", "campground_name": "Lakeside Flats", "sites": ["007"]},
        ],
        date_ranges=[
            {"check_in": "2027-07-10", "check_out": "2027-07-12"},
            {"check_in": "2027-08-14", "check_out": "2027-08-15"},
        ],
    )
    recgov.set_open(
        "100002", date(2027, 8, 1), {"6007": [date(2027, 8, 14)], "6008": [date(2027, 8, 14)]}
    )
    run(checker.check(None))
    assert sorted(recgov.calls) == [
        ("100001", JULY),
        ("100001", date(2027, 8, 1)),
        ("100002", JULY),
        ("100002", date(2027, 8, 1)),
    ]
    [found] = openings(sessions, watch_id)
    assert (found.campground_name, found.site) == ("Lakeside Flats", "007")
    assert len(notifier.sent) == 1


def test_expired_watch_is_stopped_without_fetching(env):
    sessions, recgov, _, _, checker = env
    watch_id = add_watch(
        sessions, date_ranges=[{"check_in": "2027-05-01", "check_out": "2027-05-03"}]
    )
    run(checker.check(None))
    assert recgov.calls == []
    with sessions() as db:
        watch = db.get(Watch, watch_id)
        assert not watch.active
        assert watch.status_note == "All dates have passed."


def test_paused_watches_are_skipped_but_check_now_works(env):
    sessions, recgov, notifier, _, checker = env
    watch_id = add_watch(sessions, active=False)
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert recgov.calls == []
    run(checker.check(watch_id))
    assert len(notifier.sent) == 1


def test_fetch_error_keeps_existing_openings(env):
    sessions, recgov, _, _, checker = env
    watch_id = add_watch(sessions)
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))

    recgov.months[(CAMPGROUND, JULY)] = ProviderError("HTTP 503")
    result = run(checker.check(None))
    assert result.errors
    [opening] = openings(sessions, watch_id)
    assert opening.gone_at is None
    with sessions() as db:
        assert "HTTP 503" in db.get(Watch, watch_id).last_error


def test_rate_limit_backs_off(env):
    sessions, recgov, _, _, checker = env
    add_watch(sessions)
    recgov.months[(CAMPGROUND, JULY)] = RateLimitedError("slow down")
    run(checker.check(None))
    assert checker.state.backoff_seconds == 60
    run(checker.check(None))
    assert checker.state.backoff_seconds == 120
    recgov.months[(CAMPGROUND, JULY)] = []
    run(checker.check(None))
    assert checker.state.backoff_seconds == 0


def test_cart_hold_success_pauses_watch(env):
    sessions, recgov, notifier, holder, checker = env
    watch_id = add_watch(
        sessions,
        action="cart",
        min_nights=1,
        date_ranges=[{"check_in": "2027-07-10", "check_out": "2027-07-15"}],
    )
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10)], "5013": [d(12), d(13), d(14)]})
    run(checker.check(None))

    assert holder.calls == [("5013", d(12), d(15))], "holds the longest stay"
    assert len(notifier.sent) == 2
    assert "In your cart" in notifier.sent[1].title
    with sessions() as db:
        watch = db.get(Watch, watch_id)
        assert not watch.active
        held = [o for o in watch.openings if o.cart_status == "held"]
        assert [o.site for o in held] == ["013"]


def test_cart_hold_failure_alerts_and_keeps_watching(env):
    sessions, recgov, notifier, holder, checker = env
    holder.ok = False
    watch_id = add_watch(sessions, action="cart")
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert "Couldn't hold" in notifier.sent[1].title
    with sessions() as db:
        watch = db.get(Watch, watch_id)
        assert watch.active
        assert watch.openings[0].cart_status == "failed"


def test_cart_action_without_booking_setup_just_alerts(env):
    sessions, recgov, notifier, holder, checker = env
    holder.available = False
    add_watch(sessions, action="cart")
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert holder.calls == []
    assert len(notifier.sent) == 1


def test_range_already_started_only_checks_remaining_nights(env):
    sessions, recgov, notifier, _, checker = env
    checker.today = lambda: d(11)
    add_watch(sessions)  # Jul 10 -> 12; only the night of the 11th is still bookable
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(11)]})
    run(checker.check(None))
    assert len(notifier.sent) == 1


def ca_target(**kw):
    return {
        "provider": "reservecalifornia",
        "campground_id": "766",
        "campground_name": "Redwood Flat",
        "park_id": "718",
        "sites": [],
        **kw,
    }


def test_mixed_providers_fetch_and_record_separately(env):
    sessions, recgov, notifier, _, checker = env
    ca = checker.providers["reservecalifornia"]
    watch_id = add_watch(
        sessions,
        targets=[
            {"campground_id": CAMPGROUND, "campground_name": "Pine Hollow", "sites": []},
            ca_target(),
        ],
    )
    # Same campsite ID in both systems must not be confused.
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    ca.set_open("766", JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))

    assert recgov.calls == [(CAMPGROUND, JULY)]
    assert ca.calls == [("766", JULY)]
    found = sorted((o.provider, o.book_url) for o in openings(sessions, watch_id))
    assert found == [
        ("recgov", "https://recgov.example/100001/5012"),
        ("reservecalifornia", "https://reservecalifornia.example/766/5012"),
    ]
    assert len(notifier.sent) == 1


def test_cart_hold_only_for_recreation_gov(env):
    sessions, _, notifier, holder, checker = env
    add_watch(sessions, action="cart", targets=[ca_target()])
    checker.providers["reservecalifornia"].set_open("766", JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert holder.calls == []
    assert len(notifier.sent) == 1


def test_rate_limit_on_one_system_does_not_block_the_other(env):
    sessions, recgov, notifier, _, checker = env
    ca = checker.providers["reservecalifornia"]
    add_watch(
        sessions,
        targets=[{"campground_id": CAMPGROUND, "campground_name": "Pine Hollow"}, ca_target()],
    )
    recgov.months[(CAMPGROUND, JULY)] = RateLimitedError("slow down")
    ca.set_open("766", JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert checker.state.backoff_seconds == 60
    assert len(notifier.sent) == 1


def test_old_watches_without_provider_default_to_recreation_gov(env):
    sessions, recgov, notifier, _, checker = env
    add_watch(sessions, targets=[{"campground_id": CAMPGROUND, "campground_name": "Old"}])
    recgov.set_open(CAMPGROUND, JULY, {"5012": [d(10), d(11)]})
    run(checker.check(None))
    assert len(notifier.sent) == 1
