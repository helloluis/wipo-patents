#!/usr/bin/env python3
"""
Stream a patent dataset straight from BigQuery into Neon Postgres — NO local SQLite intermediate.

Each BigQuery batch (~tens of thousands of families) is processed in memory (WIPO-field concordance
+ IPC/CPC subclass mapping) and COPY'd immediately into the target schema, so the host never holds
more than one batch on disk/RAM. After the load, derived tables (family_country, company_stats,
country_stats) + indexes are built server-side and the schema is ANALYZE'd. Optionally --promote
points the read-only roles' search_path at the new schema (zero-downtime dataset swap).

Scope is parameterized because it isn't decided yet (US+EU, worldwide, year range, ...).

Prereqs:  NEON_DSN = the OWNER connection string (this script writes/creates tables).
          GOOGLE_CLOUD_PROJECT + ADC creds (BigQuery). google-cloud-bigquery-storage + psycopg.

  NEON_DSN='postgresql://neondb_owner:...@.../neondb?sslmode=require' \
  GOOGLE_CLOUD_PROJECT=gen-lang-client-0866257144 \
  python scripts/extract_to_neon.py --years 2000-2026 --origins worldwide --schema world2000

  Flags: --dry-run (validate BQ query + scan size, no load) · --limit N (cap output rows, for tests)
         --schema NAME (target; created fresh) · --promote (repoint app_ro/jannie_ro/wren_ro)
         --origins worldwide|us+eu|CC,CC,... · --no-require-assignee
"""
import argparse
import os
import sys
import time
from pathlib import Path

from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from wipo_patents.concordance import Concordance  # noqa: E402

import psycopg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONC = ROOT / "data" / "concordance" / "ipc_technology_bootstrap.csv"
READ_ROLES = ["app_ro", "jannie_ro", "wren_ro"]

EUR = ['AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','HU','IE','IT','LV','LT','LU',
       'MT','NL','PL','PT','RO','SK','SI','ES','SE','GB','CH','IS','LI','NO','AL','BA','ME','MK',
       'RS','XK','RU','UA','BY','MD','GE','AM','AZ','TR','AD','MC','SM','VA']

BATCH_COMMIT = 20  # commit every N BigQuery batches

# ---- Postgres schema (per target schema; same shape the app + sync_to_neon.py expect) ----------
def ddl(s):
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};
DROP TABLE IF EXISTS {s}.patent_family, {s}.family_assignee, {s}.family_field,
  {s}.family_inventor_country, {s}.family_class, {s}.family_country,
  {s}.company_stats, {s}.country_stats, {s}.field CASCADE;
CREATE TABLE {s}.field (
  field_number INTEGER PRIMARY KEY, field_name TEXT, sector_number INTEGER, sector_name TEXT);
CREATE TABLE {s}.patent_family (
  family_id TEXT PRIMARY KEY, rep_publication TEXT, rep_application TEXT,
  priority_date INTEGER, filing_date INTEGER, publication_date INTEGER, grant_date INTEGER,
  filing_year INTEGER, granted INTEGER, n_publications INTEGER, n_bwd_citations INTEGER,
  primary_field_number INTEGER, primary_field_name TEXT, n_fields INTEGER,
  ipc_main TEXT, ipc_codes TEXT, cpc_codes TEXT, member_publications TEXT);
CREATE TABLE {s}.family_assignee (family_id TEXT, name TEXT, country_code TEXT);
CREATE TABLE {s}.family_field (family_id TEXT, field_number INTEGER, weight DOUBLE PRECISION);
CREATE TABLE {s}.family_inventor_country (family_id TEXT, country_code TEXT);
CREATE TABLE {s}.family_class (family_id TEXT, scheme TEXT, code TEXT);
"""

# family_id-keyed data tables and their column order (matches the COPY row builders below).
TABLES = {
    "patent_family": ["family_id", "rep_publication", "rep_application", "priority_date",
        "filing_date", "publication_date", "grant_date", "filing_year", "granted",
        "n_publications", "n_bwd_citations", "primary_field_number", "primary_field_name",
        "n_fields", "ipc_main", "ipc_codes", "cpc_codes", "member_publications"],
    "family_field": ["family_id", "field_number", "weight"],
    "family_assignee": ["family_id", "name", "country_code"],
    "family_inventor_country": ["family_id", "country_code"],
    "family_class": ["family_id", "scheme", "code"],
}


def post_load(s):
    # indexes + derived tables + grants + ANALYZE, all after the bulk COPY.
    roles = ", ".join(READ_ROLES)
    return f"""
