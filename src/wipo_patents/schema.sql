-- Normalized, query-friendly SQLite schema for the web app.
-- Weighting: a family with N technology fields contributes 1/N to each (fractional counting,
-- the WIPO standard). Same for applicant/inventor countries. Store the weight so the UI can
-- offer either fractional OR whole counting without re-extracting.

CREATE TABLE IF NOT EXISTS field (
  field_number   INTEGER PRIMARY KEY,
  field_name     TEXT NOT NULL,
  sector_number  INTEGER NOT NULL,
  sector_name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS family (
  family_id             TEXT PRIMARY KEY,
  earliest_filing_year  INTEGER NOT NULL,
  earliest_filing_date  INTEGER,            -- YYYYMMDD
  n_publications        INTEGER,
  n_fields              INTEGER             -- distinct tech fields, for the 1/N weight
);

CREATE TABLE IF NOT EXISTS family_field (
  family_id     TEXT NOT NULL REFERENCES family(family_id),
  field_number  INTEGER NOT NULL REFERENCES field(field_number),
  weight        REAL NOT NULL,
  PRIMARY KEY (family_id, field_number)
);

CREATE TABLE IF NOT EXISTS family_applicant_country (
  family_id     TEXT NOT NULL REFERENCES family(family_id),
  country_code  TEXT NOT NULL,
  weight        REAL NOT NULL,
  PRIMARY KEY (family_id, country_code)
);

CREATE TABLE IF NOT EXISTS family_inventor_country (
  family_id     TEXT NOT NULL REFERENCES family(family_id),
  country_code  TEXT NOT NULL,
  weight        REAL NOT NULL,
  PRIMARY KEY (family_id, country_code)
);

-- Pre-aggregated cube for instant UI: families per (year, field, applicant country).
-- Rebuilt at load time. The normalized tables above remain for ad-hoc/complex queries.
CREATE TABLE IF NOT EXISTS agg_year_field_applicant (
  earliest_filing_year INTEGER NOT NULL,
  field_number         INTEGER NOT NULL,
  applicant_country    TEXT NOT NULL,
  families_fractional  REAL NOT NULL,   -- sum of field_weight * applicant_weight
  families_whole       INTEGER NOT NULL,
  PRIMARY KEY (earliest_filing_year, field_number, applicant_country)
);

CREATE INDEX IF NOT EXISTS ix_family_year     ON family(earliest_filing_year);
CREATE INDEX IF NOT EXISTS ix_ff_field        ON family_field(field_number);
CREATE INDEX IF NOT EXISTS ix_fac_country     ON family_applicant_country(country_code);
CREATE INDEX IF NOT EXISTS ix_fic_country     ON family_inventor_country(country_code);
CREATE INDEX IF NOT EXISTS ix_agg_field       ON agg_year_field_applicant(field_number);
CREATE INDEX IF NOT EXISTS ix_agg_country     ON agg_year_field_applicant(applicant_country);

-- Provenance / reproducibility for the thesis methodology section.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
