-- Materialized static aggregates so the landing page never computes them live.
-- These are the expensive unfiltered overviews (top companies, country distribution)
-- that otherwise scan the 6.5M-row assignee table on every cold request.
-- Rebuilt by extract_europe.py; safe to re-run on an existing DB.

DROP TABLE IF EXISTS company_stats;
CREATE TABLE company_stats AS
  SELECT name, country_code, COUNT(DISTINCT family_id) AS n_families
  FROM family_assignee WHERE name != '' GROUP BY name, country_code;
CREATE INDEX IF NOT EXISTS ix_cs_n ON company_stats(n_families);

DROP TABLE IF EXISTS country_stats;
CREATE TABLE country_stats AS
  SELECT country_code, COUNT(DISTINCT family_id) AS n_families
  FROM family_assignee WHERE country_code != '' GROUP BY country_code;
CREATE INDEX IF NOT EXISTS ix_cts_n ON country_stats(n_families);