ALTER TABLE {s}.family_field ADD PRIMARY KEY (family_id, field_number);
ALTER TABLE {s}.family_inventor_country ADD PRIMARY KEY (family_id, country_code);
ALTER TABLE {s}.family_class ADD PRIMARY KEY (family_id, scheme, code);
CREATE INDEX ix_pf_year       ON {s}.patent_family(filing_year);
CREATE INDEX ix_pf_field      ON {s}.patent_family(primary_field_number);
CREATE INDEX ix_pf_field_year ON {s}.patent_family(primary_field_number, filing_year);
CREATE INDEX ix_fa_name       ON {s}.family_assignee(name);
CREATE INDEX ix_fa_cc         ON {s}.family_assignee(country_code);
CREATE INDEX ix_fa_fid        ON {s}.family_assignee(family_id);
CREATE INDEX ix_fa_cc_fid     ON {s}.family_assignee(country_code, family_id);
CREATE INDEX ix_ff_field      ON {s}.family_field(field_number);
CREATE INDEX ix_fc_code       ON {s}.family_class(scheme, code, family_id);
CREATE TABLE {s}.company_stats AS
  SELECT name, country_code, COUNT(DISTINCT family_id) AS n_families
  FROM {s}.family_assignee WHERE name <> '' GROUP BY name, country_code;
CREATE INDEX ix_cs_n ON {s}.company_stats(n_families);
CREATE TABLE {s}.country_stats AS
  SELECT country_code, COUNT(DISTINCT family_id) AS n_families
  FROM {s}.family_assignee WHERE country_code <> '' GROUP BY country_code;
CREATE INDEX ix_cts_n ON {s}.country_stats(n_families);
CREATE TABLE {s}.family_country AS
  SELECT a.country_code, f.granted, f.filing_year, a.family_id
  FROM (SELECT DISTINCT family_id, country_code FROM {s}.family_assignee WHERE country_code <> '') a
  JOIN {s}.patent_family f ON f.family_id = a.family_id;
CREATE INDEX ix_fcn     ON {s}.family_country(country_code, granted, filing_year, family_id);
CREATE INDEX ix_fcn_fid ON {s}.family_country(family_id);
CREATE TABLE {s}.meta_stats AS SELECT
  (SELECT COUNT(*) FROM {s}.patent_family) AS total_families,
  (SELECT COUNT(DISTINCT name) FROM {s}.family_assignee WHERE name <> '') AS total_companies,
  (SELECT MIN(filing_year) FROM {s}.patent_family) AS year_min,
  (SELECT MAX(filing_year) FROM {s}.patent_family) AS year_max;
CREATE TABLE {s}.class_index AS SELECT DISTINCT scheme, code FROM {s}.family_class;
CREATE INDEX ix_ci ON {s}.class_index(scheme, code);
CREATE TABLE {s}.year_stats AS
  SELECT filing_year, SUM(granted) AS granted, SUM(1 - granted) AS pending
  FROM {s}.patent_family GROUP BY filing_year;
CREATE TABLE {s}.field_stats AS
  SELECT f.primary_field_number AS field_number, fd.field_name, COUNT(*) AS n
  FROM {s}.patent_family f JOIN {s}.field fd ON fd.field_number = f.primary_field_number
  GROUP BY f.primary_field_number, fd.field_name;
GRANT USAGE ON SCHEMA {s} TO {roles};
GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {roles};
ANALYZE {s}.patent_family; ANALYZE {s}.family_assignee; ANALYZE {s}.family_field;
ANALYZE {s}.family_inventor_country; ANALYZE {s}.family_class; ANALYZE {s}.family_country;
ANALYZE {s}.company_stats; ANALYZE {s}.country_stats; ANALYZE {s}.field;
"""


def subclasses(codes):
    """Distinct 4-char IPC/CPC subclasses (e.g. 'F21V8/00' -> 'F21V') for the prefix index."""
    out = set()
    for c in codes:
        t = (c or "").replace(" ", "").upper()
        if len(t) >= 4 and t[0].isalpha() and t[1:3].isdigit() and t[3].isalpha():
            out.add(t[:4])
    return out


def build_query(y0, y1, origins, require_assignee, limit):
    if origins is None:  # worldwide
        origin_filter = ("AND EXISTS(SELECT 1 FROM UNNEST(assignees) a WHERE a.name IS NOT NULL "
                         "AND a.name != '')") if require_assignee else ""
    else:
        lst = "(" + ",".join(f"'{c}'" for c in origins) + ")"
        origin_filter = f"AND EXISTS(SELECT 1 FROM UNNEST(assignees) a WHERE a.country_code IN {lst})"
    lim = f"LIMIT {int(limit)}" if limit else ""
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
  AND CAST(FLOOR(COALESCE(priority_date, filing_date)/10000) AS INT64) BETWEEN {y0} AND {y1}
  {origin_filter}
{lim}
"""


