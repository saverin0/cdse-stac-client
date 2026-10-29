import json
from pathlib import Path

import pytest
import requests
import responses

from cdse_stac.client import (
    CdseClient,
    CredentialsRejected,
    FetchError,
    Page,
    TokenBucket,
    answer_problem,
    next_request,
    parse_answer,
    retry_wait_seconds,
    token_problem,
)
from cdse_stac.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = load_settings(ROOT / "config" / "bornheim.toml")
SEARCH_URL = SETTINGS.source.search_url
NEXT_URL = "https://stac.dataspace.copernicus.eu/v1/search"  # where the server's next links point
TOKEN = "secret-token-do-not-print"
FIRST_BODY = SETTINGS.query.search_body(SETTINGS.source.page_size)


def answer(name):
    return json.loads((ROOT / "tests" / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))["response"]


def first_page():
    """Page 1 whose next link repeats the question, as the real server does (the fixture was captured
    for a search without the cloud filter)."""
    data = answer("search_page1")
    for link in data["links"]:
        if link["rel"] == "next":
            link["body"] = {**FIRST_BODY, "token": link["body"]["token"]}
    return data


def last_page():
    data = answer("search_page2")
    data["links"] = [link for link in data["links"] if link.get("rel") != "next"]
    return data


class FakeTime:
    """A clock that only moves when the code sleeps, so tests run instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


@pytest.fixture
def fake_time():
    return FakeTime()


def make_client(fake_time, token=None, source=None, **kwargs):
    return CdseClient(
        source or SETTINGS.source, token=token, sleep=fake_time.sleep, clock=fake_time.clock, rng=lambda: 0.0, **kwargs
    )


def sent_body(call):
    return json.loads(call.request.body)


def with_next(data, **changes):
    for link in data["links"]:
        if link["rel"] == "next":
            link.update(changes)
    return data


@responses.activate
def test_follows_next_links_until_the_last_page(fake_time):
    first = first_page()
    responses.post(SEARCH_URL, json=first)
    responses.post(NEXT_URL, json=last_page())
    pages = list(make_client(fake_time).pages(SETTINGS.query))
    assert [page.number for page in pages] == [1, 2]
    expected = next(link for link in first["links"] if link["rel"] == "next")["body"]
    assert sent_body(responses.calls[1]) == expected
    assert "token" in sent_body(responses.calls[1])


@responses.activate
def test_first_request_is_the_query_with_time_limits(fake_time):
    responses.post(SEARCH_URL, json=answer("search_empty"))
    list(make_client(fake_time).pages(SETTINGS.query))
    call = responses.calls[0]
    assert sent_body(call) == FIRST_BODY
    assert call.request.req_kwargs["timeout"] == (5.0, 30.0)


@responses.activate
def test_empty_result_is_one_page_and_no_error(fake_time):
    responses.post(SEARCH_URL, json=answer("search_empty"))
    pages = list(make_client(fake_time).pages(SETTINGS.query))
    assert len(pages) == 1
    assert parse_answer(pages[0])["features"] == []


@responses.activate
def test_too_many_requests_is_retried_after_a_long_wait(fake_time):
    responses.post(SEARCH_URL, status=429, json={"error": "WAF", "message": "Rate limit exceeded", "status": 429})
    responses.post(SEARCH_URL, json=answer("search_empty"))
    client = make_client(fake_time)
    pages = list(client.pages(SETTINGS.query))
    assert pages[0].attempts == 2
    assert client.rate_limited == 1
    assert 10.0 in fake_time.sleeps


@responses.activate
def test_retry_after_header_wins(fake_time):
    responses.post(SEARCH_URL, status=503, headers={"Retry-After": "7"})
    responses.post(SEARCH_URL, json=answer("search_empty"))
    list(make_client(fake_time).pages(SETTINGS.query))
    assert 7.0 in fake_time.sleeps


@responses.activate
def test_dropped_connection_is_retried(fake_time):
    responses.post(SEARCH_URL, body=requests.ConnectionError("connection reset"))
    responses.post(SEARCH_URL, json=answer("search_empty"))
    pages = list(make_client(fake_time).pages(SETTINGS.query))
    assert pages[0].attempts == 2


@responses.activate
def test_gives_up_after_the_last_attempt(fake_time):
    responses.post(SEARCH_URL, status=502)
    with pytest.raises(FetchError, match="gave up"):
        list(make_client(fake_time).pages(SETTINGS.query))
    assert len(responses.calls) == SETTINGS.source.max_attempts


@responses.activate
def test_bad_request_fails_at_once_with_the_server_message(fake_time):
    error = answer("error_unknown_collection")
    responses.post(SEARCH_URL, status=400, json=error)
    with pytest.raises(FetchError, match="CollectionInQuerryDoesNotExist"):
        list(make_client(fake_time).pages(SETTINGS.query))
    assert len(responses.calls) == 1


@responses.activate
def test_refused_token_is_reported_without_showing_it(fake_time):
    responses.post(SEARCH_URL, status=401, json={"detail": "invalid token"})
    with pytest.raises(CredentialsRejected) as caught:
        list(make_client(fake_time, token=TOKEN).pages(SETTINGS.query))
    assert TOKEN not in str(caught.value)
    assert "invalid token" in str(caught.value)  # the server's own words, so a firewall answer is not blamed on the token


@responses.activate
def test_token_goes_in_the_header_and_never_into_the_logs(fake_time, caplog):
    caplog.set_level("DEBUG")
    responses.post(SEARCH_URL, status=503)
    responses.post(SEARCH_URL, json=answer("search_empty"))
    client = make_client(fake_time, token=TOKEN)
    list(client.pages(SETTINGS.query))
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert caplog.records, "the retry was logged"
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(client)


@responses.activate
def test_no_token_means_no_authorization_header(fake_time):
    responses.post(SEARCH_URL, json=answer("search_empty"))
    list(make_client(fake_time).pages(SETTINGS.query))
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_next_link_to_a_foreign_host_is_refused(fake_time):
    responses.post(SEARCH_URL, json=with_next(first_page(), href="https://example.com/steal"))
    with pytest.raises(FetchError, match="refusing to follow"):
        list(make_client(fake_time, token=TOKEN).pages(SETTINGS.query))
    assert len(responses.calls) == 1


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example\\@stac.dataspace.copernicus.eu/v1/search",  # our parser and requests read different hosts
        "https://user@stac.dataspace.copernicus.eu/v1/search",  # a user part can hide where a link really goes
        "https://stac.dataspace.copernicus.eu@evil.example/v1/search",
        "https://stac.dataspace.copernicus.eu:8443/v1/search",  # another port on the right host
        "https://STAC.dataspace.copernicus.eu./v1/search",  # a trailing dot names another host to some resolvers
        "http://stac.dataspace.copernicus.eu/v1/search",
    ],
)
@responses.activate
def test_next_link_tricks_are_refused_before_any_request(fake_time, href):
    responses.post(SEARCH_URL, json=with_next(first_page(), href=href))
    with pytest.raises(FetchError, match="refusing to follow"):
        list(make_client(fake_time, token=TOKEN).pages(SETTINGS.query))
    assert len(responses.calls) == 1  # the token never went anywhere else


@responses.activate
def test_relative_next_link_is_resolved_against_the_page_it_came_from(fake_time):
    responses.post(SEARCH_URL, json=with_next(first_page(), href="/stac/search"))
    responses.post(SEARCH_URL, json=last_page())
    assert len(list(make_client(fake_time).pages(SETTINGS.query))) == 2


@responses.activate
def test_redirect_is_not_followed(fake_time):
    # A redirect is followed after the host check otherwise, which would carry the request anywhere.
    responses.post(SEARCH_URL, status=307, headers={"Location": "http://evil.example/collect"})
    responses.post("http://evil.example/collect", json=answer("search_empty"))
    with pytest.raises(FetchError, match="redirects are not followed"):
        list(make_client(fake_time, token=TOKEN).pages(SETTINGS.query))
    assert [call.request.url for call in responses.calls] == [SEARCH_URL]


@responses.activate
def test_other_success_codes_are_not_a_result_page(fake_time):
    responses.post(SEARCH_URL, status=204)
    with pytest.raises(FetchError, match="HTTP 204"):
        list(make_client(fake_time).pages(SETTINGS.query))


@responses.activate
def test_next_link_that_changes_the_question_is_refused(fake_time):
    first = first_page()
    for link in first["links"]:
        if link["rel"] == "next":
            del link["body"]["filter"]  # without the cloud filter the next pages would walk other scenes
    responses.post(SEARCH_URL, json=first)
    with pytest.raises(FetchError, match="changes 'filter'"):
        list(make_client(fake_time).pages(SETTINGS.query))


@responses.activate
def test_next_link_asking_to_merge_is_refused(fake_time):
    responses.post(SEARCH_URL, json=with_next(first_page(), merge=True))
    with pytest.raises(FetchError, match="merge"):
        list(make_client(fake_time).pages(SETTINGS.query))


@responses.activate
def test_next_link_equal_to_the_first_request_is_refused(fake_time):
    first = first_page()
    for link in first["links"]:
        if link["rel"] == "next":
            link["body"] = dict(FIRST_BODY)
    responses.post(SEARCH_URL, json=first)
    with pytest.raises(FetchError, match="same next page twice"):
        list(make_client(fake_time).pages(SETTINGS.query))
    assert len(responses.calls) == 1


@responses.activate
def test_repeated_page_stops_the_run(fake_time):
    first = first_page()
    responses.post(SEARCH_URL, json=first)
    responses.post(NEXT_URL, json=first)  # the server sends page 1 again, with the same next link
    with pytest.raises(FetchError, match="repeats scenes|same next page twice"):
        list(make_client(fake_time).pages(SETTINGS.query))


@responses.activate
def test_paging_loop_with_new_tokens_is_caught(fake_time):
    first = first_page()
    again = with_next(first_page(), body={**FIRST_BODY, "token": "next:another-token"})  # new token, same scenes
    responses.post(SEARCH_URL, json=first)
    responses.post(NEXT_URL, json=again)
    with pytest.raises(FetchError, match="only repeats scenes"):
        list(make_client(fake_time).pages(SETTINGS.query))


@responses.activate
def test_page_limit_comes_from_the_settings(fake_time):
    source = SETTINGS.source.model_copy(update={"max_pages": 1})
    responses.post(SEARCH_URL, json=first_page())
    with pytest.raises(FetchError, match="max_pages"):
        list(make_client(fake_time, source=source).pages(SETTINGS.query))


@pytest.mark.parametrize(
    "body",
    ["<html>proxy error</html>", "[1, 2]", '{"type": "FeatureCollection"}', '{"features": [], "links": 5}', "[" * 100_000],
    ids=["html", "list", "no-features", "links-number", "deep-nesting"],
)
@responses.activate
def test_a_200_that_is_not_a_search_result_is_retried_then_refused(fake_time, body):
    responses.post(SEARCH_URL, body=body, status=200)
    tries = []
    with pytest.raises(FetchError, match="gave up.*HTTP 200 but"):
        list(make_client(fake_time, on_attempt=tries.append).pages(SETTINGS.query))
    assert len(responses.calls) == SETTINGS.source.max_attempts
    assert [attempt["attempt"] for attempt in tries] == list(range(1, SETTINGS.source.max_attempts + 1))


@responses.activate
def test_a_200_that_is_readable_on_the_second_try_is_kept(fake_time):
    responses.post(SEARCH_URL, body="<html>proxy error</html>", status=200)
    responses.post(SEARCH_URL, json=answer("search_empty"))
    pages = list(make_client(fake_time).pages(SETTINGS.query))
    assert pages[0].attempts == 2 and pages[0].scenes == 0


@responses.activate
def test_408_and_unlisted_5xx_are_retried_but_501_is_not(fake_time):
    responses.post(SEARCH_URL, status=408)
    responses.post(SEARCH_URL, status=507)
    responses.post(SEARCH_URL, json=answer("search_empty"))
    assert list(make_client(fake_time).pages(SETTINGS.query))[0].attempts == 3
    responses.reset()
    responses.post(SEARCH_URL, status=501)
    with pytest.raises(FetchError, match="HTTP 501"):
        list(make_client(fake_time).pages(SETTINGS.query))
    assert len(responses.calls) == 1


@responses.activate
def test_broken_compressed_body_is_retried(fake_time):
    responses.post(SEARCH_URL, body=b"not gzip at all", status=200, headers={"Content-Encoding": "gzip"})
    responses.post(SEARCH_URL, json=answer("search_empty"))
    assert list(make_client(fake_time).pages(SETTINGS.query))[0].attempts == 2


@responses.activate
def test_failed_certificate_check_is_not_retried(fake_time):
    responses.post(SEARCH_URL, body=requests.exceptions.SSLError("certificate verify failed"))
    with pytest.raises(FetchError, match="secure connection"):
        list(make_client(fake_time).pages(SETTINGS.query))
    assert len(responses.calls) == 1


@responses.activate
def test_other_request_errors_become_one_clear_message(fake_time):
    responses.post(SEARCH_URL, body=requests.exceptions.InvalidHeader("bad header"))
    with pytest.raises(FetchError, match="InvalidHeader"):
        list(make_client(fake_time).pages(SETTINGS.query))


@responses.activate
def test_answer_above_the_size_limit_is_refused(fake_time):
    source = SETTINGS.source.model_copy(update={"max_page_bytes": 10_000})
    big = answer("search_page1")  # about 350 KB
    responses.post(SEARCH_URL, json=big)
    with pytest.raises(FetchError, match="max_page_bytes"):
        list(make_client(fake_time, source=source).pages(SETTINGS.query))


class DrippingBody:
    """A response body that sends one byte at a time, never finishing."""

    def read1(self, amount, decode_content=True):
        return b"x"


class DrippingResponse:
    raw = DrippingBody()


def test_answer_that_drips_in_hits_the_total_time_limit(monkeypatch):
    client = CdseClient(SETTINGS.source.model_copy(update={"total_timeout_s": 5.0}))
    times = iter(range(0, 1000))
    monkeypatch.setattr("cdse_stac.client.time.perf_counter", lambda: next(times))
    with pytest.raises(Exception, match="total_timeout_s"):
        client._read_body(DrippingResponse(), started=0)


@responses.activate
def test_failed_attempts_are_reported_with_the_server_text(fake_time):
    responses.post(SEARCH_URL, status=503, json={"detail": {"message": "maintenance"}})
    responses.post(SEARCH_URL, json=answer("search_empty"))
    tries = []
    list(make_client(fake_time, on_attempt=tries.append).pages(SETTINGS.query))
    assert tries[0]["status"] == 503 and tries[0]["server_text"] == "maintenance" and tries[0]["retry_in"] == 1.0


@responses.activate
def test_server_text_is_cleaned_of_control_characters_and_the_token(fake_time):
    responses.post(SEARCH_URL, status=400, body=f"bad request\x1b[31m red Authorization: Bearer {TOKEN}")
    with pytest.raises(FetchError) as caught:
        list(make_client(fake_time, token=TOKEN).pages(SETTINGS.query))
    assert "\x1b" not in str(caught.value) and "\\x1b" in str(caught.value)
    assert TOKEN not in str(caught.value)


@responses.activate
def test_page_keeps_useful_response_headers(fake_time):
    responses.post(SEARCH_URL, json=answer("search_empty"), headers={"x-request-id": "abc"})
    page = list(make_client(fake_time).pages(SETTINGS.query))[0]
    assert page.headers["content-type"].startswith("application/json") and page.headers["x-request-id"] == "abc"


@responses.activate
def test_count_mismatch_is_logged(fake_time, caplog):
    data = answer("search_empty")
    data["numberReturned"] = 5
    responses.post(SEARCH_URL, json=data)
    list(make_client(fake_time).pages(SETTINGS.query))
    assert "numberReturned=5" in caplog.text


@pytest.mark.parametrize("token", ["with space", "line\nend", "tab\there", "café", ""])
def test_token_with_odd_characters_is_refused_without_showing_it(token):
    if not token:
        assert CdseClient(SETTINGS.source, token=token)._token is None  # empty means no token
        return
    assert token_problem(token)
    with pytest.raises(FetchError) as caught:
        CdseClient(SETTINGS.source, token=token)
    assert token not in str(caught.value)


def test_session_ignores_proxies_and_logins_from_the_environment():
    assert CdseClient(SETTINGS.source).session.trust_env is False


def test_next_link_body_that_cannot_be_stored_is_refused():
    data = with_next(first_page(), body={**FIRST_BODY, "token": "next:\ud800"})
    with pytest.raises(FetchError, match="lone surrogate"):
        next_request(data, SETTINGS.source.allowed_hosts, FIRST_BODY)


def test_links_that_are_not_a_list_are_reported():
    assert answer_problem(json.dumps({"features": [], "links": True}).encode())[1] == "links is not a list"


def test_parse_answer_reports_deep_nesting_as_unreadable():
    page = Page(number=1, url=SEARCH_URL, request_body={}, status=200, seconds=0.1, attempts=1, raw=b"[" * 100_000)
    with pytest.raises(FetchError, match="nested too deeply"):
        parse_answer(page)


def test_token_bucket_paces_requests(fake_time):
    bucket = TokenBucket(1.0, clock=fake_time.clock, sleep=fake_time.sleep)
    for _ in range(3):
        bucket.wait()
    assert fake_time.sleeps == [1.0, 1.0]


@pytest.mark.parametrize(
    "attempt, status, expected",
    [(1, 503, 1.0), (2, 503, 2.0), (3, 503, 4.0), (4, 503, 8.0), (1, 429, 10.0), (3, 429, 40.0), (5, 429, 60.0)],
)
def test_backoff_doubles_and_is_capped(attempt, status, expected):
    assert retry_wait_seconds(attempt, status, None, rng=lambda: 0.0) == expected


def test_backoff_adds_a_random_part():
    assert retry_wait_seconds(1, 503, None, rng=lambda: 1.0) == 1.5


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "-5", "inf", "nan"])
def test_unusable_retry_after_falls_back_to_backoff(header):
    assert retry_wait_seconds(2, 503, header, rng=lambda: 0.0) == 2.0


def test_retry_after_is_capped():
    assert retry_wait_seconds(1, 503, "99999") == 300.0


def test_next_request_is_none_on_the_last_page():
    assert next_request(last_page(), SETTINGS.source.allowed_hosts) is None


@responses.activate
def test_tls_drop_while_reading_the_body_is_retried(fake_time, monkeypatch):
    import urllib3

    responses.post(SEARCH_URL, json=answer("search_empty"))
    responses.post(SEARCH_URL, json=answer("search_empty"))
    original, calls = CdseClient._read_body, []

    def flaky(self, response, started):
        calls.append(1)
        if len(calls) == 1:
            raise urllib3.exceptions.SSLError("connection dropped in violation of protocol")
        return original(self, response, started)

    monkeypatch.setattr(CdseClient, "_read_body", flaky)
    assert list(make_client(fake_time).pages(SETTINGS.query))[0].attempts == 2


@responses.activate
def test_no_urllib3_error_leaves_the_client_raw(fake_time, monkeypatch):
    import urllib3

    responses.post(SEARCH_URL, json=answer("search_empty"))

    def broken(self, response, started):
        raise urllib3.exceptions.LocationParseError("odd")

    monkeypatch.setattr(CdseClient, "_read_body", broken)
    with pytest.raises(FetchError, match="LocationParseError"):
        list(make_client(fake_time).pages(SETTINGS.query))
