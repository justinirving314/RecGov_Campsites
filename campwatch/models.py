"""Database tables (SQLite through SQLAlchemy)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import JSON, ForeignKey, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .recgov import campsite_url


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Watch(Base):
    """One thing to look for: any of ``targets`` for any of ``date_ranges``."""

    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # [{"campground_id": "232447", "campground_name": "Upper Pines", "location": "...",
    #   "sites": ["012", "013"]}]
    targets: Mapped[list[dict]] = mapped_column(JSON)
    # [{"check_in": "2027-07-09", "check_out": "2027-07-11"}]
    date_ranges: Mapped[list[dict]] = mapped_column(JSON)
    # None: the whole range at one site. N: any N+ consecutive nights inside the range.
    min_nights: Mapped[int | None]
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


class Opening(Base):
    """A stay that showed up as bookable. ``gone_at`` is set once it stops showing up."""

    __tablename__ = "openings"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
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
    cart_status: Mapped[str | None] = mapped_column(String(20))  # "held" | "failed"
    cart_message: Mapped[str | None] = mapped_column(String(500))

    watch: Mapped[Watch] = relationship(back_populates="openings")

    @property
    def key(self) -> tuple[str, date, date]:
        return (self.campsite_id, self.check_in, self.check_out)

    @property
    def book_url(self) -> str:
        return campsite_url(self.campsite_id)

    @property
    def watch_name(self) -> str:
        return self.watch.name


def make_session_factory(db_url: str) -> sessionmaker:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
