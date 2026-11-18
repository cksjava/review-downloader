"""HTTP session that looks like Chrome on a phone opening Play Store pages.

The first request loads the public app listing and keeps the cookies that
response sets. Later requests are the same batchexecute calls the page makes
when someone opens permissions or asks for the next review page. Each review
request carries the continuation token from the previous page.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from typing import Any, Callable

from playreviews.constants import MOBILE_USER_AGENT
from playreviews.parse import (
    ParseError,
    app_node,
    parse_dataset,
    parse_listing,
    parse_permissions,
    parse_review_response,
    parse_rpc,
)

Log = Callable[[str], None]
Sleep = Callable[[float], None]

RETRYABLE = {429, 500, 502, 503, 504}


class AppNotFound(Exception):
    def __init__(self, app_id: str):
        super().__init__(f"No Play Store listing found for {app_id}.")
        self.app_id = app_id


class StoreError(Exception):
    """The store responded, but not with a page we can continue from."""


def details_url(app_id: str, lang: str, country: str) -> str:
    query = urllib.parse.urlencode({"id": app_id, "hl": lang, "gl": country})
    return f"https://play.google.com/store/apps/details?{query}"


def _accept_language(lang: str, country: str) -> str:
    primary = f"{lang}-{country.upper()}"
    return f"{primary},{lang};q=0.9,en;q=0.8"


def reviews_form(
    app_id: str,
    sort: int,
    count: int,
    token: str | None,
    score: int | None,
    device: int | None,
) -> bytes:
    pagination: list[Any] = [count] if token is None else [count, None, token]
    filters = [None, score, None, None, None, None, None, None, device]
    inner = [None, [2, sort, pagination, None, filters], [app_id, 7]]
    freq = json.dumps(
        [[["oCPfdb", json.dumps(inner, separators=(",", ":")), None, "generic"]]],
        separators=(",", ":"),
    )
    return urllib.parse.urlencode({"f.req": freq}).encode()


def permissions_form(app_id: str) -> bytes:
    inner = [[None, [app_id, 7], []]]
    freq = json.dumps(
        [[["xdSrCf", json.dumps(inner, separators=(",", ":")), None, "generic"]]],
        separators=(",", ":"),
    )
    return urllib.parse.urlencode({"f.req": freq}).encode()


class PlayClient:
    def __init__(
        self,
        lang: str,
        country: str,
        user_agent: str = MOBILE_USER_AGENT,
        log: Log | None = None,
        sleep: Sleep = time.sleep,
        retries: int = 6,
        timeout: float = 45,
    ):
        self.lang = lang
        self.country = country
        self.user_agent = user_agent
        self.log = log
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self.referer = "https://play.google.com/"
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _request(self, request: urllib.request.Request) -> str:
        delay = 5.0
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise AppNotFound("that page") from exc
                last_error = exc
                if exc.code not in RETRYABLE or attempt + 1 >= self.retries:
                    raise StoreError(f"Play Store returned HTTP {exc.code}.") from exc
                wait = delay
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after and retry_after.isdigit():
                    wait = min(int(retry_after), 180)
                self._note(f"retry    HTTP {exc.code}, waiting {wait:.0f}s")
                self.sleep(wait)
                delay = min(delay * 2, 80)
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt + 1 >= self.retries:
                    raise StoreError(f"Could not reach Play Store: {exc.reason}") from exc
                self._note(f"retry    connection failed, waiting {delay:.0f}s")
                self.sleep(delay)
                delay = min(delay * 2, 80)
            except TimeoutError as exc:
                last_error = exc
                if attempt + 1 >= self.retries:
                    raise StoreError("Play Store timed out.") from exc
                self._note(f"retry    timed out, waiting {delay:.0f}s")
                self.sleep(delay)
                delay = min(delay * 2, 80)
        raise StoreError("Play Store did not respond.") from last_error

    def _note(self, message: str) -> None:
        if self.log:
            self.log(message)

    def _document_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": _accept_language(self.lang, self.country),
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Sec-CH-UA-Mobile": "?1",
            "Sec-CH-UA-Platform": '"Android"',
        }

    def _rpc_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": _accept_language(self.lang, self.country),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://play.google.com",
            "Referer": self.referer,
            "X-Same-Domain": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA-Mobile": "?1",
            "Sec-CH-UA-Platform": '"Android"',
        }

    def _rpc_url(self) -> str:
        query = urllib.parse.urlencode({"hl": self.lang, "gl": self.country})
        return f"https://play.google.com/_/PlayStoreUi/data/batchexecute?{query}"

    def fetch_app(self, app_id: str, fetched_at: str) -> dict[str, Any]:
        url = details_url(app_id, self.lang, self.country)
        self.referer = url
        request = urllib.request.Request(url, headers=self._document_headers())
        try:
            html = self._request(request)
        except AppNotFound as exc:
            raise AppNotFound(app_id) from exc
        dataset = parse_dataset(html)
        node = app_node(dataset)
        if node is None:
            raise StoreError(
                "Could not read the app listing. Check the package id, or try again "
                "if Google returned a consent or error page."
            )
        return parse_listing(node, app_id, url, self.lang, self.country, fetched_at)

    def fetch_permissions(self, app_id: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            self._rpc_url(),
            data=permissions_form(app_id),
            headers=self._rpc_headers(),
            method="POST",
        )
        try:
            raw = self._request(request)
            payload = parse_rpc(raw, "xdSrCf")
        except (StoreError, ParseError) as exc:
            raise StoreError(f"Could not read permissions: {exc}") from exc
        return parse_permissions(payload)

    def fetch_reviews(
        self,
        app_id: str,
        sort: int,
        count: int,
        token: str | None,
        score: int | None,
        device: int | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        request = urllib.request.Request(
            self._rpc_url(),
            data=reviews_form(app_id, sort, count, token, score, device),
            headers=self._rpc_headers(),
            method="POST",
        )
        try:
            raw = self._request(request)
            return parse_review_response(raw)
        except ParseError as exc:
            raise StoreError(str(exc)) from exc
