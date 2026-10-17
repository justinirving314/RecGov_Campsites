"""Database tables (SQLite through SQLAlchemy)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import JSON, ForeignKey, String, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .providers.recgov import campsite_url

DONE_NOTE = "Every night is booked or skipped."
COVERED = ("booked", "held", "skipped")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Watch(Base):
    """One thing to look for: any of ``targets`` for any of ``date_ranges``."""

    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # [{"provider": "recgov", "campground_id": "232447", "campground_name": "Upper Pines",
    #   "location": "...", "park_id": "", "sites": ["012", "013"]}]
    # provider is "recgov" or "reservecalifornia"; park_id is ReserveCalifornia's park.
    targets: Mapped[list[dict]] = mapped_column(JSON)
    # [{"check_in": "2027-07-09", "check_out": "2027-07-11"}]
    date_ranges: Mapped[list[dict]] = mapped_column(JSON)
    # None: the whole range at one site. N: any N+ consecutive nights inside the range.
    min_nights: Mapped[int | None]
    # Night by night: take any open nights, even single ones, and keep looking for the rest.
    piecemeal: Mapped[bool] = mapped_column(default=False)
    # Night-by-night progress: {"2027-07-10": {"status": "booked", "site": "012", ...}}.
    # status is "booked", "held" (in the Recreation.gov cart) or "skipped"; a night with no
    # entry is still needed.
    nights: Mapped[dict | None] = mapped_column(JSON, default=dict)
    action: Mapped[str] = mapped_column(String(10), default="alert")  # "alert" | "cart"
    active: Mapped[bool] = mapped_column(default=True)
    status_note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_checked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(500))

    openings: Mapped[list[Opening]] = relationship(
        back_populates="watch", cascade="all, delete-orphan", order_by="Opening.first_seen_at"
    )

    def ranges(self) -> list[tuple[date, date]]:
        return [
            (date.fromisoformat(r["check_in"]), date.fromisoformat(r["check_out"]))
            for r in self.date_ranges
        ]

    def all_nights(self) -> list[date]:
        nights = {
            check_in + timedelta(days=i)
            for check_in, check_out in self.ranges()
            for i in range((check_out - check_in).days)
        }
        return sorted(nights)

    def needed_nights(self, today: date) -> set[date]:
        """Nights from today on that aren't booked, held or skipped yet."""
        covered = self.nights or {}
        return {
            n
            for n in self.all_nights()
            if n >= today and covered.get(n.isoformat(), {}).get("status") not in COVERED
        }

    def mark_nights(self, nights: Iterable[date], status: str | None, **info) -> None:
        """Record nights as booked, held or skipped, or (status None) as needed again."""
        updated = dict(self.nights or {})  # a new dict, so SQLAlchemy sees the change
        for night in nights:
            if status is None:
                updated.pop(night.isoformat(), None)
            else:
                updated[night.isoformat()] = {"status": status, **info}
        self.nights = updated

    def prune_nights(self) -> None:
        """Forget progress for nights no longer in the date ranges."""
        keep = {n.isoformat() for n in self.all_nights()}
        self.nights = {k: v for k, v in (self.nights or {}).items() if k in keep}

    def update_completion(self, today: date) -> None:
        """Night by night: stop once nothing is needed, and resume if something is again."""
        if not self.piecemeal:
            return
        if not self.needed_nights(today):
            self.active = False
            self.status_note = DONE_NOTE
        elif not self.active and self.status_note == DONE_NOTE:
            self.active = True
            self.status_note = None

    @property
    def night_plan(self) -> list[dict]:
        covered = self.nights or {}
        return [
            {"night": n, **covered.get(n.isoformat(), {"status": "needed"})}
            for n in self.all_nights()
        ]


class Opening(Base):
    """A stay that showed up as bookable. ``gone_at`` is set once it stops showing up."""

    __tablename__ = "openings"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20), default="recgov")
    campground_id: Mapped[str] = mapped_column(String(20))
    campground_name: Mapped[str] = mapped_column(String(200))
    campsite_id: Mapped[str] = mapped_column(String(20))
    site: Mapped[str] = mapped_column(String(50))
    loop: Mapped[str] = mapped_column(String(100), default="")
    check_in: Mapped[date]
    check_out: Mapped[date]
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    gone_at: Mapped[datetime | None]
    # "held" or "failed" (cart hold), or "booked" when you mark it booked.
    cart_status: Mapped[str | None] = mapped_column(String(20))
    cart_message: Mapped[str | None] = mapped_column(String(500))
    booking_url: Mapped[str | None] = mapped_column(String(300))

    watch: Mapped[Watch] = relationship(back_populates="openings")

    @property
    def key(self) -> tuple[str, str, date, date]:
        return (self.provider, self.campsite_id, self.check_in, self.check_out)

    @property
    def book_url(self) -> str:
        return self.booking_url or campsite_url(self.campsite_id)

    @property
    def watch_name(self) -> str:
        return self.watch.name


# Columns added after the first release: (table, column, DDL). Applied on startup if missing.
_ADDED_COLUMNS = [
    ("openings", "provider", "VARCHAR(20) NOT NULL DEFAULT 'recgov'"),
    ("openings", "booking_url", "VARCHAR(300)"),
    ("watches", "piecemeal", "BOOLEAN NOT NULL DEFAULT 0"),
    ("watches", "nights", "JSON"),
]


def _upgrade(engine: Engine) -> None:
    existing = {
        table: {c["name"] for c in inspect(engine).get_columns(table)}
        for table in {t for t, _, _ in _ADDED_COLUMNS}
    }
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            if column not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def make_session_factory(db_url: str) -> sessionmaker:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    _upgrade(engine)
    return sessionmaker(engine, expire_on_commit=False)
