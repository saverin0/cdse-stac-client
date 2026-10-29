# Quality and error report

Parcel `nw_bornheim_core`, a 3.96 km² box at 6.966, 50.761, 6.994, 50.779 (WGS84 west, south, east, north) near Bornheim, North Rhine-Westphalia. Collection `sentinel-2-l2a`, 2026-04-01 to 2026-09-30, cloud limit 30 percent. Retrieved on 2026-10-28 and again on 2026-10-29. The long tables (every run, every reject reason, every handled edge case, the evidence test by test) are in `docs/details.md`.

## Result in short

| Measure | Value |
|---|---|
| Scenes staged | 66 (33 on tile 31UGS, 33 on tile 32ULB) |
| Days with at least one scene | 27 |
| Satellites | Sentinel-2A 24 scenes, Sentinel-2B 20, Sentinel-2C 22 |
| Cloud cover of staged scenes | 0.0 to 29.79 percent, median 9.89 |
| Records refused | 0 in the real runs |
| Duplicates | 0 in the real runs |
| Validation checks | 14 of 14 passed (`docs/example_validation_output.txt`) |
| Rate-limit answers (HTTP 429) | 0 in all three real runs |
| Tests | 317 passed, none skipped, on the laptop as the client's own database role; 304 passed and 13 skipped in the test image |

Three fetches (two on 2026-10-28, one on 2026-10-29 from a fresh copy of this repository) returned the same 66 scenes with the same query fingerprint, and loading again left the table unchanged. The fresh copy took 31 s for migrate, fetch (5.1 s), load and validate. `docs/example_result.csv` is made by `sql/example_result.sql`, so it can be produced again after any run.

## Coverage per month

| Month | Scenes | Days with a scene | Lowest cloud, percent | Mean cloud, percent | Footprints covering the parcel only partly |
|---|---|---|---|---|---|
| 2026-04 | 16 | 6 | 0.2 | 13.7 | 0 |
| 2026-05 | 10 | 4 | 0.0 | 7.4 | 0 |
| 2026-06 | 12 | 5 | 0.2 | 6.6 | 0 |
| 2026-07 | 10 | 5 | 0.2 | 9.5 | 0 |
| 2026-08 | 10 | 5 | 0.0 | 5.4 | 0 |
| 2026-09 | 8 | 2 | 8.0 | 14.1 | 0 |

The largest gap is in September, where only 2 days passed the cloud limit, the last one on 2026-09-25. Scenes were published 3.5 to 9.6 hours after acquisition, with a median of 6.3 hours. That is the delay a monitoring job should expect.

## Rejected data

The real runs refused nothing. The server already applied the cloud filter, and all 66 records were complete and well formed. Every one of the 21 refusal reasons is exercised by tests, mostly on real records changed on purpose. A refused record is stored in `cdse_reject` with its reason, a detail and the record itself (as an escaped copy when the database could not hold the original). An unreadable page counts as one record seen and refused, so the counts still add up. A row the database still refuses becomes `database_refused` and the rest of its page is loaded. Optional values that cannot be read are left empty, named in the load log, and kept in `raw_item`.

## Edge cases found in the real source

1. **West bigger than east is accepted by the server.** By the STAC rules such a box crosses the date line and covers almost every longitude. The client refuses it before sending.
2. **The old collection name fails.** `SENTINEL-2` is refused with HTTP 400 `CollectionInQuerryDoesNotExist`; the id is `sentinel-2-l2a`.
3. **Paging moves to another host.** The documented address answers, but its next links point to `stac.dataspace.copernicus.eu`. Both hosts are on an allow list that may only hold Copernicus Data Space hosts; redirects are never followed, so a token cannot leak.
4. **The total is unknown in advance.** The server returns no `numberMatched`, so the client counts as it pages.
5. **The rate limit is strict.** About 10 fast requests in a row were answered with HTTP 429 during exploration. At 1 request per second the real runs saw none.
6. **Every acquisition appears twice.** The parcel lies where two grid tiles overlap (31UGS and 32ULB, either side of the 6° E zone line). They are different products with different ids, not duplicates.
7. **Two satellites on the same day.** On 6 days two satellites passed about 10 minutes apart, on relative orbits 8 and 108. Both are kept.
8. **The processing version changes in the season.** 05.12 up to 2026-09-15 (62 scenes), 05.13 from 2026-09-25 (4 scenes); `processing_baseline` makes this visible.
9. **A suspicious orbit direction.** One Sentinel-2B pass on 2026-05-28 is labelled `ascending` although its time and sun position fit a normal descending pass. Stored as received and flagged here.
10. **Footprints are simplified.** Every footprint is a four-corner outline, from 4,820 to 12,059 km²; by the source's own statistics, no-data covers 0 to 60.37 percent of a tile.
11. **The next link repeats the whole question.** The client relies on that and refuses a next link that drops or changes a query term, because it would walk another part of the catalogue.

