#!/usr/bin/env python3
"""
Extract the US+Europe dataset from BigQuery into local SQLite (family-level, enriched).

Universe: DOCDB family, earliest priority/filing year 2000-2026, >=1 applicant in the US or Europe.
Includes BOTH granted patents and pending applications (the `granted` flag distinguishes them) so
recent filing years aren't lost to grant lag.

Per family we capture everything available in Google Patents public data:
  identifiers (representative publication + application number, family members),
  dates (priority / filing / publication / grant), granted status, applicant/assignee
  names+countries, IPC + CPC + WIPO-field classifications, backward-citation count, family size.

NOT available in BigQuery (Orbis-only, added later): patent valuation metrics, BvD company IDs,
detailed legal status, forward citations (computable separately but heavier).
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from wipo_patents.concordance import Concordance  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONC = ROOT / "data" / "concordance" / "ipc_technology_bootstrap.csv"

# Applicant-origin universe: the United States + all of (broad) Europe.
ORIGINS = ['US',
       'AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','HU','IE','IT','LV','LT','LU',
       'MT','NL','PL','PT','RO','SK','SI','ES','SE','GB','CH','IS','LI','NO','AL','BA','ME','MK',
       'RS','XK','RU','UA','BY','MD','GE','AM','AZ','TR','AD','MC','SM','VA']

SCHEMA = """
CREATE TABLE IF NOT EXISTS field (
  field_number INTEGER PRIMARY KEY, field_name TEXT, sector_number INTEGER, sector_name TEXT);
CREATE TABLE IF NOT EXISTS patent_family (
  family_id TEXT PRIMARY KEY,
  rep_publication TEXT, rep_application TEXT,
  priority_date INTEGER, filing_date INTEGER, publication_date INTEGER, grant_date INTEGER,
  filing_year INTEGER, granted INTEGER,
  n_publications INTEGER, n_bwd_citations INTEGER,
  primary_field_number INTEGER, primary_field_name TEXT, n_fields INTEGER,
  ipc_main TEXT, ipc_codes TEXT, cpc_codes TEXT, member_publications TEXT);
CREATE TABLE IF NOT EXISTS family_assignee (family_id TEXT, name TEXT, country_code TEXT);
CREATE TABLE IF NOT EXISTS family_field (
  family_id TEXT, field_number INTEGER, weight REAL, PRIMARY KEY (family_id, field_number));
CREATE TABLE IF NOT EXISTS family_inventor_country (
  family_id TEXT, country_code TEXT, PRIMARY KEY (family_id, country_code));
-- Indexable classification index: one row per (family, scheme, 4-char subclass), e.g.
-- ('123','ipc','F21V'). Powers the prefix filters ("F21" -> code LIKE 'F21%', range-scannable).
CREATE TABLE IF NOT EXISTS family_class (
  family_id TEXT, scheme TEXT, code TEXT, PRIMARY KEY (family_id, scheme, code));
"""

# Created AFTER the bulk load — building indexes up front makes every insert maintain them
# and slows the load ~10x.
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_pf_year       ON patent_family(filing_year);
CREATE INDEX IF NOT EXISTS ix_pf_field      ON patent_family(primary_field_number);
CREATE INDEX IF NOT EXISTS ix_pf_field_year ON patent_family(primary_field_number, filing_year);
CREATE INDEX IF NOT EXISTS ix_fa_name       ON family_assignee(name);
CREATE INDEX IF NOT EXISTS ix_fa_cc         ON family_assignee(country_code);
CREATE INDEX IF NOT EXISTS ix_fa_fid        ON family_assignee(family_id);
CREATE INDEX IF NOT EXISTS ix_fa_cc_fid     ON family_assignee(country_code, family_id);
CREATE INDEX IF NOT EXISTS ix_ff_field      ON family_field(field_number);
-- (scheme, code, family_id) covers the prefix scan and the family dedup in one index.
CREATE INDEX IF NOT EXISTS ix_fc_code       ON family_class(scheme, code, family_id);
"""


