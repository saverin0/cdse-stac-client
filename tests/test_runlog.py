import hashlib
import json

import pytest

from cdse_stac import runlog
from cdse_stac.runlog import (
    RunLog,
    body_bytes,
    end_entry,
    page_entry,
    read_log,
    seal,
    start_entry,
    summarize_log,
    unseal,
)

FINGERPRINT = "a" * 64


def make_page(number, raw=b'{"type":"FeatureCollection","features":[]}'):
    return page_entry(
        fetch_id="f-1",
        query_fingerprint=FINGERPRINT,
        page=number,
        fetched_at="2026-10-28T13:00:00Z",
        url="https://catalogue.dataspace.copernicus.eu/stac/search",
        request_body={"limit": 50},
        status=200,
        seconds=0.5,
        raw_body=raw,
    )


def make_start():
    return start_entry(
        fetch_id="f-1",
        started_at="2026-10-28T13:00:00Z",
        source_url="https://catalogue.dataspace.copernicus.eu/stac/search",
        page_size=50,
        query={"parcel_id": "p"},
        query_fingerprint=FINGERPRINT,
    )


def write_log(path, pages=2, end=True):
    log = RunLog(path)
    log.append(make_start())
    for number in range(1, pages + 1):
        log.append(make_page(number))
    if end:
        log.append(end_entry(fetch_id="f-1", pages=pages, finished_at="2026-10-28T13:00:05Z"))
    return path


def lines_of(path):
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def test_pages_written_are_read_back_unchanged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(make_page(1))
    log.append(make_page(2))
    contents = read_log(tmp_path / "run.jsonl")
    assert [page["page"] for page in contents.pages] == [1, 2]
    assert contents.pages[0] == make_page(1)
    assert contents.damaged == []
    assert not contents.complete


def test_end_line_marks_the_log_complete(tmp_path):
    path = write_log(tmp_path / "run.jsonl", pages=1)
    assert read_log(path).complete
    assert summarize_log(path).complete


