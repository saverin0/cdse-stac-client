# Copernicus Data Space STAC client for parcel monitoring

Technical assignment IRIS-SAT-CDSE (task 111).

The client searches the Copernicus Data Space Ecosystem STAC catalogue for Sentinel-2 Level-2A scenes over a parcel, writes every answer to an append-only log, and loads the scenes into a validated staging table in PostgreSQL with PostGIS, schema `iris_staging`. It writes nowhere else, and the database enforces that too, because the client connects with its own role that has rights on `iris_staging` only.

The example run covers one 2 x 2 km parcel near Bornheim (North Rhine-Westphalia) for April to September 2026, with a cloud limit of 30 percent. It found 66 scenes on 2 pages, all 14 validation checks pass, and loading the same data again changes nothing. From a fresh copy of this repository, `docker compose run --rm client run` took 31 s.

This README has the run and test steps, the data source, the schema mapping, the assumptions and the limitations. Every setting, command, column and fault-handling rule is in `docs/details.md`. The numbers, edge cases, evidence and known gaps are in `QUALITY_REPORT.md`.

## Run it with Docker

You need Docker Engine 25 or newer with Docker Compose v2 (tested with Engine 29.9 and Compose 5.6).

```bash
cp .env.example .env                  # then choose the two passwords in it
chmod 600 .env                        # Linux and macOS: only you can read it
docker compose build                  # both images, 2.5 to 4 min the first time; again after any code change
docker compose up -d db               # PostGIS 17 on 127.0.0.1:5434, plus an empty test database
docker compose run --rm client run    # migrate, fetch, load, validate; exit code 0 when every check passes
docker compose run --rm tests         # the test suite inside the test image, about 2.5 min
```

On Windows PowerShell, use `Copy-Item .env.example .env` for the first line and skip `chmod`.

`.env` holds two passwords. `POSTGRES_PASSWORD` is for the owner of the database and is used only by the database container. `CDSE_DB_PASSWORD` is for the client's own role `cdse_loader`, which may write `iris_staging` and nothing else. Any character works in both. They reach the containers as Docker secrets, not as environment variables, and the optional `CDSE_ACCESS_TOKEN` travels the same way.

Single steps:

```bash
docker compose run --rm client migrate
docker compose run --rm client fetch                       # prints the run folder
docker compose run --rm client load --run data/runs/nw_bornheim_core/<fetch_id>
docker compose run --rm client fetch --resume data/runs/nw_bornheim_core/<fetch_id>
docker compose run --rm client validate
docker compose run --rm client cleanup --keep 5            # lists old finished runs; add --yes to remove them
```

The settings file `config/bornheim.toml` is mounted read-only, so an edit counts at the next run without a rebuild. Copying fetch logs out of the volume, changing a password and the role script for an older database volume are described in `docs/details.md`.

## Run it without Docker

You need Python 3.11 or newer; the tests run on 3.11, 3.12 and 3.13. The database still comes from `docker compose up -d db`.

```bash
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock   # Windows: .venv\Scripts\pip install ...
export PYTHONPATH=src                                              # PowerShell: $env:PYTHONPATH = "src"
python -m cdse_stac run
pytest                                                             # 317 tests
```

The client finds the database from `CDSE_DB_HOST`, `CDSE_DB_PORT`, `CDSE_DB_NAME`, `CDSE_DB_USER` and `CDSE_DB_PASSWORD` in `.env`, or from one full `DATABASE_URL`. The tests use the client's address with the database named in `CDSE_TEST_DB_NAME`; that name must contain `test` as a whole word, because the tests empty its tables. Two tests also need `TEST_ADMIN_DATABASE_URL`, the owner of the test database; without it they skip with their reason. Use `127.0.0.1`, not `localhost`, which Windows tries over IPv6 first.

Dependencies: `requirements.txt` lists the 8 packages the program needs and `requirements-dev.txt` adds the 2 test tools, all with exact versions. `requirements.lock` (23 packages) and `requirements-dev.lock` (30 packages) pin every package, indirect ones included, with sha256 hashes, and every install uses `--require-hashes`.

## How it works