def subclasses(codes):
    """Distinct 4-char IPC/CPC subclasses (e.g. 'F21V8/00' -> 'F21V') for the prefix index.
    Storing subclass level keeps the index small while supporting section/class/subclass
    prefix searches ('F', 'F21', 'F21V')."""
    out = set()
    for c in codes:
        s = (c or "").replace(" ", "").upper()
        if len(s) >= 4 and s[0].isalpha() and s[1:3].isdigit() and s[3].isalpha():
            out.add(s[:4])
    return out


def build_query():
    origins = "(" + ",".join(f"'{x}'" for x in ORIGINS) + ")"
    return f"""
WITH pub AS (
  SELECT family_id, publication_number, application_number,
         filing_date, priority_date, publication_date, grant_date,
         ipc, cpc, assignee_harmonized, inventor_harmonized, ARRAY_LENGTH(citation) AS n_bwd
  FROM `patents-public-data.patents.publications`
  WHERE family_id IS NOT NULL AND family_id != '-1'
),
fam AS (
  SELECT family_id,
    MIN(IF(filing_date>0,filing_date,NULL))       AS filing_date,
    MIN(IF(priority_date>0,priority_date,NULL))   AS priority_date,
    MIN(IF(publication_date>0,publication_date,NULL)) AS publication_date,
    MIN(IF(grant_date>0,grant_date,NULL))         AS grant_date,
    LOGICAL_OR(grant_date>0)                      AS granted,
    COUNT(*)                                      AS n_pub,
    ANY_VALUE(publication_number)                 AS rep_pub,
    ANY_VALUE(application_number)                 AS rep_app,
    SUM(n_bwd)                                    AS n_bwd,
    ARRAY_CONCAT_AGG(ipc)                         AS ipc_all,
    ARRAY_CONCAT_AGG(cpc)                         AS cpc_all,
    ARRAY_AGG(DISTINCT publication_number IGNORE NULLS) AS member_pubs,
    ARRAY_CONCAT_AGG(assignee_harmonized)         AS assignees,
    ARRAY_CONCAT_AGG(inventor_harmonized)         AS inventors
  FROM pub GROUP BY family_id
)
SELECT
  family_id, rep_pub, rep_app,
  filing_date, priority_date, publication_date, grant_date, granted, n_pub, n_bwd,
  CAST(FLOOR(COALESCE(priority_date, filing_date)/10000) AS INT64) AS filing_year,
  ARRAY(SELECT DISTINCT code FROM UNNEST(ipc_all)) AS ipc_codes,
  ARRAY(SELECT DISTINCT code FROM UNNEST(cpc_all)) AS cpc_codes,
  member_pubs,
  ARRAY(SELECT AS STRUCT name, country_code FROM UNNEST(assignees)
        WHERE name IS NOT NULL AND name != '' GROUP BY name, country_code) AS assignees,
  ARRAY(SELECT DISTINCT country_code FROM UNNEST(inventors)
        WHERE country_code IS NOT NULL AND country_code != '') AS inventor_countries
FROM fam
WHERE COALESCE(priority_date, filing_date) IS NOT NULL
  AND CAST(FLOOR(COALESCE(priority_date, filing_date)/10000) AS INT64) BETWEEN 2000 AND 2026
  AND EXISTS(SELECT 1 FROM UNNEST(assignees) a WHERE a.country_code IN {origins})
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "europe.sqlite"))
    ap.add_argument("--max-gb", type=float, default=120.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    cli = bigquery.Client(project=project) if project else bigquery.Client()
    conc = Concordance.load(CONC)
    sql = build_query()

    if args.dry_run:
        job = cli.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
        print(f"DRY RUN: would scan ~{job.total_bytes_processed/1024**3:,.1f} GiB")
        return

    from google.cloud import bigquery_storage  # fast gRPC/Arrow streaming (vs slow REST pagination)

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(args.max_gb * 1024**3))
    print("Running BigQuery extract (all-Europe 2000-2026, enriched, via Storage API)...")
    result = cli.query(sql, job_config=cfg).result()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    con.execute("PRAGMA journal_mode=OFF")     # bulk build from scratch — re-run on failure
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(SCHEMA)
    con.executemany("INSERT OR REPLACE INTO field VALUES (?,?,?,?)",
                    [(fn, nm, s, sn) for fn, (nm, s, sn) in conc.fields.items()])
    cur = con.cursor()
    n = 0
    bqs = bigquery_storage.BigQueryReadClient()
    for batch in result.to_arrow_iterable(bqstorage_client=bqs):
        c = batch.to_pydict()
        rows_pf, rows_ff, rows_fa, rows_fi, rows_fc = [], [], [], [], []
        for i in range(batch.num_rows):
            fid = c["family_id"][i]
            ipc = c["ipc_codes"][i] or []
            cpc = c["cpc_codes"][i] or []
            fields = sorted(conc.fields_for(ipc))
            primary = conc.field_for(ipc[0]) if ipc else None
            if primary is None and fields:
                primary = fields[0]
            rows_pf.append((
                fid, c["rep_pub"][i], c["rep_app"][i],
                c["priority_date"][i], c["filing_date"][i], c["publication_date"][i], c["grant_date"][i],
                c["filing_year"][i], 1 if c["granted"][i] else 0, c["n_pub"][i], c["n_bwd"][i],
                primary, conc.fields.get(primary, [None])[0] if primary else None, len(fields),
                ipc[0] if ipc else None, "; ".join(ipc), "; ".join(cpc),
                "; ".join(c["member_pubs"][i] or [])))
            if fields:
                w = 1.0 / len(fields)
                rows_ff.extend((fid, f, w) for f in fields)
            for a in (c["assignees"][i] or []):
                rows_fa.append((fid, a["name"], a["country_code"]))
            for cc in (c["inventor_countries"][i] or []):
                rows_fi.append((fid, cc))
            for sc in subclasses(ipc):
                rows_fc.append((fid, "ipc", sc))
            for sc in subclasses(cpc):
                rows_fc.append((fid, "cpc", sc))
            n += 1
        cur.executemany("INSERT OR REPLACE INTO patent_family VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_pf)
        cur.executemany("INSERT OR REPLACE INTO family_field VALUES (?,?,?)", rows_ff)
        cur.executemany("INSERT INTO family_assignee VALUES (?,?,?)", rows_fa)
        cur.executemany("INSERT OR REPLACE INTO family_inventor_country VALUES (?,?)", rows_fi)
        cur.executemany("INSERT OR REPLACE INTO family_class VALUES (?,?,?)", rows_fc)
        con.commit()
        print(f"  {n:,} families")
    print(f"Loaded {n:,} families -> {out}")
    print("Creating indexes...")
    con.executescript(INDEXES)
    con.commit()
    print("Building materialized stats tables (company_stats, country_stats)...")
    con.executescript((ROOT / "sql" / "build_stats.sql").read_text(encoding="utf-8"))
    con.commit()
    for label, q in [
        ("families", "SELECT COUNT(*) FROM patent_family"),
        ("granted", "SELECT COUNT(*) FROM patent_family WHERE granted=1"),
        ("applications", "SELECT COUNT(*) FROM patent_family WHERE granted=0"),
        ("with CPC", "SELECT COUNT(*) FROM patent_family WHERE cpc_codes != ''"),
        ("class index rows", "SELECT COUNT(*) FROM family_class"),
        ("distinct IPC subclasses", "SELECT COUNT(DISTINCT code) FROM family_class WHERE scheme='ipc'"),
        ("distinct companies", "SELECT COUNT(DISTINCT name) FROM family_assignee")]:
        print(f"  {label:<20} {con.execute(q).fetchone()[0]:>12,}")
    con.close()


if __name__ == "__main__":
    main()
