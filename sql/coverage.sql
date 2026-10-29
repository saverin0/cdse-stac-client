-- How well the staged scenes cover each parcel, one row per parcel and month (UTC).
-- A scene counts for every parcel whose fetch returned it, so overlapping parcels each keep their scenes.
-- partial_cover counts scenes whose footprint does not cover the whole parcel box.
WITH parcel_scene AS (  -- each scene once per parcel, however many fetches of that parcel returned it
    SELECT r.parcel_id, f.scene_id, bool_and(ST_Covers(s.footprint, r.parcel_bbox)) AS covers
    FROM iris_staging.cdse_scene_fetch f
    JOIN iris_staging.cdse_scene s ON s.scene_id = f.scene_id
    JOIN iris_staging.cdse_fetch_run r ON r.fetch_id = f.fetch_id
    GROUP BY r.parcel_id, f.scene_id
)
SELECT p.parcel_id,
       to_char(date_trunc('month', s.acquired_at AT TIME ZONE 'UTC'), 'YYYY-MM') AS month,
       count(*) AS scenes,
       count(DISTINCT (s.acquired_at AT TIME ZONE 'UTC')::date) AS days_with_a_scene,
       string_agg(DISTINCT s.mgrs_tile, ' ') AS tiles,
       string_agg(DISTINCT s.platform, ' ') AS satellites,
       round(min(s.cloud_cover_pct)::numeric, 1) AS min_cloud_pct,
       round(avg(s.cloud_cover_pct)::numeric, 1) AS mean_cloud_pct,
       count(*) FILTER (WHERE NOT p.covers) AS partial_cover
FROM parcel_scene p
JOIN iris_staging.cdse_scene s ON s.scene_id = p.scene_id
GROUP BY p.parcel_id, 2
ORDER BY p.parcel_id, 2;