def copy_batch(pg, schema, table, cols, rows):
    if not rows:
        return
    collist = ", ".join(cols)
    with pg.cursor() as cur:
        with cur.copy(f"COPY {schema}.{table} ({collist}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2000-2026")
    ap.add_argument("--origins", default="worldwide", help="worldwide | us+eu | CC,CC,...")
    ap.add_argument("--no-require-assignee", dest="require_assignee", action="store_false")
    ap.add_argument("--schema", default="staging")
    ap.add_argument("--limit", type=int, default=0, help="cap output rows (testing)")
    ap.add_argument("--max-gb", type=float, default=200.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--promote", action="store_true",
                    help="after load, repoint the read-only roles' search_path at --schema")
    args = ap.parse_args()

    y0, y1 = (int(x) for x in args.years.split("-"))
    o = args.origins.lower()
    origins = None if o == "worldwide" else (EUR + ["US"] if o in ("us+eu", "useu") else
                                             [c.strip().upper() for c in args.origins.split(",")])
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    cli = bigquery.Client(project=project) if project else bigquery.Client()
    sql = build_query(y0, y1, origins, args.require_assignee, args.limit)

    if args.dry_run:
        job = cli.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
        print(f"DRY RUN ok: scope years={y0}-{y1} origins={o} require_assignee={args.require_assignee}")
        print(f"  would scan ~{job.total_bytes_processed/1024**3:,.1f} GiB")
        return

    dsn = os.environ.get("NEON_DSN")
    if not dsn:
        sys.exit("error: NEON_DSN (owner connection string) required")

    conc = Concordance.load(CONC)
    from google.cloud import bigquery_storage
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(args.max_gb * 1024**3))
    print(f"Running extract → Neon schema '{args.schema}' (years {y0}-{y1}, origins {o})...", flush=True)
    result = cli.query(sql, job_config=cfg).result()

    pg = psycopg.connect(dsn, autocommit=False)
    s = args.schema
    with pg.cursor() as cur:
        cur.execute(ddl(s))
        cur.executemany(f"INSERT INTO {s}.field VALUES (%s,%s,%s,%s)",
                        [(fn, nm, sec, sn) for fn, (nm, sec, sn) in conc.fields.items()])
    pg.commit()

    bqs = bigquery_storage.BigQueryReadClient()
    n, t0, nb = 0, time.time(), 0
    for batch in result.to_arrow_iterable(bqstorage_client=bqs):
        c = batch.to_pydict()
        pf, ff, fa, fi, fc = [], [], [], [], []
        for i in range(batch.num_rows):
            fid = c["family_id"][i]
            ipc = c["ipc_codes"][i] or []
            cpc = c["cpc_codes"][i] or []
            fields = sorted(conc.fields_for(ipc))
            primary = conc.field_for(ipc[0]) if ipc else None
            if primary is None and fields:
                primary = fields[0]
            pf.append((fid, c["rep_pub"][i], c["rep_app"][i], c["priority_date"][i],
                c["filing_date"][i], c["publication_date"][i], c["grant_date"][i],
                c["filing_year"][i], 1 if c["granted"][i] else 0, c["n_pub"][i], c["n_bwd"][i],
                primary, conc.fields.get(primary, [None])[0] if primary else None, len(fields),
                ipc[0] if ipc else None, "; ".join(ipc), "; ".join(cpc),
                "; ".join(c["member_pubs"][i] or [])))
            if fields:
                w = 1.0 / len(fields)
                ff.extend((fid, f, w) for f in fields)
            for a in (c["assignees"][i] or []):
                fa.append((fid, a["name"], a["country_code"]))
            for cc in (c["inventor_countries"][i] or []):
                fi.append((fid, cc))
            for sc in subclasses(ipc):
                fc.append((fid, "ipc", sc))
            for sc in subclasses(cpc):
                fc.append((fid, "cpc", sc))
            n += 1
        copy_batch(pg, s, "patent_family", TABLES["patent_family"], pf)
        copy_batch(pg, s, "family_field", TABLES["family_field"], ff)
        copy_batch(pg, s, "family_assignee", TABLES["family_assignee"], fa)
        copy_batch(pg, s, "family_inventor_country", TABLES["family_inventor_country"], fi)
        copy_batch(pg, s, "family_class", TABLES["family_class"], fc)
        nb += 1
        if nb % BATCH_COMMIT == 0:
            pg.commit()
            print(f"  {n:,} families ({n/(time.time()-t0):,.0f}/s)", flush=True)
    pg.commit()
    print(f"Loaded {n:,} families. Building indexes + derived tables + ANALYZE...", flush=True)
    with pg.cursor() as cur:
        cur.execute(post_load(s))
    pg.commit()

    for label, qy in [("families", f"SELECT COUNT(*) FROM {s}.patent_family"),
                      ("assignee rows", f"SELECT COUNT(*) FROM {s}.family_assignee"),
                      ("class rows", f"SELECT COUNT(*) FROM {s}.family_class"),
                      ("family_country rows", f"SELECT COUNT(*) FROM {s}.family_country")]:
        print(f"  {label:<22} {pg.execute(qy).fetchone()[0]:>14,}")

    if args.promote:
        with pg.cursor() as cur:
            for role in READ_ROLES:
                cur.execute(f"ALTER ROLE {role} SET search_path = {s}, public")
        pg.commit()
        print(f"PROMOTED: {', '.join(READ_ROLES)} now default search_path = {s}, public "
              f"(reconnect to pick up).")
    pg.close()
    print(f"DONE in {(time.time()-t0)/60:.1f} min.")


if __name__ == "__main__":
    main()
