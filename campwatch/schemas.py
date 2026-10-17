"""Request and response bodies for the JSON API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

MAX_RANGE_DAYS = 180


def _as_utc(value: datetime) -> datetime:
    """The database stores naive UTC; label it so browsers convert to local time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class Target(BaseModel):
    campground_id: str = Field(pattern=r"^\d{1,12}$")
    campground_name: str = Field("", max_length=200)
    location: str = Field("", max_length=200)
    sites: list[str] = Field(default_factory=list, max_length=300)

    @field_validator("sites")
    @classmethod
    def _clean_sites(cls, sites: list[str]) -> list[str]:
        return list(dict.fromkeys(s.strip()[:50] for s in sites if s.strip()))


class DateRange(BaseModel):
    check_in: date
    check_out: date

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.check_out <= self.check_in:
            raise ValueError("check-out must be after check-in")
        if self.check_out - self.check_in > timedelta(days=MAX_RANGE_DAYS):
            raise ValueError(f"a date range can span at most {MAX_RANGE_DAYS} days")
        return self


class WatchIn(BaseModel):
    name: str = Field("", max_length=100)
    targets: list[Target] = Field(min_length=1, max_length=20)
    date_ranges: list[DateRange] = Field(min_length=1, max_length=30)
    min_nights: int | None = Field(None, ge=1, le=30)
    action: Literal["alert", "cart"] = "alert"
    active: bool = True


class WatchPatch(BaseModel):
    active: bool


class OpeningOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    watch_id: int
    watch_name: str
    campground_id: str
    campground_name: str
    campsite_id: str
    site: str
    loop: str
    check_in: date
    check_out: date
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    gone_at: UtcDatetime | None
    cart_status: str | None
    cart_message: str | None
    book_url: str


class WatchOut(WatchIn):
    model_config = {"from_attributes": True}

    id: int
    status_note: str | None
    created_at: UtcDatetime
    last_checked_at: UtcDatetime | None
    last_error: str | None
    openings: list[OpeningOut] = Field(default_factory=list)


class CampgroundOut(BaseModel):
    id: str
    name: str
    parent_name: str = ""
    location: str = ""


class SiteOut(BaseModel):
    campsite_id: str
    site: str
    loop: str
    campsite_type: str


class LoginIn(BaseModel):
    password: str = Field(max_length=200)