## Edge cases handled and tested

- **Settings.** Numbers must be numbers (`true`, `"50.7"`, NaN and infinity are refused), times must be text with a time zone, every limit has bounds, a box larger than `max_box_deg` is refused, and a missing, broken or non-UTF-8 settings file ends in one line and exit code 2.
- **Server answers.** A 200 that is not a search result, 408, 425, most 5xx, dropped connections and broken compression are retried; redirects, other success codes, next links that change the question or point elsewhere, paging loops, oversized and endlessly slow answers are refused.
- **Scene records.** Values PostgreSQL cannot store, text longer than its column, odd ids, huge numbers, dates at the edge of the calendar, footprints with heights or across the date line, coordinates as text and a disagreeing item bbox are refused or left empty with a reason, never a crash.
- **Fetch log.** Every line is chained by sha256 to the line before; changed, removed, moved or re-sealed lines, gaps and repeats are reported; a line cut off by a crash is set aside on resume; non-UTF-8 answers are kept byte for byte; a lock stops two fetches on one run.
- **Load and database.** The newest fetch wins in any load order, overlapping parcels share scenes, a load cut off halfway is reported until loaded again, a changed log is refused unless it only grew by a resume, one load runs at a time, and CHECK constraints guard the ranges.
- **Security.** The token is checked before use and never shown; it only reaches Copernicus hosts over https; server text is cleaned of control characters; passwords are Docker secrets; every package is pinned by hash and every image and CI action by digest or commit.
- **Speed.** A reload resends only changed scenes, inserts go in batches of 100, logs are read one line at a time, and each page is parsed once.

## Evidence for the acceptance criteria

| Criterion | Evidence |
|---|---|
| Empty responses | a real empty answer in `tests/fixtures/search_empty.json`; `test_empty_result_is_one_page_and_no_error`, `test_correct_empty_result_passes_validation` |
| Coordinate ordering | `test_stac_bbox_is_longitude_first`, `test_box_must_name_its_sides`, `test_footprint_with_swapped_coordinates_is_refused`; edge case 1 above |
| Invalid bounding boxes | `test_invalid_boxes_are_refused` (swapped, zero width, beyond the pole, out of range, not a number, true, a number in quotes), `test_box_larger_than_max_box_deg_is_refused` |
| Credentials | `test_refused_token_is_reported_without_showing_it`, `test_token_goes_in_the_header_and_never_into_the_logs`, `test_token_with_a_line_end_is_refused_before_any_request`, `test_next_link_tricks_are_refused_before_any_request`, `test_redirect_is_not_followed` |
| Missing, malformed, duplicate records | `tests/test_records.py` (one test per refusal reason), `test_unreadable_page_keeps_the_counts_adding_up`, `test_same_id_with_different_content_is_kept_as_a_reject`, `test_a_row_the_database_still_refuses_does_not_stop_the_page` |
| Staging only and repeatable | `test_the_client_role_cannot_write_outside_iris_staging`, `test_migrate_refuses_a_shared_database_unless_told`, `test_second_load_of_the_same_log_changes_nothing`, `test_reloading_an_older_run_does_not_roll_back_newer_content`; validation checks 9 to 11 and 14 |
| Deterministic or auditable | fixed sort order (datetime, then id); query fingerprint; sha256 on every log line, chained, and on the whole log; three independent fetches gave the same 66 scenes |
| Pinned dependencies and code quality | both locks pin every package with hashes and are installed with `--require-hashes`; ruff 0.13.3 (E4, E7, E9, F) and bandit find nothing; `pip-audit` 2.10.1 finds no known vulnerabilities |

## Known gaps

- **The log chain is not a signature.** Someone who rewrites every line after a change can still forge a valid chain; the stored last seal and line count catch that only for a log loaded before. There is no signing key.
- **No image scan in CI.** `pip-audit` covers the Python packages and the base images are pinned by digest, but the operating system packages inside the images are not scanned.
- **Two CI steps are untested until CI runs on GitHub.** The database port bound to 127.0.0.1 and the step that creates the client's role were checked locally with Docker Compose, not on a GitHub runner.
- **No pixel-level checks.** Cloud cover and no-data are tile-level numbers; the scene classification and cloud probability masks are linked for every scene but not read.
- **Simplified footprints.** "Footprint touches the parcel" does not prove that the parcel has valid pixels.
- **No deletions.** A scene withdrawn from the catalogue later stays in staging; it can be found through `cdse_scene_fetch`.
- **One parcel per settings file.** Several parcels need several settings files and fetches; overlapping parcels then share their scenes correctly.
- **Not supported.** Parcel polygons, boxes crossing the date line, several collections in one query.
- **Not tested against the live server.** A real 429 burst or outage during a run; those paths are covered by mocked tests only.
