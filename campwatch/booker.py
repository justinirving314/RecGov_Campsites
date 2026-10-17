"""Optional: put an open site in your Recreation.gov cart so it's held while you pay.

Recreation.gov holds a cart for about 15 minutes. This logs in with your account in a headless
browser, picks the dates on the campsite page and clicks "Add to Cart". Checkout and payment
are left to you: open Recreation.gov on your phone, sign in, and finish from the cart.

The website changes often and may show a CAPTCHA, so this is best effort. The alert is always
sent before a hold is attempted, and a failure is reported with a screenshot in data/screenshots.

Requires the ``booking`` extra: ``uv sync --extra booking && uv run playwright install chromium``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import Settings
from .providers.recgov import BASE_URL, campsite_url

log = logging.getLogger(__name__)

TIMEOUT_MS = 20_000

# Several candidates per control, tried in order, because the site's markup shifts over time.
LOGIN_BUTTON = [
    "button:has-text('Log In')",
    "button:has-text('Sign Up or Log In')",
    "a:has-text('Log In')",
]
EMAIL_INPUT = ["#email", "input[type='email']"]
PASSWORD_INPUT = ["#rec-acct-sign-in-password", "input[type='password']"]
SUBMIT_LOGIN = ["form button[type='submit']", "button.rec-acct-sign-in-btn"]
START_DATE_INPUT = [
    "input[aria-label*='check in' i]",
    "input[id*='start-date']",
    "input[id*='startDate']",
    "[data-component='DateRangeInput'] input >> nth=0",
]
END_DATE_INPUT = [
    "input[aria-label*='check out' i]",
    "input[id*='end-date']",
    "input[id*='endDate']",
    "[data-component='DateRangeInput'] input >> nth=1",
]
ADD_TO_CART = ["button:has-text('Add to Cart')", "button:has-text('Book Now')"]
CART_URL = re.compile(r"/(cart|reservations|orderdetails)", re.I)


@dataclass
class HoldResult:
    ok: bool
    message: str
    screenshot: Path | None = None


class BookingUnavailableError(RuntimeError):
    pass


class CartHolder:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def available(self) -> bool:
        if not self.settings.booking_configured:
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def unavailable_reason(self) -> str | None:
        if not self.settings.booking_configured:
            return "Set CAMPWATCH_RECGOV_EMAIL and CAMPWATCH_RECGOV_PASSWORD."
        if not self.available:
            return "Install the booking extra: uv sync --extra booking"
        return None

    async def hold(self, campsite_id: str, check_in: date, check_out: date) -> HoldResult:
        if not self.available:
            return HoldResult(False, self.unavailable_reason or "booking unavailable")

        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        s = self.settings
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=s.booking_headless)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(TIMEOUT_MS)
            try:
                await page.goto(BASE_URL)
                await _click_first(page, LOGIN_BUTTON)
                await _fill_first(page, EMAIL_INPUT, s.recgov_email)
                await _fill_first(page, PASSWORD_INPUT, s.recgov_password.get_secret_value())
                await _click_first(page, SUBMIT_LOGIN)
                try:
                    await page.wait_for_selector("input[type='password']", state="detached")
                except PlaywrightError as exc:
                    raise BookingUnavailableError(
                        "Couldn't log in to Recreation.gov (wrong password, or a CAPTCHA appeared)"
                    ) from exc

                await page.goto(campsite_url(campsite_id))
                await _fill_first(page, START_DATE_INPUT, check_in.strftime("%m/%d/%Y"))
                await _fill_first(page, END_DATE_INPUT, check_out.strftime("%m/%d/%Y"))
                await page.keyboard.press("Escape")
                await _click_first(page, ADD_TO_CART)
                await page.wait_for_url(CART_URL)
                return HoldResult(True, "Held in your Recreation.gov cart. Finish checkout soon.")
            except (PlaywrightError, BookingUnavailableError) as exc:
                shot = await _screenshot(page, s.data_dir, campsite_id)
                log.warning("cart hold for campsite %s failed: %s", campsite_id, exc)
                return HoldResult(False, _short(exc), shot)
            finally:
                await browser.close()


async def _first_visible(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=4_000)
            return locator
        except Exception:
            continue
    raise BookingUnavailableError(f"couldn't find any of: {', '.join(selectors)}")


async def _click_first(page, selectors: list[str]) -> None:
    await (await _first_visible(page, selectors)).click()


async def _fill_first(page, selectors: list[str], value: str) -> None:
    locator = await _first_visible(page, selectors)
    await locator.click()
    await locator.fill("")
    await locator.press_sequentially(value, delay=30)


async def _screenshot(page, data_dir: Path, campsite_id: str) -> Path | None:
    folder = data_dir / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{campsite_id}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        return None


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
