import sqlite3
from datetime import date

from campwatch.models import Opening, make_session_factory


def test_upgrades_a_database_from_the_first_release(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # schema as shipped in 2.0.0
        conn.executescript(
            """
            CREATE TABLE watches (
                id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, targets JSON NOT NULL,
                date_ranges JSON NOT NULL, min_nights INTEGER, action VARCHAR(10) NOT NULL,
                active BOOLEAN NOT NULL, status_note VARCHAR(200), created_at DATETIME NOT NULL,
                last_checked_at DATETIME, last_error VARCHAR(500)
            );
            CREATE TABLE openings (
                id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL REFERENCES watches(id),
                campground_id VARCHAR(20) NOT NULL, campground_name VARCHAR(200) NOT NULL,
                campsite_id VARCHAR(20) NOT NULL, site VARCHAR(50) NOT NULL,
                loop VARCHAR(100) NOT NULL, check_in DATE NOT NULL, check_out DATE NOT NULL,
                first_seen_at DATETIME NOT NULL, last_seen_at DATETIME NOT NULL,
                gone_at DATETIME, cart_status VARCHAR(20), cart_message VARCHAR(500)
            );
            INSERT INTO watches VALUES (1, 'Old', '[]', '[]', NULL, 'alert', 1, NULL,
                '2027-01-01 00:00:00', NULL, NULL);
            INSERT INTO openings VALUES (1, 1, '100001', 'Pine Hollow', '5012', '012', 'A',
                '2027-07-10', '2027-07-12', '2027-01-01 00:00:00', '2027-01-01 00:00:00',
                NULL, NULL, NULL);
            """
        )

    sessions = make_session_factory(f"sqlite:///{path}")
    make_session_factory(f"sqlite:///{path}")  # running the upgrade twice is harmless
    with sessions() as db:
        opening = db.get(Opening, 1)
        assert opening.provider == "recgov"
        assert opening.book_url == "https://www.recreation.gov/camping/campsites/5012"
        assert opening.check_in == date(2027, 7, 10)