```text
config/bornheim.toml
   |
   v
fetch     POST /stac/search, follow the server's "next" links, paced and retried
   |      writes data/runs/<parcel_id>/<fetch_id>/fetch.jsonl, one chained, checked line per page
   v
load      reads only the log; checks it first; one page = one transaction; refused records keep their reason
   |      writes iris_staging.cdse_scene, cdse_scene_fetch, cdse_reject, cdse_fetch_run
   v
validate  sql/validation.sql (14 pass or fail checks) and sql/coverage.sql (coverage per parcel and month), read-only
```

- **Log first, data second.** The fetch step touches no database. Every answer goes to the log before the next request, and each line is pushed to the disk. A crash loses nothing; `fetch --resume` goes on from the last whole page, and `load` rebuilds the tables from the log alone.
- **A log that cannot change unnoticed.** Each line carries its own sha256 and that of the line before it. A changed, removed or moved line breaks the chain and is reported, and the load refuses a log that changed since it was loaded, unless it only grew by a resume.
- **Repeatable load.** A second load of the same log changes no row, not even `updated_at`. The newest fetch wins whatever order the logs are loaded in, so an older log cannot roll a scene back.
- **Staging only.** Every table and the Alembic version table live in `iris_staging`, owned by the client's role, which cannot create anything elsewhere. The load refuses to run if any table points elsewhere, and a database that holds an `iris_core` schema is refused unless `--allow-shared-database` is given.
- **Nothing lost silently.** Every refused record is stored in `cdse_reject` with its reason. For every run, `scenes_seen = inserted + updated + unchanged + duplicates + rejected`, and a check counts the stored rows independently.

## Data source, provenance and licence

| Item | Value |
|---|---|
| Endpoint (as documented in the assignment) | `https://catalogue.dataspace.copernicus.eu/stac/search` |
| Server behind it | its answers link on to `https://stac.dataspace.copernicus.eu/v1/`; both hosts are allowed, no other |
| Collection | `sentinel-2-l2a`, Sentinel-2 Level-2A surface reflectance (product type `S2MSI2A`), catalogue from 27 June 2015 |
| Access | search is anonymous; the asset links (`s3://eodata/...`) and the `Product` download need a Copernicus Data Space account, so no imagery is downloaded here |
| Retrieved | 2026-10-28, run ids `20261028T154005Z_952f5d4d` and `20261028T160636Z_952f5d4d`; a fresh run on 2026-10-29 returned the same 66 scenes |
| Acquisition dates | per scene in `acquired_at` (UTC); the example spans 2026-04-01 to 2026-09-25 |
| Resolution | `gsd_m` = 10 m; band files at 10, 20 and 60 m are listed in `raw_item -> 'assets'` |
| Masks | every scene lists the scene classification mask (`SCL_20m`, `SCL_60m`), cloud probability (`CLD_20m`, `CLD_60m`) and snow probability (`SNW_20m`, `SNW_60m`) in `raw_item -> 'assets'` |
| Uncertainty | see the quality report; in short, cloud cover is ESA's estimate for the whole tile, not for the parcel, and footprints are simplified four-corner outlines |
| Licence | free, full and open access under Regulation (EU) No 377/2014 and Delegated Regulation (EU) No 1159/2013 (Legal notice on the use of Copernicus Sentinel Data and Service Information) |
| Attribution | the staged rows are reshaped catalogue data, so cite them as "Contains modified Copernicus Sentinel data 2026" |

## Schema mapping

Table `iris_staging.cdse_scene` has one row per scene and is unique on `scene_id`. The main columns:

