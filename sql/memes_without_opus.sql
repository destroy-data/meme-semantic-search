-- Memes (phash representatives only) that have no successful Claude Opus 5
-- description at effort level xhigh or higher:
--
--   sqlite3 memes.db < sql/memes_without_opus.sql
--
-- Opus 5 accepts effort low|medium|high|xhigh|max (no "none"), so "minimum
-- xhigh" means effort_level IN ('xhigh', 'max'). Rows described at a lower
-- effort, rows from other models, and failed attempts all still show up here.
SELECT m.hash,
       m.path
FROM memes m
WHERE m.perceptual_duplicate_of IS NULL
  AND NOT EXISTS (
        SELECT 1
        FROM descriptions d
        WHERE d.hash = m.hash
          AND d.status = 'described'
          AND d.model LIKE '%opus-5%'
          AND d.effort_level IN ('xhigh', 'max')
  )
ORDER BY m.hash;   -- same order describe.py picks rows in, so --limit 5 = first 5 here
