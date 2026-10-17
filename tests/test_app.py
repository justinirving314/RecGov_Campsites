import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from campwatch.app import create_app
from campwatch.config import Settings
from tests.helpers import CA_FACILITIES, CA_PLACES, grid_payload, month_payload

TODAY = date(2027, 6, 1)

WATCH = {
    "targets": [{"campground_id": "100001", "campground_name": "Pine Hollow", "sites": []}],
    "date_ranges": [{"check_in": "2027-07-10", "check_out": "2027-07-12"}],
}


class FakeInternet:
    """Answers Recreation.gov and ntfy requests with synthetic data and records them."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.open_nights = {date(2027, 7, 10), date(2027, 7, 11)}
        self.ca_down = False

    def ca_grid(self, body: dict) -> dict:
        start, end = _mdy(body["StartDate"]), _mdy(body["EndDate"])
        units = {"1": ("Site 012", self.open_nights), "2": ("Site 013", set())}
        return grid_payload(start, end, units)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "ntfy.example":
            return httpx.Response(200, json={"id": "x"})
        if request.url.host.endswith("tylerapp.com"):  # ReserveCalifornia
            if self.ca_down:
                return httpx.Response(503)
            if path == "/rdr/fd/places":
                return httpx.Response(200, json=CA_PLACES)
            if path == "/rdr/fd/facilities":
                return httpx.Response(200, json=CA_FACILITIES)
            if path == "/rdr/search/grid":
                return httpx.Response(200, json=self.ca_grid(json.loads(request.content)))
            return httpx.Response(404)
        if path.endswith("/month"):
            month = date.fromisoformat(request.url.params["start_date"][:10])
            return httpx.Response(
                200,
                json=month_payload(
                    month,
                    {
                        "5010": ("10", self.open_nights),
                        "5002": ("2", set()),
                        "5099": ("A5", set()),
                        "5100": ("PICNIC", set()),
                    },
                    **{"5100": {"type_of_use": "Day"}},
                ),
            )
        if path == "/api/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"entity_id": "100001", "entity_type": "campground", "name": "PINE HOLLOW"}
                    ]
                },
            )
        if path.startswith("/api/camps/campgrounds/"):
            return httpx.Response(200, json={"campground": {"facility_name": "PINE HOLLOW"}})
        return httpx.Response(404)


def _mdy(text: str) -> date:
    month, day, year = map(int, text.split("-"))
    return date(year, month, day)


def make_client(tmp_path, internet=None, **settings) -> TestClient:
    config = Settings(_env_file=None, data_dir=tmp_path, run_scheduler=False, **settings)
    app = create_app(
        config, transport=httpx.MockTransport(internet or FakeInternet()), today=lambda: TODAY
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def test_serves_web_app(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "CampWatch" in resp.text
    assert client.get("/manifest.webmanifest").status_code == 200


def test_watch_crud(client):
    created = client.post("/api/watches", json=WATCH)
    assert created.status_code == 201
    watch = created.json()
    assert watch["name"] == "Pine Hollow, Jul 10"
    assert watch["active"] is True

    assert [w["id"] for w in client.get("/api/watches").json()] == [watch["id"]]

    updated = client.put(
        f"/api/watches/{watch['id']}", json={**WATCH, "name": "Summer", "min_nights": 1}
    )
    assert updated.json()["name"] == "Summer"
    assert updated.json()["min_nights"] == 1

    paused = client.patch(f"/api/watches/{watch['id']}", json={"active": False})
    assert paused.json()["active"] is False

    assert client.delete(f"/api/watches/{watch['id']}").status_code == 204
    assert client.get(f"/api/watches/{watch['id']}").status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {**WATCH, "date_ranges": [{"check_in": "2027-07-12", "check_out": "2027-07-12"}]},
        {**WATCH, "date_ranges": []},
        {**WATCH, "targets": []},
        {**WATCH, "targets": [{"campground_id": "../etc"}]},
        {**WATCH, "action": "buy"},
    ],
)
def test_watch_validation(client, bad):
    assert client.post("/api/watches", json=bad).status_code == 422


def test_check_now_finds_opening_and_sends_ntfy(tmp_path):
    internet = FakeInternet()
    with make_client(
        tmp_path, internet, ntfy_topic="test-topic", ntfy_server="https://ntfy.example"
    ) as c:
        watch = c.post("/api/watches", json=WATCH).json()
        checked = c.post(f"/api/watches/{watch['id']}/check").json()

    [opening] = checked["openings"]
    assert opening["site"] == "10"
    assert opening["book_url"] == "https://www.recreation.gov/camping/campsites/5010"
    assert opening["first_seen_at"].endswith("Z") or "+00:00" in opening["first_seen_at"]

    [push] = [r for r in internet.requests if r.url.host == "ntfy.example"]
    body = json.loads(push.content)
    assert body["topic"] == "test-topic"
    assert body["click"] == opening["book_url"]


def test_editing_criteria_resets_open_openings(client):
    watch = client.post("/api/watches", json=WATCH).json()
    assert client.post(f"/api/watches/{watch['id']}/check").json()["openings"]

    renamed = client.put(f"/api/watches/{watch['id']}", json={**WATCH, "name": "New name"}).json()
    assert renamed["openings"], "renaming keeps openings"

    changed = client.put(
        f"/api/watches/{watch['id']}",
        json={**WATCH, "date_ranges": [{"check_in": "2027-07-20", "check_out": "2027-07-22"}]},
    ).json()
    assert changed["openings"] == []


def test_openings_feed(client):
    watch = client.post("/api/watches", json={**WATCH, "name": "Feed"}).json()
    client.post(f"/api/watches/{watch['id']}/check")
    [opening] = client.get("/api/openings").json()
    assert opening["watch_name"] == "Feed"
    assert opening["gone_at"] is None


def search(client, q):
    resp = client.get("/api/campgrounds/search", params={"q": q})
    assert resp.status_code == 200
    return resp.json()


def test_campground_search_and_link_paste(client):
    by_name = search(client, "pine")
    assert by_name["errors"] == []
    assert by_name["results"] == [
        {
            "provider": "recgov",
            "id": "100001",
            "name": "Pine Hollow",
            "parent_name": "",
            "location": "",
            "park_id": "",
        }
    ]

    url = "https://www.recreation.gov/camping/campgrounds/100001"
    [by_link] = search(client, url)["results"]
    assert by_link["id"] == "100001"


def test_search_covers_california_state_parks(client):
    found = search(client, "https://www.reservecalifornia.com/park/718/766")["results"]
    assert [(c["provider"], c["id"], c["park_id"]) for c in found] == [
        ("reservecalifornia", "766", "718")
    ]
    by_id = search(client, "801")["results"]
    assert {c["provider"] for c in by_id} == {"recgov", "reservecalifornia"}


def test_search_reports_a_failing_system_but_keeps_the_others(tmp_path):
    internet = FakeInternet()
    internet.ca_down = True
    with make_client(tmp_path, internet) as c:
        result = search(c, "pine")
    assert [r["provider"] for r in result["results"]] == ["recgov"]
    assert result["errors"] and result["errors"][0].startswith("CA State Parks")


def test_california_watch_alerts_with_park_booking_link(client):
    watch = client.post(
        "/api/watches",
        json={
            **WATCH,
            "targets": [
                {
                    "provider": "reservecalifornia",
                    "campground_id": "766",
                    "campground_name": "Redwood Flat Campground",
                    "park_id": "718",
                }
            ],
        },
    ).json()
    [opening] = client.post(f"/api/watches/{watch['id']}/check").json()["openings"]
    assert opening["provider"] == "reservecalifornia"
    assert opening["site"] == "012"
    assert opening["book_url"] == "https://www.reservecalifornia.com/park/718/766"


def test_california_sites_list(client):
    sites = client.get(
        "/api/campgrounds/766/sites",
        params={"month": "2027-07-01", "provider": "reservecalifornia"},
    ).json()
    assert [s["site"] for s in sites] == ["012", "013"]
    bad = client.get("/api/campgrounds/766/sites", params={"provider": "nope"})
    assert bad.status_code == 422


def test_sites_are_sorted_naturally_without_day_use(client):
    sites = client.get("/api/campgrounds/100001/sites", params={"month": "2027-07-01"}).json()
    assert [s["site"] for s in sites] == ["2", "10", "A5"]


def test_status_and_test_notification_without_channels(client):
    status = client.get("/api/status").json()
    assert status["channels"] == []
    assert status["cart"]["available"] is False
    assert client.post("/api/notifications/test").status_code == 400


def test_password_protects_api(tmp_path):
    with make_client(tmp_path, app_password="correct horse") as c:
        assert c.get("/api/session").json() == {"auth_required": True, "logged_in": False}
        assert c.get("/api/watches").status_code == 401
        assert c.get("/").status_code == 200, "the app shell loads so it can show the login form"

        assert c.post("/api/login", json={"password": "nope"}).status_code == 401
        assert c.get("/api/watches").status_code == 401

        assert c.post("/api/login", json={"password": "correct horse"}).status_code == 204
        assert c.get("/api/watches").status_code == 200
        assert c.get("/api/session").json()["logged_in"] is True

        c.post("/api/logout")
        assert c.get("/api/watches").status_code == 401


def test_forged_cookie_rejected(tmp_path):
    with make_client(tmp_path, app_password="pw") as c:
        c.cookies.set("campwatch_session", "99999999999.deadbeef")
        assert c.get("/api/watches").status_code == 401
