-- Step 1: EXACT family counts per filing year -> this is what sizes your VPS mirror.
-- Scans only family_id + filing_date columns, so it's the cheapest meaningful query
-- (tens of GB, well inside the 1 TB/month free tier).
--
-- Params substituted by pipeline.py: @year_start, @year_end
WITH fam AS (
  SELECT
    family_id,
    MIN(IF(filing_date > 0, filing_date, NULL)) AS earliest_filing_date
  FROM `patents-public-data.patents.publications`
  WHERE family_id IS NOT NULL AND family_id != '-1'
  GROUP BY family_id
)
SELECT
  CAST(FLOOR(earliest_filing_date / 10000) AS INT64) AS filing_year,
  COUNT(*) AS families
FROM fam
WHERE earliest_filing_date IS NOT NULL
  AND CAST(FLOOR(earliest_filing_date / 10000) AS INT64) BETWEEN @year_start AND @year_end
GROUP BY filing_year
ORDER BY filing_year;
