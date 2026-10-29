-- The query behind docs/example_result.csv: one row per staged scene, oldest first, with its mask and thumbnail links.
-- Run with: psql "$DATABASE_URL" --csv -f sql/example_result.sql > docs/example_result.csv
SELECT scene_id,
       to_char(acquired_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS acquired_at_utc,
       platform,
       mgrs_tile,
       relative_orbit,
       orbit_state,
       processing_baseline,
       cloud_cover_pct,
       snow_cover_pct,
       round(gsd_m::numeric, 1) AS gsd_m,
       round(sun_elevation_deg::numeric, 1) AS sun_elevation_deg,
       (SELECT count(*) FROM jsonb_object_keys(raw_item -> 'assets')) AS asset_count,
       raw_item -> 'assets' -> 'SCL_20m' ->> 'href' AS scl_mask_20m,  -- the scene classification mask, 20 m pixels
       thumbnail_url,
       first_fetch_id,
       updated_by_fetch_id,
       last_seen_fetch_id
FROM iris_staging.cdse_scene
ORDER BY acquired_at, scene_id;
