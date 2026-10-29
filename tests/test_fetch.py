import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import responses

from cdse_stac.client import CdseClient, FetchError
from cdse_stac.config import load_settings
from cdse_stac.fetch import LOCK_NAME, run_fetch
from cdse_stac.runlog import RunLog, page_entry, read_log, start_entry

ROOT = Path(__file__).resolve().parent.parent
BASE_SETTINGS = load_settings(ROOT / "config" / "bornheim.toml")
SEARCH_URL = BASE_SETTINGS.source.search_url
NEXT_URL = "https://stac.dataspace.copernicus.eu/v1/search"
FIXED_NOW = datetime(2026, 10, 28, 15, 0, 0, tzinfo=timezone.utc)
FIRST_BODY = BASE_SETTINGS.query.search_body(BASE_SETTINGS.source.page_size)


def answer(name):
    return json.loads((ROOT / "tests" / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))["response"]


def first_page():
    """Page 1 whose next link repeats the question, as the real server does."""
    data = answer("search_page1")
    for link in data["links"]:
        if link["rel"] == "next":
            link["body"] = {**FIRST_BODY, "token": link["body"]["token"]}
    return data


def last_page():
    data = answer("search_page2")
    data["links"] = [link for link in data["links"] if link.get("rel") != "next"]
    return data


@pytest.fixture
def settings(tmp_path):
    return BASE_SETTINGS.model_copy(update={"runs_dir": tmp_path / "runs"})


def quick_client(settings):
    """A client that does not really sleep, so the tests run instantly."""
    return CdseClient(settings.source, sleep=lambda seconds: None, rng=lambda: 0.0)


def fetch(settings, **kwargs):
    return run_fetch(settings, client=quick_client(settings), now=lambda: FIXED_NOW, **kwargs)


def status_of(run_dir):
    return json.loads((run_dir / "status.json").read_text(encoding="utf-8"))


def only_run_dir(settings):
    return next((settings.runs_dir / settings.query.parcel_id).iterdir())


@responses.activate
def test_fetch_writes_start_pages_and_end(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, json=last_page())
    result = fetch(settings)
    contents = read_log(result.log_path)
    assert [entry["kind"] for entry in contents.entries] == ["start", "page", "page", "end"]
    assert contents.start["query_fingerprint"] == settings.query.fingerprint()
    assert result.pages == 2 and result.scenes == 6
    assert result.fetch_id == "20261028T150000Z_" + settings.query.fingerprint()[:8]
    assert status_of(result.run_dir)["status"] == "complete"
    assert not (result.run_dir / LOCK_NAME).exists()  # the lock is gone once the fetch ends


@responses.activate
def test_empty_search_is_a_complete_run_with_no_scenes(settings):
    responses.post(SEARCH_URL, json=answer("search_empty"))
    result = fetch(settings)
    assert result.pages == 1 and result.scenes == 0
    assert read_log(result.log_path).complete


