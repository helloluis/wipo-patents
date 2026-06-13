#!/usr/bin/env python3
"""
Extract the all-Europe dataset from BigQuery into local SQLite (family-level, enriched).

Universe: DOCDB family, earliest priority/filing year 2000-2026, >=1 European applicant.
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

EUR = ['AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','HU','IE','IT','LV','LT','LU',
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
"""


def build_query():
    eur = "(" + ",".join(f"'{x}'" for x in EUR) + ")"
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
  AND EXISTS(SELECT 1 FROM UNNEST(assignees) a WHERE a.country_code IN {eur})
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

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(args.max_gb * 1024**3))
    print("Running BigQuery extract (all-Europe, granted, 2000-2026, enriched)...")
    rows = cli.query(sql, job_config=cfg).result(page_size=10_000)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    con.executescript(SCHEMA)
    con.executemany("INSERT OR REPLACE INTO field VALUES (?,?,?,?)",
                    [(fn, n, s, sn) for fn, (n, s, sn) in conc.fields.items()])
    cur = con.cursor()
    n = 0
    for row in rows:
        fid = row["family_id"]
        ipc = list(row["ipc_codes"])
        cpc = list(row["cpc_codes"])
        fields = sorted(conc.fields_for(ipc))
        primary = conc.field_for(ipc[0]) if ipc else None
        if primary is None and fields:
            primary = fields[0]
        cur.execute(
            "INSERT OR REPLACE INTO patent_family VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, row["rep_pub"], row["rep_app"],
             row["priority_date"], row["filing_date"], row["publication_date"], row["grant_date"],
             row["filing_year"], 1 if row["granted"] else 0, row["n_pub"], row["n_bwd"],
             primary, conc.fields.get(primary, [None])[0] if primary else None, len(fields),
             ipc[0] if ipc else None, "; ".join(ipc), "; ".join(cpc),
             "; ".join(row["member_pubs"] or [])))
        if fields:
            w = 1.0 / len(fields)
            cur.executemany("INSERT OR REPLACE INTO family_field VALUES (?,?,?)",
                            [(fid, f, w) for f in fields])
        for a in row["assignees"]:
            cur.execute("INSERT INTO family_assignee VALUES (?,?,?)", (fid, a["name"], a["country_code"]))
        for cc in row["inventor_countries"]:
            cur.execute("INSERT OR REPLACE INTO family_inventor_country VALUES (?,?)", (fid, cc))
        n += 1
        if n % 100_000 == 0:
            con.commit()
            print(f"  {n:,} families")
    con.commit()
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
        ("distinct companies", "SELECT COUNT(DISTINCT name) FROM family_assignee")]:
        print(f"  {label:<20} {con.execute(q).fetchone()[0]:>12,}")
    con.close()


if __name__ == "__main__":
    main()
