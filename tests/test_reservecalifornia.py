import asyncio
import json
from datetime import date

import httpx
import pytest

from campwatch.providers import ProviderError, RateLimitedError
from campwatch.providers.reservecalifornia import (
    ReserveCaliforniaClient,
    parse_grid,
    parse_link,
)
from tests.helpers import CA_FACILITIES, CA_PLACES, grid_payload

JULY = date(2027, 7, 1)


def d(day: int, month: int = 7) -> date:
    return date(2027, month, day)


def run(coro):
    return asyncio.run(coro)


def client_for(handler, today=date(2027, 6, 1)) -> ReserveCaliforniaClient:
    http = ReserveCaliforniaClient.create_http(transport=httpx.MockTransport(handler))
    return ReserveCaliforniaClient(http, today=lambda: today)


class FakeTyler:
    """Answers the metadata and grid endpoints and counts requests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/rdr/fd/places":
            return httpx.Response(200, json=CA_PLACES)
        if request.url.path == "/rdr/fd/facilities":
            return httpx.Response(200, json=CA_FACILITIES)
        if request.url.path == "/rdr/search/grid":
            return httpx.Response(
                200, json=grid_payload(d(1), d(1, 8), {"1": ("Site 012", {d(4)})})
            )
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def test_parse_grid_statuses_and_names():
    payload = grid_payload(
        d(1),
        d(1, 8),
        {"1": ("Site 012", {d(4), d(5), d(6), d(7)}), "2": ("Campsite #3", set())},
        **{
            "1": {
                "slices": {
                    d(5): {"IsWalkin": True},
                    d(6): {"IsBlocked": True},
                    d(7): {"IsReservationDraw": True},
                    d(4): {"MinStay": 2},
                }
            }
        },
    )
    sites = {s.campsite_id: s for s in parse_grid(payload, JULY)}
    assert (sites["1"].site, sites["2"].site) == ("012", "3")
    a = sites["1"].availability
    assert (a[d(4)], a[d(5)], a[d(6)], a[d(7)], a[d(8)]) == (
        "Available",
        "Walk-up",
        "Not Available",
        "Lottery",
        "Reserved",
    )
    assert d(1, 8) not in a, "nights outside the month are dropped"
    assert sites["1"].min_stay[d(4)] == 2


def test_parse_grid_skips_units_not_bookable_online():
    payload = grid_payload(
        d(1), d(2), {"1": ("1", {d(1)}), "2": ("2", {d(1)})}, **{"2": {"AllowWebBooking": False}}
    )
    assert [s.campsite_id for s in parse_grid(payload, JULY)] == ["1"]


def test_parse_grid_reports_api_message():
    with pytest.raises(ProviderError, match="Facility not found"):
        parse_grid({"Message": "Facility not found", "Facility": None}, JULY)


def test_month_availability_request():
    fake = FakeTyler()
    sites = run(client_for(fake).month_availability("766", JULY))
    assert sites[0].availability[d(4)] == "Available"
    [request] = fake.requests
    assert request.method == "POST"
    body = json.loads(request.content)
    assert body["FacilityId"] == 766
    assert (body["StartDate"], body["EndDate"]) == ("07-01-2027", "08-01-2027")


def test_month_availability_starts_today_and_rolls_over_year():
    fake = FakeTyler()
    run(client_for(fake, today=date(2027, 12, 20)).month_availability("766", date(2027, 12, 1)))
    body = json.loads(fake.requests[0].content)
    assert (body["StartDate"], body["EndDate"]) == ("12-20-2027", "01-01-2028")


def test_past_month_makes_no_request():
    fake = FakeTyler()
    assert run(client_for(fake, today=d(10, 9)).month_availability("766", JULY)) == []
    assert fake.requests == []


def test_rate_limit():
    client = client_for(lambda r: httpx.Response(429))
    with pytest.raises(RateLimitedError):
        run(client.month_availability("766", JULY))


def test_search_by_park_or_campground_name():
    fake = FakeTyler()
    client = client_for(fake)
    by_park = run(client.search_campgrounds("redwoods"))
    assert [c.id for c in by_park] == ["767", "766"], "only web-bookable, park matches"
    assert by_park[1].parent_name == "Example Redwoods SP"
    assert by_park[1].location == "Somewhere, CA"
    assert by_park[1].park_id == "718"
    assert by_park[1].provider == "reservecalifornia"

    assert [c.id for c in run(client.search_campgrounds("beach camp"))] == ["801"]
    assert fake.paths().count("/rdr/fd/facilities") == 1, "metadata is cached"


def test_search_by_link_or_id():
    client = client_for(FakeTyler())
    park = run(client.search_campgrounds("https://www.reservecalifornia.com/park/718"))
    assert sorted(c.id for c in park) == ["766", "767"]
    facility = run(client.search_campgrounds("https://www.reservecalifornia.com/park/718/766"))
    assert [c.id for c in facility] == ["766"]
    old_style = "https://www.reservecalifornia.com/Web/Default.aspx#!park/719/801"
    assert [c.id for c in run(client.search_campgrounds(old_style))] == ["801"]
    assert [c.id for c in run(client.search_campgrounds("801"))] == ["801"]
    assert run(client.search_campgrounds("999999")) == []


def test_search_ignores_other_links_without_requests():
    fake = FakeTyler()
    url = "https://www.recreation.gov/camping/campgrounds/232447"
    assert run(client_for(fake).search_campgrounds(url)) == []
    assert fake.requests == []


def test_campground_lookup_and_booking_url():
    client = client_for(FakeTyler())
    cg = run(client.campground("766"))
    assert (cg.name, cg.park_id) == ("Redwood Flat Campground", "718")
    with pytest.raises(ProviderError):
        run(client.campground("768"))  # not bookable online
    target = {"campground_id": "766", "park_id": "718"}
    assert client.booking_url(target, "1") == "https://www.reservecalifornia.com/park/718/766"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://www.reservecalifornia.com/park/718/766", ("718", "766")),
        ("reservecalifornia.com/park/718", ("718", None)),
        ("https://www.reservecalifornia.com/Web/Default.aspx#!park/719/801", ("719", "801")),
        ("https://www.recreation.gov/camping/campgrounds/1", None),
    ],
)
def test_parse_link(text, expected):
    assert parse_link(text) == expected