@responses.activate
def test_failed_fetch_keeps_its_pages_and_says_why(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": {"code": "BadToken", "message": "token expired"}})
    with pytest.raises(FetchError):
        fetch(settings)
    run_dir = only_run_dir(settings)
    contents = read_log(run_dir / "fetch.jsonl")
    assert [entry["kind"] for entry in contents.entries] == ["start", "page"]
    status = status_of(run_dir)
    assert status["status"] == "failed" and "token expired" in status["error"]
    assert not (run_dir / LOCK_NAME).exists()


@responses.activate
def test_failed_attempts_are_written_to_the_log(settings):
    responses.post(SEARCH_URL, status=429, json={"message": "Rate limit exceeded"})
    responses.post(SEARCH_URL, json=answer("search_empty"))
    result = fetch(settings)
    entries = read_log(result.log_path).entries
    attempt = next(entry for entry in entries if entry["kind"] == "attempt")
    assert (attempt["page"], attempt["status"], attempt["server_text"]) == (1, 429, "Rate limit exceeded")
    assert entries[-1]["rate_limited"] == 1


@responses.activate
def test_resume_goes_on_from_the_saved_next_link_and_sets_a_torn_line_aside(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    run_dir = only_run_dir(settings)
    with open(run_dir / "fetch.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"half a line written when the power went')  # a crash on top of the failure

    responses.reset()
    responses.post(NEXT_URL, json=last_page())
    result = fetch(settings, resume_dir=run_dir)
    contents = read_log(result.log_path)
    assert [page["page"] for page in contents.pages] == [1, 2]
    assert contents.complete
    assert contents.damaged == []  # the half line was moved aside, so the resumed log is whole
    assert "half a line" in (run_dir / "fetch.jsonl.torn").read_text(encoding="utf-8")
    assert json.loads(responses.calls[0].request.body)["token"].startswith("next:")


@responses.activate
def test_resume_counts_rate_limits_from_before_the_stop(settings):
    responses.post(SEARCH_URL, status=429)
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    run_dir = only_run_dir(settings)
    assert status_of(run_dir)["rate_limited"] == 1
    responses.reset()
    responses.post(NEXT_URL, json=last_page())
    result = fetch(settings, resume_dir=run_dir)
    assert result.rate_limited == 1 and read_log(result.log_path).entries[-1]["rate_limited"] == 1


@responses.activate
def test_resume_refuses_a_log_from_a_different_question(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    other = settings.model_copy(update={"query": settings.query.model_copy(update={"max_cloud_cover_pct": 20.0})})
    with pytest.raises(FetchError, match="different question"):
        fetch(other, resume_dir=only_run_dir(settings))


@responses.activate
def test_resuming_a_complete_log_sends_nothing(settings):
    responses.post(SEARCH_URL, json=answer("search_empty"))
    result = fetch(settings)
    responses.reset()
    again = fetch(settings, resume_dir=result.run_dir)
    assert again.pages == 1
    assert len(responses.calls) == 0


def test_resume_of_a_folder_that_does_not_exist_is_refused_and_creates_nothing(settings, tmp_path):
    missing = tmp_path / "no_such_run"
    with pytest.raises(FetchError, match="no fetch log"):
        fetch(settings, resume_dir=missing)
    assert not missing.exists()


def write_old_log(run_dir, settings, body):
    """A log as an older version wrote it, ending with a page that is not a search result."""
    log = RunLog(run_dir / "fetch.jsonl")
    log.append(
        start_entry(
            fetch_id="20261028T150000Z_old00001",
            started_at="2026-10-28T15:00:00Z",
            source_url=SEARCH_URL,
            page_size=settings.source.page_size,
            query=settings.query.model_dump(mode="json"),
            query_fingerprint=settings.query.fingerprint(),
        )
    )
    log.append(
        page_entry(
            fetch_id="20261028T150000Z_old00001",
            query_fingerprint=settings.query.fingerprint(),
            page=1,
            fetched_at="2026-10-28T15:00:01Z",
            url=SEARCH_URL,
            request_body=FIRST_BODY,
            status=200,
            seconds=0.1,
            raw_body=body,
        )
    )


@pytest.mark.parametrize("body", [b'{"detail": "maintenance"}', b"[1, 2]", b"<html>proxy</html>"])
def test_resume_refuses_a_last_page_that_is_not_a_search_result(settings, tmp_path, body):
    run_dir = tmp_path / "old_run"
    write_old_log(run_dir, settings, body)
    with pytest.raises(FetchError, match="not a search result"):
        fetch(settings, resume_dir=run_dir)
    assert not read_log(run_dir / "fetch.jsonl").complete  # not marked complete with the rest never fetched


def test_resume_refuses_a_log_with_a_missing_page(settings, tmp_path):
    run_dir = tmp_path / "old_run"
    write_old_log(run_dir, settings, json.dumps(first_page()).encode())
    lines = (run_dir / "fetch.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (run_dir / "fetch.jsonl").write_text(lines[0], encoding="utf-8", newline="")
    RunLog(run_dir / "fetch.jsonl").append(
        page_entry(
            fetch_id="20261028T150000Z_old00001", query_fingerprint=settings.query.fingerprint(), page=2,
            fetched_at="2026-10-28T15:00:01Z", url=NEXT_URL, request_body=FIRST_BODY, status=200, seconds=0.1,
            raw_body=json.dumps(last_page()).encode(),
        )
    )
    with pytest.raises(FetchError, match="expected page 1"):
        fetch(settings, resume_dir=run_dir)


@responses.activate
def test_second_fetch_in_the_same_second_gets_its_own_folder(settings):
    responses.post(SEARCH_URL, json=answer("search_empty"))
    responses.post(SEARCH_URL, json=answer("search_empty"))
    first = fetch(settings)
    second = fetch(settings)
    assert second.fetch_id == first.fetch_id + "-2" and second.run_dir != first.run_dir


@responses.activate
def test_a_locked_run_is_not_written_by_a_second_fetch(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    run_dir = only_run_dir(settings)
    (run_dir / LOCK_NAME).write_text('{"pid": 1}', encoding="utf-8")  # another fetch is busy with it
    before = (run_dir / "fetch.jsonl").read_bytes()
    with pytest.raises(FetchError, match="another fetch is writing"):
        fetch(settings, resume_dir=run_dir)
    assert (run_dir / "fetch.jsonl").read_bytes() == before


@responses.activate
def test_status_that_cannot_be_written_does_not_stop_the_fetch(settings, monkeypatch, caplog):
    import cdse_stac.fetch as fetch_module

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fetch_module.os, "replace", refuse)
    responses.post(SEARCH_URL, json=answer("search_empty"))
    result = fetch(settings)
    assert read_log(result.log_path).complete
    assert "status.json could not be written" in caplog.text


@responses.activate
def test_resume_uses_the_page_size_of_the_run_not_of_the_settings(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    other = settings.model_copy(update={"source": settings.source.model_copy(update={"page_size": 100})})
    responses.reset()
    responses.post(NEXT_URL, json=last_page())
    result = run_fetch(other, client=quick_client(other), now=lambda: FIXED_NOW, resume_dir=only_run_dir(settings))
    assert result.pages == 2


@responses.activate
def test_refused_resume_leaves_the_log_untouched(settings):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    run_dir = only_run_dir(settings)
    with open(run_dir / "fetch.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"half a line')
    before = (run_dir / "fetch.jsonl").read_bytes()
    other = settings.model_copy(update={"query": settings.query.model_copy(update={"max_cloud_cover_pct": 20.0})})
    with pytest.raises(FetchError, match="different question"):
        fetch(other, resume_dir=run_dir)
    assert (run_dir / "fetch.jsonl").read_bytes() == before and not (run_dir / "fetch.jsonl.torn").exists()


@responses.activate
def test_status_is_not_written_through_a_symbolic_link(settings, tmp_path):
    responses.post(SEARCH_URL, json=first_page())
    responses.post(NEXT_URL, status=400, json={"detail": "try later"})
    with pytest.raises(FetchError):
        fetch(settings)
    run_dir = only_run_dir(settings)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    try:
        (run_dir / "status.json.tmp").symlink_to(outside)
    except OSError:
        pytest.skip("this system does not allow creating symbolic links")
    responses.reset()
    responses.post(NEXT_URL, json=last_page())
    fetch(settings, resume_dir=run_dir)
    assert outside.read_text(encoding="utf-8") == "keep me"


def test_lock_holder_text_is_cleaned(settings, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "fetch.jsonl").write_text("", encoding="utf-8")
    (run_dir / LOCK_NAME).write_text("\x1b[2J busy", encoding="utf-8")
    with pytest.raises(FetchError) as caught:
        fetch(settings, resume_dir=run_dir)
    assert "\x1b" not in str(caught.value)
