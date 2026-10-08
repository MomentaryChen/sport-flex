from __future__ import annotations

from playwright.sync_api import Page

from sportflex.core.models import Availability, Court, Slot, slot_matches
from sportflex.core.venue import Venue


class ProviderError(RuntimeError):
    pass


class NeedLogin(ProviderError):
    pass


class Throttled(ProviderError):
    """The site asked us to slow down; the engine backs off by rules.throttle_backoff_sec."""


class Provider:
    """What the engine needs from a booking platform. One instance drives one logged-in browser page.

    Snapshots returned by submit/press are dicts with url, text, buttons, image (JPEG bytes) and pressed.
    submit must stop before any payment step: the person pays by hand.
    """

    name: str
    login_url: str

    def __init__(self, page: Page) -> None:
        self.page = page

    def refresh_session(self) -> dict:
        """Reload the home page and report {"loggedIn": bool, "banner": str}."""
        raise NotImplementedError

    def captcha(self, username: str, password: str) -> bytes:
        """Fill the login form and return the captcha image."""
        raise NotImplementedError

    def login(self, code: str) -> dict:
        """Submit the login form with the captcha code. Returns {"ok": bool, "message": str}."""
        raise NotImplementedError

    def list_courts(self, venue: Venue, category: str) -> list[Court]:
        raise NotImplementedError

    def availability(self, court: Court, query_date: str = "") -> Availability:
        raise NotImplementedError

    def prepare(self, court: Court) -> None:
        """Park on the court's reserve page so the first submit after release is fast."""
        raise NotImplementedError

    def submit(self, court: Court, query_date: str, slot: Slot) -> dict:
        raise NotImplementedError

    def press(self, label: str) -> dict:
        """Press a button the person picked from the last snapshot."""
        raise NotImplementedError

    def search(self, venue: Venue, category: str, query_date: str, start: str, end: str) -> list[str]:
        """Ids of courts with a bookable slot starting in [start, end) on query_date.

        This default asks every court in turn; platforms with a time-filter page should override it
        with a single request.
        """
        found = []
        for court in self.list_courts(venue, category):
            availability = self.availability(court, query_date)
            if availability.query_date != query_date:
                continue
            if any(slot.bookable and slot_matches(slot, start, end) for slot in availability.slots):
                found.append(court.id)
        return found

    def find_court(self, venue: Venue, category: str, court_id: str) -> Court:
        for court in self.list_courts(venue, category):
            if court.id == court_id:
                return court
        raise ProviderError("找不到這面球場，請先重新整理")
