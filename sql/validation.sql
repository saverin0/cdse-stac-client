-- Validation checks for iris_staging. One row per check: its name, whether it passed, and the numbers behind it.
-- Run with psql "$DATABASE_URL" -f sql/validation.sql, or with python -m cdse_stac validate (read-only).
-- Details list only the runs that fail, so the report stays short however many runs are loaded.
WITH scene AS (  -- each scene with the question of the fetch whose content is stored
    SELECT s.*, r.query
    FROM iris_staging.cdse_scene s
    JOIN iris_staging.cdse_fetch_run r ON r.fetch_id = s.updated_by_fetch_id
),
returned AS (  -- each scene with the parcel box of every fetch that returned it
    SELECT s.scene_id, s.footprint, r.fetch_id, r.parcel_bbox
    FROM iris_staging.cdse_scene_fetch f
    JOIN iris_staging.cdse_scene s ON s.scene_id = f.scene_id
    JOIN iris_staging.cdse_fetch_run r ON r.fetch_id = f.fetch_id
),
run_rows AS (  -- what is really stored for each run, counted independently of the loader's own numbers
    SELECT r.fetch_id, r.inserted + r.updated + r.unchanged AS kept, r.rejected,
           (SELECT count(*) FROM iris_staging.cdse_scene_fetch f WHERE f.fetch_id = r.fetch_id) AS links,
           (SELECT count(*) FROM iris_staging.cdse_reject j WHERE j.fetch_id = r.fetch_id) AS rejects
    FROM iris_staging.cdse_fetch_run r
    WHERE r.log_lines IS NOT NULL AND r.load_status = 'loaded'  -- runs loaded by this version of the loader
),
newest AS (  -- the newest fetch of each question; older fetches of the same question are history
    SELECT DISTINCT ON (query_fingerprint) *
    FROM iris_staging.cdse_fetch_run
    ORDER BY query_fingerprint, started_at DESC NULLS LAST, fetch_id DESC
)
SELECT 'at least one fetch is loaded' AS check_name,
       count(*) > 0 AS passed,
       format('%s fetches, %s scenes', count(*), (SELECT count(*) FROM iris_staging.cdse_scene)) AS detail
FROM iris_staging.cdse_fetch_run

UNION ALL
SELECT 'scene ids are unique',
       count(*) = count(DISTINCT scene_id),
       format('%s rows, %s distinct scene ids', count(*), count(DISTINCT scene_id))
FROM iris_staging.cdse_scene

UNION ALL
SELECT 'every scene was taken inside its date window',
       coalesce(bool_and(acquired_at BETWEEN (query ->> 'start')::timestamptz AND (query ->> 'end')::timestamptz), true),
       format('first %s, last %s', to_char(min(acquired_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
              to_char(max(acquired_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
FROM scene

UNION ALL
SELECT 'cloud cover is within 0 and the query limit',
       coalesce(bool_and(cloud_cover_pct BETWEEN 0 AND (query ->> 'max_cloud_cover_pct')::float), true),
       format('highest %s percent', max(cloud_cover_pct))
FROM scene

UNION ALL
SELECT 'footprints are valid multipolygons in WGS84 (EPSG 4326)',
       coalesce(bool_and(ST_IsValid(footprint) AND ST_SRID(footprint) = 4326
                         AND GeometryType(footprint) = 'MULTIPOLYGON'), true),
       format('%s footprints checked', count(*))
FROM scene

UNION ALL
SELECT 'every footprint touches the box of every parcel that fetched it',
       coalesce(bool_and(ST_Intersects(footprint, parcel_bbox)), true),
       format('%s of %s scene and fetch pairs touch', count(*) FILTER (WHERE ST_Intersects(footprint, parcel_bbox)), count(*))
FROM returned

UNION ALL
SELECT 'bbox columns match the footprint',
       coalesce(bool_and(abs(bbox_west - ST_XMin(footprint)) < 1e-9 AND abs(bbox_south - ST_YMin(footprint)) < 1e-9
                     AND abs(bbox_east - ST_XMax(footprint)) < 1e-9 AND abs(bbox_north - ST_YMax(footprint)) < 1e-9), true),
       format('%s rows checked', count(*))
FROM iris_staging.cdse_scene

UNION ALL
SELECT 'every scene keeps its raw record with its id and assets',
       coalesce(bool_and(jsonb_typeof(raw_item -> 'assets') = 'object' AND raw_item -> 'assets' <> '{}'::jsonb
                         AND raw_item ->> 'id' = scene_id), true),
       format('%s scenes checked', count(*))
FROM iris_staging.cdse_scene

UNION ALL
SELECT 'counts add up for every run, so nothing was lost',
       coalesce(bool_and(scenes_seen = inserted + updated + unchanged + duplicates + rejected), true),
       coalesce(left(string_agg(format('%s seen %s, but inserted %s + updated %s + unchanged %s + duplicates %s + rejected %s',
                                       fetch_id, scenes_seen, inserted, updated, unchanged, duplicates, rejected), '; ')
                     FILTER (WHERE scenes_seen <> inserted + updated + unchanged + duplicates + rejected), 400),
                format('%s runs checked', count(*)))
FROM iris_staging.cdse_fetch_run

UNION ALL
SELECT 'stored rows match the counts of every run',
       coalesce(bool_and(kept = links AND rejected = rejects), true),
       coalesce(left(string_agg(format('%s kept %s but %s links, rejected %s but %s reject rows',
                                       fetch_id, kept, links, rejected, rejects), '; ')
                     FILTER (WHERE kept <> links OR rejected <> rejects), 400),
                format('%s runs checked', count(*)))
FROM run_rows

UNION ALL
SELECT 'every load ran to the end',
       coalesce(bool_and(load_status = 'loaded'), true),
       coalesce(left(string_agg(format('%s was cut off while loading; load it again', fetch_id), '; ')
                     FILTER (WHERE load_status <> 'loaded'), 400),
                format('%s runs checked', count(*)))
FROM iris_staging.cdse_fetch_run

UNION ALL
SELECT 'the newest fetch of each question is complete and undamaged',
       coalesce(bool_and(fetch_status = 'complete' AND damaged_lines = 0), true),
       coalesce(left(string_agg(format('%s %s, %s damaged lines', fetch_id, fetch_status, damaged_lines), '; ')
                     FILTER (WHERE fetch_status <> 'complete' OR damaged_lines <> 0), 400),
                format('%s questions checked', count(*)))
FROM newest

UNION ALL
SELECT 'all stored times carry a time zone',
       coalesce(bool_and(data_type = 'timestamp with time zone'), true),
       format('%s time columns', count(*))
FROM information_schema.columns
WHERE table_schema = 'iris_staging' AND data_type LIKE 'timestamp%'

UNION ALL
SELECT 'no project table exists outside iris_staging',
       count(*) = 0,
       format('%s cdse tables found elsewhere', count(*))
FROM pg_class c  -- the catalog shows every schema; information_schema hides those the client may not use
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname LIKE 'cdse%' AND c.relkind IN ('r', 'p', 'v', 'm') AND n.nspname <> 'iris_staging';
