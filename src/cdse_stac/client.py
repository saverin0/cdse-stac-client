"""Talk to the Copernicus STAC search: paced, with time limits and careful retries.

The rules, in plain words:
- Every request waits its turn in a token bucket, so we stay under the
  server's rate limit instead of running into it.
- Every request has three time limits: for connecting, for any silence while
  reading, and for the whole answer. An answer larger than max_page_bytes is refused.
- Only failures that can pass by themselves are retried: 408, 425, 429 (too
  many requests), most 5xx (server trouble), time-outs, dropped connections,
  and a 200 answer that is not a readable search result. The waits double
  each time and get a random part, so many clients do not all retry at the
  same moment. A Retry-After header from the server wins.
- Any other 4xx means our request is wrong. It fails at once, with the
  server's own message, cleaned of control characters.
- Redirects are never followed, and "next page" links are followed only to the
  allowed Copernicus hosts, with the same question as the first request, so an
  access token can never be sent anywhere else.
- Proxy, certificate and .netrc settings from the environment are ignored.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
import urllib3

from cdse_stac.config import Query, SourceSettings, url_problem
from cdse_stac.errors import CredentialsRejected, FetchError
from cdse_stac.jsonsafe import loads, printable, unstorable

log = logging.getLogger(__name__)

RETRY_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    urllib3.exceptions.ProtocolError,  # the connection broke while the body was read
    urllib3.exceptions.ReadTimeoutError,
    urllib3.exceptions.DecodeError,  # a broken compressed body
    urllib3.exceptions.SSLError,  # the secure connection dropped while the body was read
)
BACKOFF_BASE_S = 1.0  # server trouble: wait 1, 2, 4, 8 s, plus up to half again at random
RATE_LIMIT_BASE_S = 10.0  # 429 from the server's firewall: wait 10, 20, 40 s, plus random
BACKOFF_CAP_S = 60.0
RETRY_AFTER_CAP_S = 300.0  # never wait longer than this, even if the server asks
READ_CHUNK = 64 * 1024
USER_AGENT = "cdse-stac-client/0.1 (parcel scene search, staging ingest)"
SAVED_HEADERS = ("content-type", "content-length", "content-encoding", "date", "server", "x-request-id")


class _TooLarge(Exception):
    """The answer is larger than max_page_bytes. Asking again will not help."""


class _TooSlow(Exception):
    """The answer took longer than total_timeout_s. Worth another try."""


@dataclass(frozen=True)
class Page:
    """One answer from the server, exactly as received, and read once."""

    number: int
    url: str
    request_body: dict
    status: int
    seconds: float
    attempts: int
    raw: bytes
    headers: dict = field(default_factory=dict)
    answer: dict = field(default_factory=dict, repr=False)

    @property
    def scenes(self) -> int:
        return len(self.answer.get("features") or [])


def token_problem(token: str) -> str | None:
    """Why this access token cannot be used, or None. The token itself is never shown."""
    if len(token) > 8192:
        return "it is longer than 8192 characters"
    if any(not 0x21 <= ord(char) <= 0x7E for char in token):
        return "it contains spaces, line ends or characters outside plain ASCII"
    return None


def retryable(status: int) -> bool:
    """HTTP answers that can pass by themselves. 501 and 505 never will."""
    return status in (408, 425, 429) or (500 <= status <= 599 and status not in (501, 505))


class TokenBucket:
    """Hands out one token per request and refills at a fixed rate.

    With a capacity of 1 it is a plain pacer: one request every 1/rate seconds.
    """

    def __init__(
        self,
        rate_per_second: float,
        capacity: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = rate_per_second
        self.capacity = capacity
        self.tokens = capacity
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()

    def wait(self) -> None:
        self._refill()
        if self.tokens < 1:
            self.sleep((1 - self.tokens) / self.rate)
            self._refill()
        self.tokens -= 1

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now


def retry_wait_seconds(
    attempt: int,
    status: int | None,
    retry_after: str | None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait after failed try number `attempt`, counted from 1."""
    if retry_after is not None:
        try:
            asked = float(retry_after)
        except ValueError:
            asked = None  # Retry-After can also be a date; then we use our own backoff
        if asked is not None and 0 <= asked < float("inf"):
            return min(asked, RETRY_AFTER_CAP_S)
    base = RATE_LIMIT_BASE_S if status == 429 else BACKOFF_BASE_S
    delay = min(BACKOFF_CAP_S, base * 2 ** (attempt - 1))
    return delay + rng() * delay / 2


def answer_problem(raw: bytes) -> tuple[dict | None, str | None]:
    """(answer, None) for a readable search result, or (None, why not)."""
    try:
        answer = loads(raw)
    except ValueError as error:
        return None, f"not readable JSON: {error}"
    if not isinstance(answer, dict):
        return None, "not a JSON object"
    if not isinstance(answer.get("features"), list):
        return None, "no features list"
    if answer.get("links") is not None and not isinstance(answer.get("links"), list):
        return None, "links is not a list"
    return answer, None