def test_body_fingerprint_matches_the_raw_bytes():
    raw = '{"type":"FeatureCollection","features":[],"note":"Köln"}'.encode("utf-8")
    entry = make_page(1, raw)
    assert entry["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert body_bytes(entry) == raw


def test_answer_that_is_not_utf8_keeps_every_byte(tmp_path):
    raw = b'{"features": [], "note": "\xff\xfe broken"}'
    path = tmp_path / "run.jsonl"
    RunLog(path).append(make_page(1, raw))
    entry = read_log(path).pages[0]
    assert entry["body_encoding"] == "base64" and body_bytes(entry) == raw


def test_line_cut_short_by_a_crash_is_skipped(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append(make_page(1))
    whole_line = seal(make_page(2))
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(whole_line[: len(whole_line) // 2])  # the crash: half a line, no line end
    contents = read_log(path)
    assert [page["page"] for page in contents.pages] == [1]
    assert len(contents.damaged) == 1
    assert contents.damaged[0][0] == 2


def test_changed_line_fails_its_fingerprint(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append(make_page(1))
    text = path.read_text(encoding="utf-8").replace('"status":200', '"status":201')
    path.write_text(text, encoding="utf-8", newline="\n")
    contents = read_log(path)
    assert contents.pages == []
    assert "fingerprint" in contents.damaged[0][1]


def test_changed_body_fails_its_body_fingerprint(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append(make_page(1))
    text = path.read_text(encoding="utf-8").replace('\\"features\\":[]', '\\"features\\":[1]')
    path.write_text(text, encoding="utf-8", newline="\n")
    assert "does not match its fingerprint" in read_log(path).damaged[0][1]


def test_deleted_line_breaks_the_chain(tmp_path):
    path = write_log(tmp_path / "run.jsonl", pages=3)
    lines = lines_of(path)
    path.write_text("".join(lines[:2] + lines[3:]), encoding="utf-8", newline="")  # page 2 removed
    summary = summarize_log(path)
    assert any("chain is broken" in problem for _, problem in summary.damaged)
    assert not summary.complete


def test_swapped_lines_break_the_chain(tmp_path):
    path = write_log(tmp_path / "run.jsonl", pages=2)
    lines = lines_of(path)
    path.write_text("".join([lines[0], lines[2], lines[1], lines[3]]), encoding="utf-8", newline="")
    assert any("chain is broken" in problem for _, problem in summarize_log(path).damaged)


def test_edited_and_resealed_line_breaks_the_chain(tmp_path):
    path = write_log(tmp_path / "run.jsonl", pages=2)
    lines = lines_of(path)
    entry, _, prev = unseal(lines[1].rstrip("\n"))
    entry["status"] = 999
    lines[1] = seal(entry, prev) + "\n"  # a valid seal for the edited line
    path.write_text("".join(lines), encoding="utf-8", newline="")
    damaged = summarize_log(path).damaged
    assert [number for number, problem in damaged if "chain is broken" in problem] == [3]


@pytest.mark.parametrize(
    "build, problem",
    [
        (lambda log: [log.append(make_page(1)), log.append(make_page(3))], "expected page 2"),
        (lambda log: [log.append(make_page(1)), log.append(make_page(1))], "expected page 2"),
        (lambda log: [log.append(make_page(1)), log.append(make_start())], "second start line"),
        (
            lambda log: [
                log.append(make_page(1)),
                log.append(end_entry(fetch_id="f-1", pages=1, finished_at="2026-10-28T13:00:05Z")),
                log.append(make_page(2)),
            ],
            "after the end line",
        ),
        (
            lambda log: [log.append(make_page(1)), log.append(end_entry(fetch_id="f-1", pages=2, finished_at="2026-10-28T13:00:05Z"))],
            "names 2 pages",
        ),
        (lambda log: [log.append({**make_page(1), "fetch_id": "f-2"})], "another fetch"),
    ],
    ids=["gap", "repeat", "second-start", "after-end", "end-count", "other-fetch"],
)
def test_broken_structure_is_reported(tmp_path, build, problem):
    path = tmp_path / "run.jsonl"
    log = RunLog(path)
    log.append(make_start())
    build(log)
    summary = summarize_log(path)
    assert any(problem in text for text in summary.problems), summary.problems
    assert not summary.complete


def test_entry_with_a_missing_field_is_reported_not_loaded(tmp_path):
    path = tmp_path / "run.jsonl"
    log = RunLog(path)
    log.append(make_start())
    broken = make_page(1)
    del broken["fetched_at"]
    log.append(broken)
    summary = summarize_log(path)
    assert summary.page_numbers == [] and "fetched_at" in summary.damaged[0][1]


def test_resuming_after_a_crash_sets_the_torn_line_aside(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append(make_page(1))
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"half a line')
    log = RunLog(path)  # a new run opens the same log and goes on
    log.append(make_page(2))
    contents = read_log(path)
    assert [page["page"] for page in contents.pages] == [1, 2]
    assert contents.damaged == []
    assert log.removed_tail == len('{"half a line')
    assert (tmp_path / "run.jsonl.torn").read_text(encoding="utf-8") == '{"half a line\n'


def test_log_written_by_the_first_version_is_still_read(tmp_path):
    entry = {**make_page(1), "v": 1}
    for key in ("attempts", "headers", "scenes", "body_encoding"):
        del entry[key]
    fingerprint = hashlib.sha256(runlog._canonical(entry).encode("utf-8")).hexdigest()
    (tmp_path / "old.jsonl").write_text(runlog._canonical({**entry, "line_sha256": fingerprint}) + "\n", encoding="utf-8")
    assert read_log(tmp_path / "old.jsonl").pages[0]["page"] == 1


def test_summary_keeps_only_counts_and_reads_the_last_seal(tmp_path):
    path = write_log(tmp_path / "run.jsonl", pages=2)
    summary = summarize_log(path)
    assert (summary.page_numbers, summary.lines, summary.last_page) == ([1, 2], 4, None)
    assert summary.last_seal == json.loads(lines_of(path)[-1])["line_sha256"]
    assert summary.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_log_uses_linux_line_ends(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append(make_page(1))
    assert b"\r\n" not in path.read_bytes()


def test_each_line_is_synced_to_disk_in_real_runs(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(runlog, "DURABLE", True)
    monkeypatch.setattr(runlog.os, "fsync", lambda descriptor: synced.append(descriptor))
    RunLog(tmp_path / "run.jsonl").append(make_page(1))
    assert synced


def test_log_is_not_written_through_a_symbolic_link(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "run.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this system does not allow creating symbolic links")
    with pytest.raises(OSError, match="symbolic link"):
        RunLog(link)


def test_unseal_refuses_a_line_without_fingerprint():
    with pytest.raises(ValueError):
        unseal(json.dumps({"kind": "page"}))


def test_unseal_refuses_a_deeply_nested_line():
    with pytest.raises(ValueError, match="nested too deeply"):
        unseal("[" * 100_000)


@pytest.mark.parametrize("body", [5, None, [1], {"a": 1}], ids=["number", "null", "list", "object"])
def test_page_line_whose_body_is_not_text_is_reported_not_crashed(tmp_path, body):
    path = write_log(tmp_path / "run.jsonl", pages=1)
    lines = lines_of(path)
    entry = json.loads(lines[1])
    entry["body"] = body  # the seal leaves the body out, so this edit keeps a valid seal
    lines[1] = runlog._canonical(entry) + "\n"
    path.write_text("".join(lines), encoding="utf-8", newline="")
    summary = summarize_log(path)
    assert (2, "the saved answer cannot be read") in summary.damaged and not summary.complete


def test_append_does_not_parse_the_line_again(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the line was parsed again")

    log = RunLog(tmp_path / "run.jsonl")
    monkeypatch.setattr(runlog.json, "loads", refuse)
    log.append(make_page(1))
    log.append(make_page(2))
    monkeypatch.undo()
    assert summarize_log(tmp_path / "run.jsonl").damaged == []  # the second line chains to the first


def test_fetch_id_with_control_characters_is_a_damaged_line(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append({**make_start(), "fetch_id": "\x1b[2Jx"})
    assert [number for number, _ in summarize_log(path).damaged] == [1]
