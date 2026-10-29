# Details

Everything the README only summarises, in the README's order, then the long tables of the quality report.

## Run it with Docker, every step

You need Docker Engine 25 or newer with Docker Compose v2 (the health check's start interval needs Engine 25, and passwords given to the containers as secrets need a recent Compose; tested with Engine 29.9 and Compose 5.6).

```bash
cp .env.example .env                  # then choose the two passwords, see below
chmod 600 .env                        # Linux and macOS: only you can read it
docker compose build                  # both images; again after any code change
docker compose up -d db               # PostGIS 17 on 127.0.0.1:5434, plus an empty test database
docker compose run --rm client run    # migrate, fetch, load, validate; exit code 0 when every check passes
docker compose run --rm tests         # the tests, inside the test image, against the test database
```

On Windows PowerShell, use `Copy-Item .env.example .env` for the first line and skip `chmod`.

`.env` holds two passwords:
- `POSTGRES_PASSWORD` is for the owner of the database. Only the database container uses it.
- `CDSE_DB_PASSWORD` is for the client's own role, `cdse_loader`, which may write `iris_staging` and nothing else.

Any character works in both. Inside Docker they reach the containers as Docker secrets (files under `/run/secrets`), not as environment variables, so `docker inspect` and `docker compose config` do not show them. The optional `CDSE_ACCESS_TOKEN` travels the same way.

The first build of both images took 2.5 to 4 min, most of it downloading packages; later builds reuse the downloads. `docker compose run` reuses an image that is already built, so build again after any code change, or the old code runs. Settings are different: the `config` folder is mounted read-only into the container, so an edit to `config/bornheim.toml` counts at the next run without a rebuild.

Single steps:

```bash
docker compose run --rm client migrate
docker compose run --rm client fetch                       # prints the run folder
docker compose run --rm client load --run data/runs/nw_bornheim_core/<fetch_id>
docker compose run --rm client fetch --resume data/runs/nw_bornheim_core/<fetch_id>
docker compose run --rm client validate
docker compose run --rm client cleanup --keep 5            # lists old finished runs; add --yes to remove them
```

Fetch logs are kept in the named volume `runs`, in folders only the client's user may read. To copy them out to a folder `runs-export` next to this file, let Docker itself copy them (the same lines work in bash and in PowerShell):

```bash
docker compose run -d --name cdse-export --entrypoint sleep client 120
docker cp cdse-export:/app/data/runs ./runs-export
docker rm -f cdse-export
```

Remove everything with `docker compose down -v`. You also need that after changing `POSTGRES_PASSWORD`, because the database only reads it when the volume is first created. A new `CDSE_DB_PASSWORD` needs no reset; run the role script again (next paragraph).

The role `cdse_loader` is made by `docker/initdb/30_client_role.sh` when the database volume is first created. For a volume made before that script existed, run it once by hand. It also hands existing `iris_staging` tables over to the role, and it is safe to run again:

```bash
docker compose exec db sh /docker-entrypoint-initdb.d/30_client_role.sh
```

`fetch` always asks the server again. To rebuild the tables from a log without any network, use `load --run` on that run folder. `fetch --resume` on a finished log sends nothing.

In the test image, 304 tests passed and 13 skipped, in about 2.5 min (1.5 min of it the tests themselves). 11 of the skipped tests are guards for repository files that the image leaves out on purpose. The other 2 need the owner of the test database (see the settings for the tests below).

## Run it without Docker, every step

You need Python 3.11 or newer. The tests run on 3.11, 3.12 and 3.13 (the CI matrix). The image uses 3.13.15. The database still comes from `docker compose up -d db`.

```bash
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock   # Windows: .venv\Scripts\pip install ...
export PYTHONPATH=src                                              # PowerShell: $env:PYTHONPATH = "src"
python -m cdse_stac run
pytest                                                             # 317 tests
```

The client finds the database from these settings in `.env`:
- `CDSE_DB_HOST`, `CDSE_DB_PORT`, `CDSE_DB_NAME` and `CDSE_DB_PASSWORD`, plus `CDSE_DB_USER` (default `cdse_loader`). The address is built from these parts, so any character works in the password. The password can also come from a file named in `CDSE_DB_PASSWORD_FILE`.
- Or one full `DATABASE_URL`, which wins when it is set. Then special characters in the password must be written URL-encoded, for example `@` as `%40`. A plain `postgresql://` address gets the `psycopg` driver this project installs.

For the tests:
- `CDSE_TEST_DB_NAME=iris_test` makes the tests use the client's own address with the test database. A full `TEST_DATABASE_URL` works too. The name must contain `test` as a whole word, such as `iris_test`, because the tests empty its tables. `contest` or `iris_latest` are refused.
- `TEST_ADMIN_DATABASE_URL` is the owner of the test database. Only 2 tests need it: they create a schema named `iris_core` for a moment, which the client's role may not do. Without it, those 2 skip with their reason.
- Without a reachable test database, the database tests skip with the reason. With `REQUIRE_DB_TESTS=1`, as in CI, they fail instead, so a run that silently tested nothing is impossible.

On the PC, as the client's role, all 317 tests passed and none was skipped.

Only the `.env` in the folder you run from is read. Parent folders are not searched, and real environment variables win over `.env`.

In `.env`, use `127.0.0.1`, not `localhost`. On Windows, `localhost` is tried over IPv6 first and each connection waits out its 10 s time limit. With `localhost`, the test suite took 154 s instead of 6.8 s. A test guards this.

Dependencies come in four files:
- `requirements.txt` lists the 8 packages the program needs, with exact versions.
- `requirements-dev.txt` adds the 2 test tools, `pytest` and `responses`.
- `requirements.lock` pins everything the program needs, the indirect packages included: 24 lines, 23 packages, because `numpy` is pinned once for Python 3.11 and once for 3.12 and newer. Every package carries its sha256 hashes.
- `requirements-dev.lock` does the same with the test tools: 31 lines, 30 packages.

Both locks are valid for Python 3.11 and newer on Linux and Windows, and were made with `uv pip compile --generate-hashes`. Install them with `pip install --require-hashes`, so a package whose content differs from the pinned hash is refused. The runtime image installs only `requirements.lock`, so it holds no test tools. The test image, CI and the commands above install `requirements-dev.lock`. A test checks that the locks agree with the requirement files.

## Settings

All settings are in `config/bornheim.toml`. They are checked before any request is sent. Numbers must be written as numbers: `true`, `false` and numbers in quotes such as `"50.7"` are refused, because they were otherwise read silently as other values. Times must be written as text with a time zone, because a plain number would be read as seconds since 1970. Unknown or misspelt settings are refused.

| Setting | Example or default | Checked |
|---|---|---|
| `runs_dir` | `data/runs` | where the run folders go; `~` is expanded, `..` is refused |
| `query.parcel_id` | `nw_bornheim_core` | lowercase letters, digits and `_` only, at most 100 characters; it becomes a folder name, so names Windows reserves (`con`, `nul`, `com1` and so on) are refused |
| `query.collection` | `sentinel-2-l2a` | a plain STAC collection id (letters, digits, `.`, `_`, `-`), at most 100 characters; the old name `SENTINEL-2` is refused by the server |
| `query.bbox` west, south, east, north | `6.966, 50.761, 6.994, 50.779` | named sides; longitude within -180..180, latitude within -90..90, west < east, south < north; no larger than `max_box_deg` on either side |
| `query.start`, `query.end` | `2026-04-01T00:00:00Z` | text with a time zone, turned into UTC, start before end |
| `query.max_cloud_cover_pct` | `30` | 0 to 100; sent to the server as a CQL2 filter and checked again on every record |
| `source.search_url` | catalogue address below | https on the standard port, the host in `allowed_hosts`, no backslash or user part (`@`) |
| `source.allowed_hosts` | `catalogue.dataspace.copernicus.eu`, `stac.dataspace.copernicus.eu` | plain host names (no `https://`, port or path), turned into lowercase; each must end in `.dataspace.copernicus.eu`, because these are the only hosts the access token may ever reach |
| `source.page_size` | `50` | 1 to 1,000; one page is one database transaction and is held in memory a few times over |
| `source.requests_per_second` | `1.0` | pacing, 0.05 to 10; about 10 fast requests in a row were answered with 429 |
| `source.connect_timeout_s` | `5` | longest wait to connect, above 0 and at most 300 s |
| `source.read_timeout_s` | `30` | longest silence while an answer arrives, above 0 and at most 300 s |
| `source.total_timeout_s` | `120` | longest time for one whole answer, above 0 and at most 3,600 s, so a server sending one byte at a time cannot hold the run |
| `source.max_attempts` | `5` | tries per page for failures worth retrying, 1 to 10 |
| `source.max_page_bytes` | `50000000` | largest answer accepted, counted after decompression, 10,000 bytes to 1 GB; a real page of 50 scenes is about 3.5 MB |
| `source.max_pages` | `200` | safety net against endless next links, 1 to 10,000 |
| `source.max_box_deg` | `1.0` | largest box side in degrees, above 0 and at most 10; a parcel, not a country |

A settings file that cannot be read (missing, not UTF-8, broken TOML) ends with one line saying why and exit code 2. A byte-order mark from a Windows editor is fine.

Environment variables (see `.env.example`):
- `CDSE_DB_*` or `DATABASE_URL` say where to write; `CDSE_TEST_DB_NAME`, `TEST_DATABASE_URL` and `TEST_ADMIN_DATABASE_URL` are for the tests (see above).
- `CDSE_ACCESS_TOKEN` is optional, also from a file named in `CDSE_ACCESS_TOKEN_FILE`. When set, it is sent as a Bearer header, only to the allowed Copernicus hosts, and never logged. A token with spaces, line ends or characters outside plain ASCII is refused before any request, without being shown.
- `LOG_LEVEL` sets how much is logged: a level name in any case, or a number. An unknown name falls back to `INFO` with a warning. A level above `ERROR` becomes `ERROR`, because it would hide the error the program exits with.

## Commands and exit codes

| Command | What it does |
|---|---|
| `migrate` | creates or updates the `iris_staging` tables and prints the migration it is at; safe to run again |
| `fetch [--config FILE] [--resume RUN_DIR]` | asks the catalogue and writes every page to a new run folder, or goes on with an unfinished one; no database |
| `load --run RUN_DIR [--force]` | reads a fetch log and writes `iris_staging`; no network; `--force` loads a log that changed since it was loaded before |
| `validate [--out FILE]` | runs the SQL checks and the coverage summary in a read-only transaction |
| `run [--config FILE] [--out FILE]` | migrate, fetch, load, then validate |
| `cleanup [--keep N] [--yes]` | removes old run folders: keeps the newest N runs of each parcel (default 5), removes only runs that finished and that no fetch holds; without `--yes` it only lists them |

Before writing, `migrate`, `load` and `run` print the database name and check it. A database that holds an `iris_core` schema looks like a shared or production database, so they refuse it unless `--allow-shared-database` is given.

Exit codes: 0 when everything worked and every check passed; 2 when the settings cannot be used (a settings file that is missing, unreadable or not valid, or an unusable access token); 1 for every other failure, such as a fetch that gave up, a log that cannot be loaded, a database problem, a file that cannot be written, or a failed validation check. Each failure ends with one clear line, never a Python traceback. A database problem shows the database's own first line of explanation, never the password.

## Schema mapping, column by column

Table `iris_staging.cdse_scene` has one row per scene and is unique on `scene_id`.

| Column | From the STAC item | Normalised as |
|---|---|---|
| `id` | made here | random UUID text |
| `scene_id` | `id` | text, at most 255 characters, unique (`uq_cdse_scene_scene_id`), the stable identifier; an id with spaces around it or control characters is refused |
| `collection` | `collection` | must equal the requested collection |
| `platform` | `properties.platform` | text, at most 50 characters, for example `sentinel-2b` |
| `product_type` | `properties.product:type` | text, at most 50 characters |
| `processing_baseline` | `properties.processing:version` | text, at most 20 characters, for example `05.12` |
| `mgrs_tile` | `properties.grid:code` | `MGRS-` prefix removed, at most 10 characters, for example `32ULB` |
| `relative_orbit` | `properties.sat:relative_orbit` | whole number within the 32-bit range |
| `orbit_state` | `properties.sat:orbit_state` | text, at most 20 characters, kept as received |
| `acquired_at` | `properties.datetime`, or `properties.start_datetime` when `datetime` is null (STAC allows that) | timestamp with time zone, UTC; must carry a time zone |
| `published_at`, `source_updated_at` | `properties.published`, `updated` | UTC |
| `cloud_cover_pct` | `properties.eo:cloud_cover` | percent, 0 to 100; required |
| `snow_cover_pct` | `properties.eo:snow_cover` | percent, 0 to 100 |
| `gsd_m` | `properties.gsd` | metres per pixel, above 0 |
| `sun_elevation_deg` | `properties.view:sun_elevation` | degrees, -90 to 90 |
| `footprint` | `geometry` | PostGIS `MULTIPOLYGON`, SRID 4326 (WGS84 longitude, latitude), GiST index; heights in the coordinates are dropped (they stay in `raw_item`) |
| `bbox_west`, `bbox_south`, `bbox_east`, `bbox_north` | `geometry` | bounds of the footprint, so the two always agree; the item's own `bbox`, when given, must contain the footprint |
| `thumbnail_url` | `assets.thumbnail.href` | text |
| `raw_item` | the whole item | JSONB, unchanged, so nothing is lost; the asset list is in `raw_item -> 'assets'` (there is no separate `assets` column, which only held a second copy) |
| `record_sha256` | made here | sha256 of the item with sorted keys, plus the version of the cleaning rules; decides whether a reload updates the row, and a change to the rules updates rows cleaned by the old ones |
| `first_fetch_id`, `first_seen_at` | made here | the earliest fetch that returned the scene, and when it started |
| `updated_by_fetch_id`, `source_fetched_at` | made here | the fetch whose content is stored, and when it started |
| `last_seen_fetch_id`, `last_seen_at` | made here | the latest fetch that returned the scene at all, changed or not, and when it started |
| `created_at`, `updated_at` | the database | timestamps with time zone; `updated_at` moves only when the content changed |

An optional value that is present but cannot be read (wrong type, out of range, too large) is left empty, named in the load log, and kept as received in `raw_item`. The table itself also refuses values outside their ranges: CHECK constraints cover cloud and snow cover, ground sample distance, sun elevation and the bbox columns.

Table `iris_staging.cdse_scene_fetch` links every scene to every fetch that returned it, with the page and position. Two overlapping parcels therefore each keep their scenes, even when a scene was first loaded for the other parcel. Coverage and validation use these links.

Table `iris_staging.cdse_reject` has one row per refused record: run, page, position (-1 for an unreadable page), scene id when it can be stored, reason, detail and the raw record. Values the database cannot hold (NaN, NUL characters, broken characters) are kept as text in the raw record, so even those rejects are stored. For an unreadable page the raw record is empty (SQL NULL). Each fetch, page and position is stored once.

Table `iris_staging.cdse_fetch_run` has one row per fetch log: the question and its fingerprint, the parcel box, the absolute log path, the log's sha256, its number of lines and its last line's sha256, whether the fetch was complete (`fetch_status`), whether the load ran to the end (`load_status`, `loading` until it did), the timings and the counts. For every run, `scenes_seen = inserted + updated + unchanged + duplicates + rejected`. The `load` command also prints the result with the usual ETL keys `processed`, `inserted`, `updated`, `duplicates_removed`, `failed` and `execution_time`, plus `unchanged`, `pages` and `damaged_lines`.

All tables belong to the client's role `cdse_loader`, which can use PostGIS from the `public` schema but create nothing there or in any other schema.

Reasons a record is refused:
- From the cleaning step: `not_a_scene`, `unstorable_value` (NaN, Infinity, a NUL character or a broken character anywhere in the scene), `missing_id`, `bad_id`, `field_too_long`, `wrong_collection`, `missing_properties`, `bad_datetime`, `outside_date_window`, `missing_cloud_cover`, `bad_cloud_cover`, `above_cloud_limit`, `missing_geometry`, `bad_geometry`, `outside_query_bbox`, `missing_assets`, `bad_assets`, `conflicting_duplicate`, and `bad_record` (an error no rule foresaw, kept with its message instead of stopping the load).
- From the load step: `unreadable_page` (the whole page) and `database_refused` (a row the database still refused; see Fault handling).

`bad_geometry` covers, among others, coordinates written as text, a self-crossing outline, an item `bbox` that does not contain the footprint, and a footprint spanning more than 180 degrees of longitude, which means it crosses the date line without being split. A scene id seen twice in one fetch with the same content is counted as a duplicate and kept once. With different content, the first copy is kept and the second is refused as `conflicting_duplicate`, so the difference is on record.

## Fault handling

How the client talks to the server:
- Every request waits its turn in a token bucket (pacing) and has three time limits: to connect, for any silence while reading, and for the whole answer. An answer larger than `max_page_bytes` is refused.
- 408, 425, 429 and the 5xx answers except 501 and 505 are retried, as are time-outs, dropped connections, a broken compressed body, and a 200 answer that is not a readable search result (an HTML error page from a proxy, for example). The wait doubles each time, with up to half again added at random: 1, 2, 4, 8 s after server errors, and 10, 20, 40, 60 s after 429. A `Retry-After` header in seconds wins, up to 300 s; one given as a date is ignored. After `max_attempts` the run stops with a clear message.
- A failed certificate check is not retried. Any other 4xx answer fails at once, with the server's own message, cleaned of control characters and of the token. A 401 or 403 while a token is set says the request was refused, shows the server's words, and asks to check the token, without showing it.
- Redirects are never followed; a redirect ends the run with a message naming where it pointed.
- Next links are followed only to the allowed https Copernicus hosts on the standard port. The host is read the way requests will actually connect and must match. Links with a backslash or a user part (`@`) are refused outright, because they can make two URL parsers read different hosts. A relative link is read against the page it came from.
- A next link must ask the same question as the first request: only its paging token may differ. A link that drops or changes a query term, or asks to be merged with the request, is refused, because it would walk another part of the catalogue.
- A next page the server repeats, a page that holds only scenes from earlier pages, or more than `max_pages` pages stops the run instead of looping. When `numberReturned` disagrees with the scenes on a page, a warning is logged.
- Proxy, certificate and `.netrc` login settings from the environment are ignored, so nothing outside this program can change where requests go or what they carry.

The fetch log:
- Line kinds: `start` (the full question), `page` (one answer exactly as received, with the request, the useful response headers and the number of attempts), `attempt` (a failed try, with the reason and the server's words) and `end` (the fetch finished, with the number of pages and of 429 answers, counted over resumes too).
- An answer that is not UTF-8 text is kept as base64, so not a single byte is lost.
- A line cut short by a crash is moved to `fetch.jsonl.torn` when the fetch is resumed, so the resumed log is whole and the half line is still on record.
- While a fetch writes a run folder, it holds `fetch.lock`; a second fetch of the same folder is refused. `status.json` says `running`, `complete`, `failed` or `interrupted`, with the reason. `running` without `fetch.lock` means the fetch was cut off, for example by a power cut. A `status.json` that cannot be written (a full disk) is reported and does not stop the fetch; the log is the record.
- Two fetches started in the same second get their own folders (`-2`, `-3`). A resume of a folder that does not exist creates nothing. A resume refuses a log that belongs to another question, that is damaged, or whose last page is not a search result, instead of marking it complete.

The load:
- The log is checked first: every line's seal and chain, every line's fields, and the structure: one start line first, pages numbered 1, 2, 3 without gaps or repeats, at most one end line, which comes last and names the right number of pages, and every line from the same fetch. A log with a broken structure is refused with the reason. The log is read one line at a time, twice: once to check, once to load, so a large log never has to fit in memory.
- Only one load runs at a time on a database; a second one is refused and can simply be run again.
- A load cut off halfway leaves its run at `load_status = loading`, which the validation reports until the log is loaded again.
- An unreadable page counts as one record seen and refused, so the counts still add up.
- If the database still refuses a page, which the checks above should prevent, the page is loaded scene by scene. One bad row then cannot stop the rest, and each refused row is kept with reason `database_refused` and the database's words.

The database:
- Every connection has a 10 s connect limit, a 60 s statement limit, the time zone UTC and a fixed search path (`iris_staging`, then `public` for PostGIS). Options already given in the address are kept.
- Scenes are written in batches of 100 rows, so no statement comes near the time limit. For a scene that did not change, only the "seen" columns are updated, so a reload does not resend the large raw JSON.
- The validation runs in a read-only transaction, so its SQL can never change the data.

## Tests and checks

`pytest` runs 317 tests, 283 without network or database, and 34 against the PostGIS test database. They pass on the PC as the client's role with none skipped. In the test image, the 11 project-file guards skip with their reason, because the image leaves out the CI, compose and example files they check. The 2 tests that need the owner of the test database skip there too.

| Area | What is tested |
|---|---|
| Settings and boxes | swapped, zero-width, out-of-range and oversized boxes; boxes given as plain lists; true, false and quoted numbers; dates written as numbers or at the edge of the calendar; parcel ids and collections that do not fit; time limits without bounds; host names and ports; broken, missing and non-UTF-8 settings files and a byte-order mark; the request body equals the one the server accepted, and the example question keeps its fingerprint |
| Records | missing, malformed and duplicate records; same id with different content; NaN, Infinity, NUL and broken characters; huge numbers; text longer than its column, checked against the table's column sizes; ids with spaces; heights in footprints; footprints across the date line; coordinates as text; item bbox and assets; values out of range; swapped coordinates; dates outside the window; cloud above the limit even after the server filter |
| Log | lines cut short by a crash, changed lines, changed bodies, removed, swapped and re-sealed lines, gaps, repeats, a second start, lines after the end, missing fields, non-UTF-8 answers, a torn line moved aside, logs from the first version, symbolic links, deep nesting |
| Client | paging, empty results, 408, 429, other 5xx, 501, `Retry-After`, dropped connections, broken compression, certificate failures, giving up, 400 with the server message, token handling and odd tokens, foreign hosts, ports and other tricks, redirects, next links that change the question or ask to merge, repeated pages and paging loops, the page limit, size and total time limits, answers that are not a search result |
| Fetch | complete runs, failed runs, failed attempts in the log, resume with a torn line, 429 counts across a resume, a resume refused for a different question, a damaged log or a last page that is not a search result, the lock, two fetches in one second, a `status.json` that cannot be written |
| Command line | one clear line and the right exit code for missing settings, a broken settings file, a missing run folder, an unusable token and database errors, without the password; `LOG_LEVEL`; only the current folder's `.env`; `--help` loads no heavy library; `cleanup`; `migrate` prints its revision and refuses a shared database |
| Database | load, a second load changes nothing, a changed scene is updated, the newest fetch wins in any load order, overlapping parcels, unreadable pages and damaged lines, values the database cannot store, the scene-by-scene fallback, conflicting duplicates, batches beyond 100 scenes, a reload with new rules, a changed log against a resumed one, a second load at the same time, a load cut off halfway, an empty result, an old incomplete run, the independent row counts, CHECK constraints, a read-only validation, the shared-database guard, the client's role cannot write outside `iris_staging`, staging-only guard, validation checks |
| Project files | the database health check asks over TCP, the example settings use 127.0.0.1, both locks pin every package with hashes and agree with the requirement files, Docker and CI install with hashes, images are pinned by digest, CI actions are pinned to a commit and the job only reads, passwords reach the containers as secrets |

The validation checks in `sql/validation.sql`, all of which pass for the example run:
1. at least one fetch is loaded
2. scene ids are unique
3. every scene was taken inside its date window
4. cloud cover is within 0 and the query limit
5. footprints are valid multipolygons in WGS84 (EPSG 4326)
6. every footprint touches the box of every parcel that fetched it
7. bbox columns match the footprint
8. every scene keeps its raw record with its id and assets
9. counts add up for every run, so nothing was lost
10. stored rows match the counts of every run (counted from the tables, independently of the loader's own numbers)
11. every load ran to the end
12. the newest fetch of each question is complete and undamaged
13. all stored times carry a time zone
14. no project table exists outside iris_staging

A correct empty result passes too. An older incomplete fetch of a question no longer fails the checks once a newer fetch of the same question is complete.

CI runs the tests on Python 3.11, 3.12 and 3.13 against a throwaway PostGIS, as the client's own role and with `REQUIRE_DB_TESTS=1`. It also checks the dependencies for known vulnerabilities with `pip-audit` (none found) and builds both images. The job may only read the repository, has a time limit, and every action and image is pinned to an exact commit or digest.

The test fixtures in `tests/fixtures/` are real answers captured from the catalogue on 2026-10-28 with `scripts/capture_fixtures.py`. Each one keeps its request, capture time and sha256. No synthetic imagery or records were needed; the broken cases are copies of the real ones, changed on purpose inside the tests.

Evidence from the real run:
- `docs/example_validation_output.txt` holds the checks and the coverage table.
- `docs/example_result.csv` holds all 66 staged scenes with their SCL 20 m mask links.
- `QUALITY_REPORT.md` holds the numbers, edge cases and known gaps.

## Project layout

```text
config/bornheim.toml        example settings
src/cdse_stac/
  cli.py                    commands: migrate, fetch, load, validate, run, cleanup; one line and an exit code per failure
  config.py                 settings model and checks
  client.py                 HTTP client: pacing, time and size limits, retries, paging, host allow list
  fetch.py                  runner for the fetch step: client -> log, status.json, lock, resume
  runlog.py                 the append-only log: chained lines, structure check, streaming reads
  records.py                parser: one STAC item -> clean row or refusal with a reason
  load.py                   loader: log -> iris_staging, one transaction per page, newest fetch wins, result keys
  db.py                     table models, the database address and connection, the shared-database check
  validate.py               runs the SQL checks and the coverage summary, read-only
  jsonsafe.py               reads JSON the same way everywhere; finds values PostgreSQL cannot store
  cleanup.py                removes old finished run folders
  envfile.py                secrets from the environment or from Docker secret files
  errors.py                 the errors reported as one line
  logs.py                   log lines with source and run id
migrations/                 Alembic, limited to iris_staging (0001 tables, 0002 last seen, 0003 links, newest wins, checks)
sql/                        validation.sql and coverage.sql
docker/                     the test database, and the client's role (client_role.sql, initdb/30_client_role.sh)
tests/                      tests and real captured fixtures
scripts/capture_fixtures.py how the fixtures were captured
docs/                       example result and validation output
Dockerfile                  runtime image and test image
docker-compose.yml, .env.example
requirements.txt, requirements-dev.txt, requirements.lock, requirements-dev.lock (exact versions with hashes)
```

## Runs

| Run | Where | Pages | Scenes | Fetch time | Load result |
|---|---|---|---|---|---|
| `20261028T154005Z_952f5d4d` | laptop, earlier version | 2 (50 + 16) | 66 | 4.7 s | first load inserted 66 in 3.1 s; second load inserted 0, updated 0, unchanged 66 in 1.5 s |
| `20261028T160636Z_952f5d4d` | Docker container, earlier version, 26 minutes later | 2 (50 + 16) | 66 | 3.5 s | inserted 0, updated 0, unchanged 66 in 0.8 s |
| `20261029T033528Z_952f5d4d` | a fresh copy of this version, run exactly as the README says (`docker compose run --rm client run`) | 2 (50 + 16) | 66 | 5.1 s | inserted 66 in 1.8 s; 14 of 14 checks passed; the whole run, migration included, took 31 s |

All three fetches returned the same 66 scenes with the same query fingerprint. Repeating the search and loading it again left the staging table unchanged. `docs/example_result.csv` comes from the third run and is made by `sql/example_result.sql`, so it can be produced again after any run.

## Rejected data, reason by reason

The real runs refused nothing. The server already applied the cloud filter, and all 66 records were complete and well formed. Every refusal reason is still exercised by tests, mostly on real records captured from the catalogue and changed on purpose. Nothing is dropped without a trace: each refused record is stored in `cdse_reject` with its reason, a detail and the record itself (as an escaped copy when the database could not hold the original).

| Reason | Example in the tests |
|---|---|
| `not_a_scene` | a string instead of an object |
| `unstorable_value` | NaN, Infinity, a NUL character or a broken character (lone surrogate) anywhere in the scene, also when read from JSON text |
| `missing_id`, `bad_id` | the id removed, a number instead of text, only spaces; spaces around the id or a line end in it |
| `field_too_long` | an id over 255 characters; platform, product type, processing version, tile code or orbit direction longer than its column |
| `wrong_collection`, `missing_properties` | another collection; the properties removed |
| `bad_datetime`, `outside_date_window` | no time zone, unreadable text, a number, a date at the edge of the calendar; a date one year early |
| `missing_cloud_cover`, `bad_cloud_cover`, `above_cloud_limit` | cloud cover removed; 150, -5, the text "14.2", true, a number too large for a float; real scenes at 72.47 percent from an unfiltered page |
| `missing_geometry`, `bad_geometry` | the footprint removed; a point, a self-crossing "bow-tie" polygon, coordinates written as text, a footprint crossing the date line without being split, an item bbox that does not contain the footprint |
| `outside_query_bbox` | a real footprint with longitude and latitude swapped (the detail then says so); a footprint 20 degrees away |
| `missing_assets`, `bad_assets` | no assets; an asset that is not an object with a link |
| `conflicting_duplicate` | the same scene id twice in one fetch with different content (the first copy is kept) |
| `bad_record` | a rule failing on input no check foresaw (the test breaks one rule on purpose); the scene is kept as a reject with the error message, and the load goes on |
| `unreadable_page` | an HTML error page saved in the log; it counts as one record seen and refused, so the counts still add up |
| `database_refused` | a value that slips past the checks and is refused by the database; that page is then loaded scene by scene, so one row cannot stop the rest |
| duplicate (counted, not refused) | the same scene on two pages |

Optional values that are present but cannot be read (a sun elevation above 90 degrees, a relative orbit too large for its column, a platform that is a number) are left empty, the raw value stays in `raw_item`, and the load prints which fields were left empty.

## Edge cases handled and tested

**Settings**
- Numbers must be numbers. `true`, `false` and `"50.7"` in quotes are refused, as are NaN and infinity, for the box, the cloud limit and every source setting.
- Times must be text with a time zone. A plain number (which would be read as seconds since 1970) and dates at the edge of the calendar are refused with a message, not a crash.
- A box larger than `max_box_deg` (1 degree by default) is refused, so a typo cannot ask for a whole country.
- `parcel_id` must fit its column and work as a folder name on every system (`con`, `lpt1` and the like are refused). The collection id must be a plain id without spaces.
- Time limits and the request rate have upper and lower bounds; `page_size` is at most 1,000.
- A missing settings file, broken TOML (no value, a comma decimal, a key twice) or a file that is not UTF-8 ends in one line and exit code 2. A byte-order mark from a Windows editor is accepted.
- `LOG_LEVEL` accepts names in any case and numbers; an unknown name falls back to INFO with a note, and a level above ERROR becomes ERROR, so error messages are never hidden.
- `runs_dir` may use `~` and may not contain `..`.
- Only the `.env` of the current folder is read, never one in a parent folder.

**Server answers**
- A 200 answer that is not a search result (an HTML page, a list, no features, links that are not a list, JSON nested too deeply) is retried, and refused after the last try. It never enters the log as a page, so a resumed fetch cannot mistake it for the last page.
- 408, 425 and every 5xx except 501 and 505 are retried; other errors fail at once with the server's own words, cleaned of control characters.
- A redirect is refused, never followed. Other success codes such as 204 are refused.
- `Retry-After` given as a date, a negative number or infinity falls back to the client's own waiting rule; any wait is capped at 300 s.
- A broken compressed body and a dropped connection are retried; a failed certificate check is not.
- An answer larger than `max_page_bytes` (50 MB, counted after decompression) is refused. An answer that takes longer than `total_timeout_s` in total, even if it arrives one byte at a time, is cut off and retried.
- A byte-order mark is read the same way by fetch, resume and load.
- A next link that asks to merge, that repeats the first request, that changes the question, that points elsewhere or uses another port is refused. A relative next link is resolved against the page it came from.
- A paging loop is caught, also when the server sends a new token with the same scenes. `max_pages` (200 by default) is the last safety net.
- `numberReturned` that does not match the page is logged. Useful response headers are kept with each page.

**Scene records**
- Values PostgreSQL cannot store (NaN, Infinity, NUL, lone surrogates) are refused with the path of the bad value.
- Text longer than its column, ids with spaces around them, very large numbers and odd dates are refused or left empty with a reason, instead of crashing the load.
- A footprint with heights is stored flat; a footprint crossing the date line unsplit, coordinates written as text and an item bbox that disagrees with the footprint are refused.
- `datetime` may be empty when `start_datetime` is given, as STAC allows.
- The record fingerprint includes the version of the cleaning rules, so loading an old log again applies new rules.

**Fetch log**
- Each line carries its own sha256 and the sha256 of the line before it. A line that is changed, removed, moved or edited and sealed again breaks the chain and is reported (`test_deleted_line_breaks_the_chain`, `test_edited_and_resealed_line_breaks_the_chain`).
- The structure is checked: one start line first, pages 1, 2, 3 without gaps or repeats, at most one end line, last, naming the right number of pages. A log that breaks it is refused by load and by resume.
- A line cut off by a crash is moved to `fetch.jsonl.torn` before a resume, so the resumed log is whole and nothing disappears.
- An answer that is not UTF-8 text is kept byte for byte (base64).
- Every failed attempt is written to the log with the status and the server's words, so the 429 count survives a stop and a resume.
- A lock file stops two fetches from writing one run; two fetches in the same second get separate folders.
- `status.json` is written in one step and synced; if the disk is full, the fetch goes on and says so.
- `cleanup` removes old finished run folders and keeps the newest per parcel; it only lists them unless `--yes` is given.

**Load and database**
- The newest fetch wins, whatever order logs are loaded in: loading an older run again does not roll content back, and `first_fetch_id` is always the earliest fetch.
- A scene returned for two overlapping parcels counts for both (`cdse_scene_fetch` links every scene to every fetch that returned it).
- A load cut off halfway leaves its run row marked `loading`, and validation fails until the log is loaded again.
- Loading a page again replaces that page's links and rejects, so a reload with new rules leaves no stale rejects.
- A log that changed since it was loaded is refused, unless it only grew by a resume, or `--force` is given.
- Only one load runs at a time; a second one is refused with a clear message.
- CHECK constraints make the table itself refuse values outside their ranges.
- A correct empty result passes validation; an old incomplete fetch no longer fails validation once a newer fetch of the same question is complete.
- A separate check counts the stored rows of every run and compares them with the loader's own numbers.
- The client connects as its own role, `cdse_loader`, which owns `iris_staging` and may create nothing anywhere else (`test_the_client_role_cannot_write_outside_iris_staging`). Migrations and loads refuse a database that holds `iris_core` unless `--allow-shared-database` is given.
- Database errors show their reason (for example a refused password or an unreachable server), never the password. Any character works in the password, because the address is built from parts.

**Security**
- The access token is checked before use: spaces, line ends and characters outside plain ASCII are refused, and the token is never printed, logged or stored.
- The token only ever goes to Copernicus Data Space hosts over https on the standard port. Proxy, certificate and `.netrc` settings from the environment are ignored.
- Server text is cleaned of control characters before it reaches the screen, the log or `status.json`.
- Run folders are created for the owner only, and the log is never written through a symbolic link.
- Validation runs in a read-only transaction; the database connection uses a fixed search path and UTC.
- Passwords reach the containers as Docker secrets (files), not as environment variables. The client container has no Linux capabilities and cannot gain new ones.
- Every package is pinned with its sha256 hash and installed with `--require-hashes`. The runtime image holds no test tools. Both base images are pinned by content digest.
- In CI the actions are pinned to a full commit, the job may only read the repository, keeps no token on the runner, has a time limit, and the database port listens on 127.0.0.1 only. A dependency audit runs on every push.

**Speed**
- A reload sends only new and changed scenes in full; for the others only the "seen" columns move, so the large raw JSON is not sent or rewritten again.
- The asset list is stored once, inside `raw_item`, instead of twice. Each scene is written out as JSON once.
- Inserts go in batches of 100 rows (`test_more_scenes_than_one_statement_takes_load_in_batches`), far below the one-minute statement limit.
- Each page is parsed once during a fetch, and its scene count is kept in the log, so a resume does not parse every saved page again.
- Load and resume read the log one line at a time, so a large log never has to fit in memory. The page body is hashed once, not twice.
- `--help` and the light commands do not load the database, geometry and network libraries (`test_help_does_not_load_the_heavy_libraries`).
- Tests skip the disk sync per log line; one test checks that real runs do sync. The whole suite takes about 45 s on the laptop.
- `run` uses one database connection pool. CI keeps a cache of downloaded packages. The database health check asks every second while the server starts.

## Evidence for the acceptance criteria

| Criterion | Evidence |
|---|---|
| Empty responses | real empty answer in `tests/fixtures/search_empty.json`; `test_empty_result_is_one_page_and_no_error`, `test_empty_search_is_a_complete_run_with_no_scenes`, `test_correct_empty_result_passes_validation` |
| Coordinate ordering | `test_stac_bbox_is_longitude_first`, `test_box_must_name_its_sides`, the swapped cases in `test_invalid_boxes_are_refused`, `test_footprint_with_swapped_coordinates_is_refused`; edge case 1 above |
| Invalid bounding boxes | `test_invalid_boxes_are_refused` (swapped longitudes, swapped latitudes, zero width, beyond the pole, longitude out of range, not a number, true, a number in quotes), `test_box_larger_than_max_box_deg_is_refused` |
| Credentials | `test_refused_token_is_reported_without_showing_it`, `test_token_goes_in_the_header_and_never_into_the_logs`, `test_no_token_means_no_authorization_header`, `test_token_with_odd_characters_is_refused_without_showing_it`, `test_token_with_a_line_end_is_refused_before_any_request`, `test_next_link_to_a_foreign_host_is_refused`, `test_next_link_tricks_are_refused_before_any_request`, `test_redirect_is_not_followed` |
| Missing, malformed, duplicate records | the tests in `tests/test_records.py` (one for every refusal reason), plus `test_unreadable_page_keeps_the_counts_adding_up`, `test_scenes_the_database_cannot_store_become_rejects`, `test_same_id_with_different_content_is_kept_as_a_reject`, `test_a_row_the_database_still_refuses_does_not_stop_the_page` |
| Pagination | `test_follows_next_links_until_the_last_page`, `test_repeated_page_stops_the_run`, `test_paging_loop_with_new_tokens_is_caught`, `test_next_link_that_changes_the_question_is_refused`, `test_page_limit_comes_from_the_settings`; all three real runs paged 50 + 16 |
| Staging only | `test_all_tables_live_in_iris_staging`, `test_load_refuses_tables_outside_staging`, `test_migrations_name_no_schema_but_iris_staging`, `test_the_client_role_cannot_write_outside_iris_staging`, `test_migrate_refuses_a_shared_database_unless_told`; validation check "no project table exists outside iris_staging" |
| Repeatable, no data loss | `test_second_load_of_the_same_log_changes_nothing`, `test_new_fetch_with_the_same_content_only_moves_last_seen`, `test_reloading_an_older_run_does_not_roll_back_newer_content`, `test_load_cut_off_halfway_fails_validation_until_loaded_again`, `test_stored_rows_are_counted_independently_of_the_loader`; validation checks "counts add up for every run" and "stored rows match the counts of every run"; the raw item with all its assets and every refusal are stored |
| Pinned dependencies | `requirements.lock` holds 24 pinned lines for 23 packages (numpy is pinned once per Python version), every one with its sha256 hashes; `requirements-dev.lock` adds the test tools, 31 lines for 30 packages; `test_every_package_in_the_locks_is_pinned_with_hashes`, `test_locks_hold_every_direct_requirement_at_the_same_version`, `test_docker_and_ci_install_with_hashes`, `test_images_are_pinned_by_digest` |
| Python 3.11+ | the CI matrix runs every test on 3.11, 3.12 and 3.13; locally every test passed on Python 3.13.15, on the laptop and in the test image |
| Deterministic or auditable | fixed sort order (datetime, then id); query fingerprint; sha256 on every log line, chained to the line before, and on the whole log in `cdse_fetch_run`; three independent fetches gave the same 66 scenes |
| Documented source | `config/bornheim.toml` uses the documented address; provenance and licence in the README |
| Code quality | ruff 0.13.3 with the rules E4, E7, E9 and F finds nothing; `pip-audit` 2.10.1 finds no known vulnerabilities in the 30 locked packages |
