import asyncio
from datetime import date

import httpx
import pytest

from campwatch.recgov import (
    RateLimitedError,
    RecGovClient,
    RecGovError,
    parse_campground_id,
    parse_month,
)
from tests.helpers import month_payload


def client_for(handler) -> RecGovClient:
    return RecGovClient(RecGovClient.create_http(transport=httpx.MockTransport(handler)))


def run(coro):
    return asyncio.run(coro)


def test_parse_month_reads_sites_and_nights():
    payload = month_payload(date(2027, 7, 1), {"9001": ("012", {date(2027, 7, 4)})})
    [site] = parse_month(payload)
    assert (site.campsite_id, site.site, site.loop) == ("9001", "012", "Loop A")
    assert site.availability[date(2027, 7, 4)] == "Available"
    assert site.availability[date(2027, 7, 5)] == "Reserved"
    assert len(site.availability) == 31


def test_parse_month_rejects_unexpected_shape():
    with pytest.raises(RecGovError):
        parse_month({"error": "nope"})


def test_month_availability_always_asks_for_first_of_month():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=month_payload(date(2027, 7, 1), {}))

    run(client_for(handler).month_availability("100001", date(2027, 7, 19)))
    assert seen[0].url.path == "/api/camps/availability/campground/100001/month"
    assert seen[0].url.params["start_date"] == "2027-07-01T00:00:00.000Z"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429), RateLimitedError),
        (httpx.Response(500), RecGovError),
        (httpx.Response(200, text="<html>maintenance</html>"), RecGovError),
    ],
)
def test_errors_are_raised_as_recgov_errors(response, error):
    client = client_for(lambda request: response)
    with pytest.raises(error):
        run(client.month_availability("100001", date(2027, 7, 1)))


def test_network_failure_is_a_recgov_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(RecGovError):
        run(client_for(handler).month_availability("100001", date(2027, 7, 1)))


def test_search_campgrounds_parses_and_filters():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fq"] == "entity_type:campground"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "entity_id": "100001",
                        "entity_type": "campground",
                        "name": "PINE HOLLOW",
                        "parent_name": "Example National Forest",
                        "city": "SOMEWHERE",
                        "state_code": "CA",
                    },
                    {"entity_id": "5", "entity_type": "permit", "name": "A Permit"},
                    {"entity_type": "campground", "name": "No ID"},
                ]
            },
        )

    [result] = run(client_for(handler).search_campgrounds("pine"))
    assert result.id == "100001"
    assert result.name == "Pine Hollow"
    assert result.location == "Somewhere, CA"


def test_campground_details():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "campground": {
                    "facility_name": "LAKESIDE FLATS",
                    "parent_name": "Example Recreation Area",
                    "addresses": [{"city": "Lakeville", "state_code": "OR"}],
                }
            },
        )

    cg = run(client_for(handler).campground("100002"))
    assert (cg.name, cg.parent_name, cg.location) == (
        "Lakeside Flats",
        "Example Recreation Area",
        "Lakeville, OR",
    )


def test_campground_missing_is_error():
    with pytest.raises(RecGovError):
        run(client_for(lambda r: httpx.Response(200, json={"campground": {}})).campground("1"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("232447", "232447"),
        (" https://www.recreation.gov/camping/campgrounds/232447 ", "232447"),
        ("https://www.recreation.gov/camping/campgrounds/232447/availability", "232447"),
        ("upper pines", None),
    ],
)
def test_parse_campground_id(text, expected):
    assert parse_campground_id(text) == expected
