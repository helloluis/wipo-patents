#!/usr/bin/env python3
"""
Extract the all-Europe viability dataset from BigQuery into local SQLite.

Universe (matches her Orbis definition, scoped to Europe):
  - DOCDB family (one invention per row; her 19.86M was priority-deduplicated too)
  - GRANTED (>=1 family member granted)
  - earliest priority/filing year 2000-2026
  - >=1 applicant (assignee) whose harmonized country is in Europe

Includes: family id, representative publication, dates, granted flag, company (assignee)
names + countries, WIPO 35-field (mapped from IPC via our concordance), inventor countries,
backward-citation count. NOT included (Orbis-only): BvD IDs, current/ultimate owner, financials.
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
  family_id TEXT PRIMARY KEY, rep_publication TEXT, filing_year INTEGER,
  filing_date INTEGER, priority_date INTEGER, granted INTEGER,
  n_publications INTEGER, n_bwd_citations INTEGER,
  primary_field_number INTEGER, primary_field_name TEXT, n_fields INTEGER);
CREATE TABLE IF NOT EXISTS family_assignee (
  family_id TEXT, name TEXT, country_code TEXT);
CREATE TABLE IF NOT EXISTS family_field (
  family_id TEXT, field_number INTEGER, weight REAL,
  PRIMARY KEY (family_id, field_number));
CREATE TABLE IF NOT EXISTS family_inventor_country (
  family_id TEXT, country_code TEXT, PRIMARY KEY (family_id, country_code));
CREATE INDEX IF NOT EXISTS ix_pf_year   ON patent_family(filing_year);
CREATE INDEX IF NOT EXISTS ix_pf_field  ON patent_family(primary_field_number);
CREATE INDEX IF NOT EXISTS ix_fa_name   ON family_assignee(name);
CREATE INDEX IF NOT EXISTS ix_fa_cc     ON family_assignee(country_code);
CREATE INDEX IF NOT EXISTS ix_ff_field  ON family_field(field_number);
"""


def build_query():
    eur = "(" + ",".join(f"'{x}'" for x in EUR) + ")"
    return f"""
WITH pub AS (
  SELECT family_id, publication_number, filing_date, priority_date, grant_date,
         ipc, assignee_harmonized, inventor_harmonized, ARRAY_LENGTH(citation) AS n_bwd
  FROM `patents-public-data.patents.publications`
  WHERE family_id IS NOT NULL AND family_id != '-1'
),
fam AS (
  SELECT family_id,
    MIN(IF(filing_date>0,filing_date,NULL))     AS filing_date,
    MIN(IF(priority_date>0,priority_date,NULL)) AS priority_date,
    LOGICAL_OR(grant_date>0)                    AS granted,
    COUNT(*)                                    AS n_pub,
    ANY_VALUE(publication_number)               AS rep_pub,
    SUM(n_bwd)                                  AS n_bwd,
    ARRAY_CONCAT_AGG(ipc)                       AS ipc_all,
    ARRAY_CONCAT_AGG(assignee_harmonized)       AS assignees,
    ARRAY_CONCAT_AGG(inventor_harmonized)       AS inventors
  FROM pub GROUP BY family_id
)
SELECT
  family_id, rep_pub, filing_date, priority_date, granted, n_pub, n_bwd,
  CAST(FLOOR(COALESCE(priority_date, filing_date)/10000) AS INT64) AS filing_year,
  ARRAY(SELECT DISTINCT code FROM UNNEST(ipc_all)) AS ipc_codes,
  ARRAY(SELECT AS STRUCT name, country_code FROM UNNEST(assignees)
        WHERE name IS NOT NULL AND name != '' GROUP BY name, country_code) AS assignees,
  ARRAY(SELECT DISTINCT country_code FROM UNNEST(inventors)
        WHERE country_code IS NOT NULL AND country_code != '') AS inventor_countries
FROM fam
WHERE COALESCE(priority_date, filing_date) IS NOT NULL
  AND CAST(FLOOR(COALESCE(priority_date, filing_date)/10000) AS INT64) BETWEEN 2000 AND 2026
  AND granted
  AND EXISTS(SELECT 1 FROM UNNEST(assignees) a WHERE a.country_code IN {eur})
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "europe.sqlite"))
    ap.add_argument("--max-gb", type=float, default=60.0)
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
    print("Running BigQuery extract (all-Europe, granted, 2000-2026)...")
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
        fields = sorted(conc.fields_for(ipc))
        primary = conc.field_for(ipc[0]) if ipc else None
        if primary is None and fields:
            primary = fields[0]
        cur.execute(
            "INSERT OR REPLACE INTO patent_family VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, row["rep_pub"], row["filing_year"], row["filing_date"], row["priority_date"],
             1 if row["granted"] else 0, row["n_pub"], row["n_bwd"],
             primary, conc.fields.get(primary, [None])[0] if primary else None, len(fields)))
        if fields:
            w = 1.0 / len(fields)
            cur.executemany("INSERT OR REPLACE INTO family_field VALUES (?,?,?)",
                            [(fid, f, w) for f in fields])
        for a in row["assignees"]:
            cur.execute("INSERT INTO family_assignee VALUES (?,?,?)",
                        (fid, a["name"], a["country_code"]))
        for cc in row["inventor_countries"]:
            cur.execute("INSERT OR REPLACE INTO family_inventor_country VALUES (?,?)", (fid, cc))
        n += 1
        if n % 100_000 == 0:
            con.commit()
            print(f"  {n:,} families")
    con.commit()
    print(f"Loaded {n:,} families -> {out}")
    # summary
    for label, q in [
        ("families", "SELECT COUNT(*) FROM patent_family"),
        ("distinct companies", "SELECT COUNT(DISTINCT name) FROM family_assignee"),
        ("assignee rows", "SELECT COUNT(*) FROM family_assignee")]:
        print(f"  {label:<20} {con.execute(q).fetchone()[0]:>12,}")
    print("Top fields:")
    for r in con.execute("""SELECT fd.field_name, ROUND(SUM(ff.weight)) n FROM family_field ff
                            JOIN field fd ON fd.field_number=ff.field_number
                            GROUP BY ff.field_number ORDER BY n DESC LIMIT 6"""):
        print(f"   {r[1]:>9,.0f}  {r[0]}")
    print("Top company countries:")
    for r in con.execute("""SELECT country_code, COUNT(DISTINCT family_id) n FROM family_assignee
                            WHERE country_code!='' GROUP BY country_code ORDER BY n DESC LIMIT 8"""):
        print(f"   {r[1]:>9,}  {r[0]}")
    con.close()


if __name__ == "__main__":
    main()
