-- tools/backfill_jobs.sql
-- Idempotent backfill for unified `jobs` table using legacy archive_jobs data
-- 1) Backfill master archive_size_bytes + start/end/duration
WITH master_stats AS (
  SELECT
    a.id AS archive_id,
    COALESCE(SUM(aj.archive_size_bytes), 0) AS total_size,
    a.start_time,
    a.end_time,
    CASE WHEN a.start_time IS NOT NULL AND a.end_time IS NOT NULL
         THEN GREATEST(FLOOR(EXTRACT(EPOCH FROM (a.end_time - a.start_time)))::int, 1)
         ELSE NULL END AS duration_secs
  FROM archive_jobs a
  LEFT JOIN archive_jobs aj ON aj.archive_id = a.id
  GROUP BY a.id, a.start_time, a.end_time
)
UPDATE jobs j
SET
  archive_size_bytes = COALESCE(j.archive_size_bytes, ms.total_size),
  start_time = COALESCE(j.start_time, ms.start_time),
  end_time = COALESCE(j.end_time, ms.end_time),
  duration_seconds = COALESCE(j.duration_seconds, ms.duration_secs)
FROM master_stats ms
WHERE j.job_type = 'archive_master' AND j.legacy_archive_id = ms.archive_id
  AND (j.archive_size_bytes IS NULL OR j.start_time IS NULL OR j.end_time IS NULL OR j.duration_seconds IS NULL);

-- 2) Backfill per-stack archive duration_seconds from archive_jobs timing
UPDATE jobs j
SET duration_seconds = COALESCE(j.duration_seconds,
   GREATEST(FLOOR(EXTRACT(EPOCH FROM (a.end_time - a.start_time)))::int, 1))
FROM archive_jobs a
WHERE j.job_type = 'archive_stack' AND j.legacy_archive_id = a.id
  AND a.start_time IS NOT NULL AND a.end_time IS NOT NULL
  AND (j.duration_seconds IS NULL OR j.duration_seconds = 0);

-- 3) Ensure jobs.start_time/end_time present for archive_stack from archive_jobs
UPDATE jobs j
SET start_time = COALESCE(j.start_time, a.start_time),
    end_time = COALESCE(j.end_time, a.end_time)
FROM archive_jobs a
WHERE j.job_type = 'archive_stack' AND j.legacy_archive_id = a.id
  AND (j.start_time IS NULL OR j.end_time IS NULL);

-- End of backfill