def parse_answer(page: Page) -> dict:
    """The page as a dict with a features list. FetchError if it cannot be read."""
    answer, problem = answer_problem(page.raw)
    if problem:
        raise FetchError(f"page {page.number} is not a search result: {problem}")
    return answer


def same_question(body: dict, first_body: dict) -> str | None:
    """Why a next-link body asks a different question than the first request, or None.

    The server keeps the question and adds a paging token. A next link that drops or
    changes a query term (or asks to merge) would walk a different part of the catalogue.
    """
    for key in sorted(set(body) | set(first_body)):
        if key == "token":
            continue
        if body.get(key) != first_body.get(key):
            return f"it changes {key!r}"
    return None


def next_request(
    answer: dict, allowed_hosts: tuple[str, ...], first_body: dict | None = None, base_url: str = ""
) -> tuple[str, dict] | None:
    """The address and body of the next page, or None on the last page."""
    links = answer.get("links")
    if links is None:
        return None
    if not isinstance(links, list):
        raise FetchError("the answer's links are not a list")
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        body = link.get("body")
        if str(link.get("method", "GET")).upper() != "POST" or not isinstance(body, dict):
            raise FetchError("the next link is not a POST with a body; this client pages with POST only")
        if link.get("merge"):
            raise FetchError("the next link asks to merge its body with the request; this client sends it whole")
        problem = unstorable(body, "next link body")
        if problem:
            raise FetchError(f"refusing the next link: {problem}")
        if first_body is not None:
            changed = same_question(body, first_body)
            if changed:
                raise FetchError(f"refusing the next link: {changed} of the question")
        href = link.get("href")
        url = urljoin(base_url, href) if isinstance(href, str) and base_url else href
        _check_host(url, allowed_hosts)
        return url, body
    return None


def _check_host(url: object, allowed_hosts: tuple[str, ...]) -> None:
    """Refuse any address that is not plainly an https address on an allowed Copernicus host."""
    problem = url_problem(url, allowed_hosts)
    if problem:
        raise FetchError(f"refusing to follow {printable(str(url), 200)!r}: {problem}")


