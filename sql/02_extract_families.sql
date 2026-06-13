-- Step 2: the main extract. One row per DOCDB patent family, with everything the
-- thesis needs EXCEPT the IPC->field mapping (done in Python via the concordance, so
-- we can swap the bootstrap concordance for PATSTAT TLS901 without re-running BigQuery).
--
-- Unit of analysis: DOCDB family (family_id). Earliest filing year across the family.
-- Dimensions returned: technology (raw IPC codes -> mapped later), applicant country,
-- inventor country. No titles/abstracts/full-text (keeps the mirror small; see README).
--
-- Params substituted by pipeline.py: @year_start, @year_end. A LIMIT is appended for smoke tests.
WITH pub AS (
  SELECT family_id, filing_date, ipc, assignee_harmonized, inventor_harmonized
  FROM `patents-public-data.patents.publications`
  WHERE family_id IS NOT NULL AND family_id != '-1'
),
fam AS (
  SELECT
    family_id,
    MIN(IF(filing_date > 0, filing_date, NULL)) AS earliest_filing_date,
    COUNT(*) AS n_publications
  FROM pub
  GROUP BY family_id
),
fam_scoped AS (
  SELECT
    family_id,
    earliest_filing_date,
    CAST(FLOOR(earliest_filing_date / 10000) AS INT64) AS earliest_filing_year,
    n_publications
  FROM fam
  WHERE earliest_filing_date IS NOT NULL
    AND CAST(FLOOR(earliest_filing_date / 10000) AS INT64) BETWEEN @year_start AND @year_end
),
fam_ipc AS (
  SELECT p.family_id, ARRAY_AGG(DISTINCT i.code IGNORE NULLS) AS ipc_codes
  FROM pub p, UNNEST(p.ipc) AS i
  GROUP BY p.family_id
),
fam_app AS (
  SELECT p.family_id, ARRAY_AGG(DISTINCT a.country_code IGNORE NULLS) AS applicant_countries
  FROM pub p, UNNEST(p.assignee_harmonized) AS a
  WHERE a.country_code IS NOT NULL AND a.country_code != ''
  GROUP BY p.family_id
),
fam_inv AS (
  SELECT p.family_id, ARRAY_AGG(DISTINCT v.country_code IGNORE NULLS) AS inventor_countries
  FROM pub p, UNNEST(p.inventor_harmonized) AS v
  WHERE v.country_code IS NOT NULL AND v.country_code != ''
  GROUP BY p.family_id
)
SELECT
  f.family_id,
  f.earliest_filing_year,
  f.earliest_filing_date,
  f.n_publications,
  COALESCE(fi.ipc_codes, []) AS ipc_codes,
  COALESCE(fa.applicant_countries, []) AS applicant_countries,
  COALESCE(fv.inventor_countries, []) AS inventor_countries
FROM fam_scoped f
LEFT JOIN fam_ipc fi USING (family_id)
LEFT JOIN fam_app fa USING (family_id)
LEFT JOIN fam_inv fv USING (family_id);