| Column | From the STAC item | Normalised as |
|---|---|---|
| `scene_id` | `id` | text, at most 255 characters, unique, the stable identifier |
| `collection`, `platform`, `product_type`, `processing_baseline` | `collection`, `properties.platform`, `product:type`, `processing:version` | text within its column length; the collection must equal the requested one |
| `mgrs_tile`, `relative_orbit`, `orbit_state` | `properties.grid:code`, `sat:relative_orbit`, `sat:orbit_state` | `MGRS-` prefix removed; whole number within 32 bits; text as received |
| `acquired_at`, `published_at`, `source_updated_at` | `properties.datetime` (or `start_datetime` when `datetime` is null), `published`, `updated` | timestamps with time zone, UTC |
| `cloud_cover_pct`, `snow_cover_pct`, `gsd_m`, `sun_elevation_deg` | `eo:cloud_cover`, `eo:snow_cover`, `gsd`, `view:sun_elevation` | percent 0 to 100, metres above 0, degrees -90 to 90; the table's CHECK constraints refuse anything else |
| `footprint`, `bbox_west` to `bbox_north` | `geometry` | PostGIS `MULTIPOLYGON`, SRID 4326 (WGS84 longitude, latitude), GiST index; the bbox columns are its bounds |
| `thumbnail_url`, `raw_item` | `assets.thumbnail.href`, the whole item | text; JSONB unchanged, so nothing is lost, the asset list included |
| `record_sha256` | made here | fingerprint of the item plus the version of the cleaning rules; decides whether a reload updates the row |
| `first_fetch_id`, `updated_by_fetch_id`, `last_seen_fetch_id`, with their times | made here | the earliest fetch, the fetch whose content is stored, and the latest fetch that returned the scene |

`cdse_scene_fetch` links every scene to every fetch that returned it, so two overlapping parcels each keep their scenes. `cdse_reject` holds every refused record with its reason, detail and raw record. `cdse_fetch_run` holds one row per log, with the question, the log's sha256, the counts and whether the load ran to the end. An optional value that cannot be read is left empty and named in the load log, and the raw value stays in `raw_item`. The column-by-column mapping and the 21 reject reasons are in `docs/details.md`.

## Tests and checks

`pytest` runs 317 tests, 283 without network or database and 34 against the PostGIS test database. All pass on the PC, as the client's role. In the test image, 304 pass and 13 skip with their reason (11 guards for repository files the image leaves out, and 2 that need the database owner). CI runs them on Python 3.11, 3.12 and 3.13 against a throwaway PostGIS, audits the dependencies with `pip-audit`, and builds both images.

The fixtures in `tests/fixtures/` are real answers captured from the catalogue on 2026-10-28, each with its request and sha256. The broken cases are copies of them, changed on purpose inside the tests. No synthetic data was needed.

The 14 checks in `sql/validation.sql`, all passing for the example run: at least one fetch is loaded; scene ids are unique; every scene was taken inside its date window; cloud cover is within 0 and the query limit; footprints are valid multipolygons in WGS84; every footprint touches the box of every parcel that fetched it; bbox columns match the footprint; every scene keeps its raw record with its id and assets; counts add up for every run; stored rows match the counts of every run; every load ran to the end; the newest fetch of each question is complete and undamaged; all stored times carry a time zone; no project table exists outside `iris_staging`.

Evidence from the real run: `docs/example_validation_output.txt` holds the checks and the coverage table, and `docs/example_result.csv` holds all 66 scenes, made by `sql/example_result.sql`.

## Assumptions

- A parcel is given as a bounding box in WGS84. "Covers the parcel" means the scene footprint intersects that box.
- The cloud limit applies to the scene-level `eo:cloud_cover` published with each scene.
- The documented `/stac/search` address is the source of record. Following its own next links to `stac.dataspace.copernicus.eu` counts as the same source.
- Search results are metadata only. Downloading or reading imagery is out of scope.

## Limitations

- No pixel-level check. Cloud cover and no-data are tile-level figures from the source, and the masks are linked, not read.
- Footprints are simplified four-corner outlines, so a parcel inside an outline can still fall on no-data pixels.
- A scene withdrawn from the catalogue later is not removed from staging. It can be found, because `cdse_scene_fetch` holds no link between it and the newest fetch of its parcel.
- Boxes crossing the date line, parcel polygons and several collections per query are not supported. A box larger than `max_box_deg` is refused unless that setting is raised.
- `cdse_fetch_run` keeps the counts of the latest load of each log; the log itself keeps every page and every failed attempt.
- The log chain catches accidents and careless edits. Someone who rewrites every line after a change could still forge it, which is why the load step also compares the stored line count and last line's sha256.