class CdseClient:
    """Sends searches to the catalogue and walks through the pages."""

    def __init__(
        self,
        source: SourceSettings,
        token: str | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        logger: logging.Logger | logging.LoggerAdapter | None = None,
        on_attempt: Callable[[dict], None] | None = None,
    ) -> None:
        if token:
            problem = token_problem(token)
            if problem:
                raise FetchError(f"CDSE_ACCESS_TOKEN cannot be used: {problem}")
        self.source = source
        self.log = logger or log
        self.session = session or requests.Session()
        self.session.trust_env = False  # no proxies, CA bundles or .netrc logins from the environment
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"})
        self._token = token or None
        self.sleep = sleep
        self.rng = rng
        self.bucket = TokenBucket(source.requests_per_second, clock=clock, sleep=sleep)
        self.rate_limited = 0  # how many times the server answered 429
        self.on_attempt = on_attempt  # told about every failed attempt, so the fetch log can keep it

    def __repr__(self) -> str:
        token = "set" if self._token else "not set"
        return f"CdseClient(search_url={self.source.search_url!r}, token={token})"

    def pages(
        self,
        query: Query,
        start: tuple[str, dict] | None = None,
        first_number: int = 1,
    ) -> Iterator[Page]:
        """Yield every page of the search, oldest scenes first.

        Each page is handed out before the next request is sent, so the caller
        can write it to the log first. `start` resumes from a saved next link.
        """
        first_body = query.search_body(self.source.page_size)
        url, body = start or (self.source.search_url, first_body)
        seen_bodies = {json.dumps(first_body, sort_keys=True)}
        seen_scenes: set[str] = set()
        number = first_number
        while True:
            page = self.post(url, body, number)
            yield page
            ids = {item.get("id") for item in page.answer["features"] if isinstance(item, dict) and isinstance(item.get("id"), str)}
            if ids and ids <= seen_scenes:
                raise FetchError(f"page {number} only repeats scenes from earlier pages; the paging is looping")
            seen_scenes |= ids
            following = next_request(page.answer, self.source.allowed_hosts, first_body, url)
            if following is None:
                return
            url, body = following
            key = json.dumps(body, sort_keys=True)
            if key in seen_bodies:
                raise FetchError(f"the server sent the same next page twice after page {number}; stopping")
            seen_bodies.add(key)
            number += 1
            if number - first_number >= self.source.max_pages:
                raise FetchError(f"more than max_pages ({self.source.max_pages}) pages; stopping as a safety net")

    def post(self, url: str, body: dict, number: int) -> Page:
        """Fetch one page, with pacing, time limits, a size limit and retries."""
        _check_host(url, self.source.allowed_hosts)
        timeout = (self.source.connect_timeout_s, self.source.read_timeout_s)
        attempt = 0
        while True:
            attempt += 1
            self.bucket.wait()
            started = time.perf_counter()
            status: int | None = None
            retry_after: str | None = None
            raw = b""
            try:
                response = self.session.post(
                    url, json=body, headers=self._auth_header(), timeout=timeout, allow_redirects=False, stream=True
                )
                try:
                    raw = self._read_body(response, started)
                finally:
                    response.close()
            except requests.exceptions.SSLError as error:  # a failed certificate check does not pass by itself
                raise FetchError(f"the secure connection for page {number} failed: {self._clean(str(error))}") from None
            except RETRY_ERRORS as error:
                problem = type(error).__name__
            except _TooSlow as error:
                problem = str(error)
            except _TooLarge as error:
                raise FetchError(f"page {number}: {error}") from None
            except (requests.RequestException, urllib3.exceptions.HTTPError) as error:
                raise FetchError(f"page {number} failed: {type(error).__name__}: {self._clean(str(error))}") from None
            else:
                status = response.status_code
                if status == 200:
                    answer, problem = answer_problem(raw)
                    if answer is not None:
                        self._check_counts(answer, number)
                        return Page(
                            number=number,
                            url=url,
                            request_body=body,
                            status=status,
                            seconds=time.perf_counter() - started,
                            attempts=attempt,
                            raw=raw,
                            headers={name: response.headers[name] for name in SAVED_HEADERS if name in response.headers},
                            answer=answer,
                        )
                    problem = f"HTTP 200 but {problem}"
                elif 300 <= status < 400:
                    where = self._clean(response.headers.get("Location", "nowhere"))
                    raise FetchError(f"HTTP {status} for page {number}: the server redirects to {where}; redirects are not followed")
                elif status in (401, 403):
                    raise self._access_refused(status, raw)
                elif retryable(status):
                    if status == 429:
                        self.rate_limited += 1
                    problem = f"HTTP {status}"
                    retry_after = response.headers.get("Retry-After")
                else:
                    raise FetchError(f"HTTP {status} for page {number}: {self._server_message(raw)}")
            seconds = time.perf_counter() - started
            last = attempt >= self.source.max_attempts
            wait = 0.0 if last else retry_wait_seconds(attempt, status, retry_after, self.rng)
            if self.on_attempt is not None:
                self.on_attempt(
                    dict(
                        page=number,
                        attempt=attempt,
                        status=status,
                        problem=problem,
                        seconds=round(seconds, 3),
                        retry_in=None if last else round(wait, 1),
                        server_text=self._server_message(raw) if raw else "",
                    )
                )
            if last:
                raise FetchError(f"gave up on page {number} after {attempt} attempts; last problem: {problem}")
            self.log.warning("page=%s attempt=%s problem=%s retry_in=%.1fs", number, attempt, problem, wait)
            self.sleep(wait)

    def _read_body(self, response: requests.Response, started: float) -> bytes:
        """The whole body, decompressed, within max_page_bytes and total_timeout_s.

        read1 returns after a single read from the network, so the time limit is
        checked at least once per read_timeout_s even when a server sends one byte at a time.
        """
        deadline = started + self.source.total_timeout_s
        limit = self.source.max_page_bytes
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = response.raw.read1(READ_CHUNK, decode_content=True)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > limit:
                raise _TooLarge(f"the answer is larger than max_page_bytes ({limit} bytes)")
            if time.perf_counter() > deadline:
                raise _TooSlow(f"the answer took longer than total_timeout_s ({self.source.total_timeout_s} s)")
            chunks.append(chunk)

    def _check_counts(self, answer: dict, number: int) -> None:
        returned = answer.get("numberReturned")
        if isinstance(returned, int) and not isinstance(returned, bool) and returned != len(answer["features"]):
            self.log.warning(
                "page=%s numberReturned=%s but the page holds %s scenes", number, returned, len(answer["features"])
            )

    def _clean(self, text: str) -> str:
        """Text from outside, safe to print and store: no control characters, never the token."""
        if self._token:
            text = text.replace(self._token, "[token]")
        return printable(text)

    def _server_message(self, raw: bytes) -> str:
        """The server's own error words, short and cleaned."""
        try:
            payload = loads(raw)
        except ValueError:
            return self._clean(raw.decode("utf-8", errors="replace")) or "(empty answer)"
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        if isinstance(detail, dict):
            parts = [str(detail[key]) for key in ("code", "message", "error") if key in detail]
            if parts:
                return self._clean(": ".join(parts))
        return self._clean(json.dumps(detail, ensure_ascii=False))

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _access_refused(self, status: int, raw: bytes) -> FetchError:
        said = self._server_message(raw)
        if self._token:
            return CredentialsRejected(
                f"HTTP {status}: the server refused the request; if the access token is the cause, "
                f"check CDSE_ACCESS_TOKEN (server says: {said})"
            )
        return FetchError(f"HTTP {status} for an anonymous search: {said}")
