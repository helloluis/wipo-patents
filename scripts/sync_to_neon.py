#!/usr/bin/env python3
"""
One-way backup/mirror of the local SQLite serving DB -> a Neon Postgres database.

The VPS SQLite file (data/europe.sqlite) is authoritative. Neon is a read-only mirror;
nothing ever flows back. Two modes:

  --full         Drop + recreate the Postgres schema and bulk-COPY every row. Slow, run once
                 (or whenever you want a clean re-baseline). Idempotent: safe to re-run.

  --incremental  (default) Diff family_ids: find families that exist locally but not yet in
                 Neon and push ONLY those (plus their child rows). Small, cheap, append-only.
                 Robust to a local full rebuild of the SQLite file, because it diffs by
                 family_id rather than by rowid — re-extracting the same universe pushes nothing.

The derived stats tables (company_stats, country_stats) are NOT transferred over the wire;
they are rebuilt server-side in Postgres after the data tables land.

Connection string comes from $NEON_DSN (preferred) or --dsn.

  NEON_DSN='postgresql://.../neondb?sslmode=require' .venv/bin/python scripts/sync_to_neon.py --full
  NEON_DSN='...' .venv/bin/python scripts/sync_to_neon.py            # incremental
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.environ.get("WIPO_DB", str(ROOT / "data" / "europe.sqlite"))

# Source tables that hold real data and are mirrored verbatim. Order matters: parents first.
# (granted is 0/1; dates are yyyymmdd integers — kept as INTEGER to mirror SQLite exactly.)
DATA_TABLES = {
    "field": ["field_number", "field_name", "sector_number", "sector_name"],
    "patent_family": [
        "family_id", "rep_publication", "rep_application",
        "priority_date", "filing_date", "publication_date", "grant_date",
        "filing_year", "granted", "n_publications", "n_bwd_citations",
        "primary_field_number", "primary_field_name", "n_fields",
        "ipc_main", "ipc_codes", "cpc_codes", "member_publications",
    ],
    "family_assignee": ["family_id", "name", "country_code"],
    "family_field": ["family_id", "field_number", "weight"],
    "family_inventor_country": ["family_id", "country_code"],
    "family_class": ["family_id", "scheme", "code"],
}
# Child tables keyed by family_id (everything except field + patent_family itself).
CHILD_TABLES = ["family_assignee", "family_field", "family_inventor_country", "family_class"]

DDL = """
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
  family_id TEXT, field_number INTEGER, weight DOUBLE PRECISION,
  PRIMARY KEY (family_id, field_number));
CREATE TABLE IF NOT EXISTS family_inventor_country (
  family_id TEXT, country_code TEXT, PRIMARY KEY (family_id, country_code));
CREATE TABLE IF NOT EXISTS family_class (
  family_id TEXT, scheme TEXT, code TEXT, PRIMARY KEY (family_id, scheme, code));
CREATE TABLE IF NOT EXISTS _sync_meta (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  synced_at TIMESTAMPTZ DEFAULT now(),
  mode TEXT, families BIGINT, rows_pushed BIGINT, seconds DOUBLE PRECISION, note TEXT);
"""

# Built after the bulk load (mirrors the SQLite indexes; building up front slows COPY).
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_pf_year       ON patent_family(filing_year)",
    "CREATE INDEX IF NOT EXISTS ix_pf_field      ON patent_family(primary_field_number)",
    "CREATE INDEX IF NOT EXISTS ix_pf_field_year ON patent_family(primary_field_number, filing_year)",
    "CREATE INDEX IF NOT EXISTS ix_fa_name       ON family_assignee(name)",
    "CREATE INDEX IF NOT EXISTS ix_fa_cc         ON family_assignee(country_code)",
    "CREATE INDEX IF NOT EXISTS ix_fa_fid        ON family_assignee(family_id)",
    "CREATE INDEX IF NOT EXISTS ix_fa_cc_fid     ON family_assignee(country_code, family_id)",
    "CREATE INDEX IF NOT EXISTS ix_ff_field      ON family_field(field_number)",
    "CREATE INDEX IF NOT EXISTS ix_fc_code       ON family_class(scheme, code, family_id)",
]

STATS = """
DROP TABLE IF EXISTS company_stats;
CREATE TABLE company_stats AS
  SELECT name, country_code, COUNT(DISTINCT family_id) AS n_families
  FROM family_assignee WHERE name <> '' GROUP BY name, country_code;
CREATE INDEX ix_cs_n ON company_stats(n_families);
DROP TABLE IF EXISTS country_stats;
CREATE TABLE country_stats AS
  SELECT country_code, COUNT(DISTINCT family_id) AS n_families
  FROM family_assignee WHERE country_code <> '' GROUP BY country_code;
CREATE INDEX ix_cts_n ON country_stats(n_families);
-- denormalized country filter index (rebuilt server-side, not transferred): makes
-- country+status+year a single index range-scan for both the web app and R/Stata users.
DROP TABLE IF EXISTS family_country;
CREATE TABLE family_country AS
  SELECT a.country_code, f.granted, f.filing_year, a.family_id
  FROM (SELECT DISTINCT family_id, country_code FROM family_assignee WHERE country_code <> '') a
  JOIN patent_family f ON f.family_id = a.family_id;
CREATE INDEX ix_fcn ON family_country(country_code, granted, filing_year, family_id);
CREATE INDEX ix_fcn_fid ON family_country(family_id);
GRANT SELECT ON family_country TO jannie_ro, wren_ro;
ANALYZE;
"""

BATCH = 50_000


def sqlite_conn(path):
    con = sqlite3.connect(path)
    con.text_factory = str
    return con


def copy_rows(pg, table, cols, row_iter, log_every=500_000):
    """Stream rows into Postgres via COPY. Returns count pushed."""
    collist = ", ".join(cols)
    n = 0
    t0 = time.time()
    with pg.cursor() as cur:
        with cur.copy(f"COPY {table} ({collist}) FROM STDIN") as cp:
            for row in row_iter:
                cp.write_row(row)
                n += 1
                if n % log_every == 0:
                    rate = n / (time.time() - t0)
                    print(f"    {table}: {n:,} rows ({rate:,.0f}/s)", flush=True)
    return n


def full_sync(sl, pg, dsn_note):
    print("FULL sync: dropping + recreating Postgres schema...", flush=True)
    with pg.cursor() as cur:
        # Drop everything we manage, then recreate clean.
        cur.execute("DROP TABLE IF EXISTS company_stats, country_stats CASCADE")
        for t in reversed(list(DATA_TABLES)):
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        cur.execute(DDL)
    pg.commit()

    total = 0
    for table, cols in DATA_TABLES.items():
        c = sl.execute(f"SELECT {', '.join(cols)} FROM {table}")
        print(f"  COPY {table}...", flush=True)
        pushed = copy_rows(pg, table, cols, iter_cursor(c))
        pg.commit()
        total += pushed
        print(f"  {table}: {pushed:,} rows", flush=True)

    print("  Building indexes...", flush=True)
    with pg.cursor() as cur:
        for stmt in INDEXES:
            cur.execute(stmt)
    pg.commit()
    print("  Rebuilding stats tables (company_stats, country_stats)...", flush=True)
    with pg.cursor() as cur:
        cur.execute(STATS)
    pg.commit()
    return total


def iter_cursor(c):
    while True:
        rows = c.fetchmany(BATCH)
        if not rows:
            break
        yield from rows


def incremental_sync(sl, pg):
    print("INCREMENTAL sync: diffing family_ids...", flush=True)
    # Pull the set of family_ids already in Neon (streamed, server-side cursor).
    existing = set()
    with pg.cursor(name="existing_ids") as cur:
        cur.itersize = 100_000
        cur.execute("SELECT family_id FROM patent_family")
        for (fid,) in cur:
            existing.add(fid)
    print(f"  Neon currently has {len(existing):,} families", flush=True)

    # Find local families not yet mirrored.
    local_n = sl.execute("SELECT COUNT(*) FROM patent_family").fetchone()[0]
    new_ids = set()
    c = sl.execute("SELECT family_id FROM patent_family")
    for (fid,) in iter_cursor(c):
        if fid not in existing:
            new_ids.add(fid)
    print(f"  Local has {local_n:,} families; {len(new_ids):,} are new", flush=True)
    if not new_ids:
        print("  Up to date — nothing to push.", flush=True)
        return 0
    del existing  # free memory before pushing

    total = 0
    # Stage new family_ids in a temp table so child-row filtering happens server-side-friendly,
    # and so child inserts are idempotent (delete-by-id then insert).
    with pg.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _new_ids (family_id TEXT PRIMARY KEY) ON COMMIT DROP")
    with pg.cursor() as cur:
        with cur.copy("COPY _new_ids (family_id) FROM STDIN") as cp:
            for fid in new_ids:
                cp.write_row((fid,))

    # patent_family: push only new rows (absent by construction; ON CONFLICT guards re-runs).
    cols = DATA_TABLES["patent_family"]
    rows = (r for r in iter_cursor(sl.execute(
        f"SELECT {', '.join(cols)} FROM patent_family")) if r[0] in new_ids)
    print("  COPY new patent_family rows...", flush=True)
    total += copy_into_staged(pg, "patent_family", cols, rows, conflict="(family_id) DO NOTHING")

    # child tables: delete any partial rows for new_ids (crash-safety), then insert matching rows.
    for table in CHILD_TABLES:
        cols = DATA_TABLES[table]
        with pg.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} t USING _new_ids n WHERE t.family_id = n.family_id")
        rows = (r for r in iter_cursor(sl.execute(
            f"SELECT {', '.join(cols)} FROM {table}")) if r[0] in new_ids)
        print(f"  COPY new {table} rows...", flush=True)
        total += copy_into_staged(pg, table, cols, rows, conflict=None)
    pg.commit()

    print("  Refreshing stats tables...", flush=True)
    with pg.cursor() as cur:
        cur.execute(STATS)
    pg.commit()
    return total


def copy_into_staged(pg, table, cols, row_iter, conflict):
    """COPY rows into a temp staging table, then INSERT into target (optionally ON CONFLICT)."""
    collist = ", ".join(cols)
    with pg.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE _stage (LIKE {table}) ON COMMIT DROP")
    n = 0
    with pg.cursor() as cur:
        with cur.copy(f"COPY _stage ({collist}) FROM STDIN") as cp:
            for row in row_iter:
                cp.write_row(row)
                n += 1
    with pg.cursor() as cur:
        oc = f" ON CONFLICT {conflict}" if conflict else ""
        cur.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _stage{oc}")
        cur.execute("DROP TABLE _stage")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB, help="source SQLite path")
    ap.add_argument("--dsn", default=os.environ.get("NEON_DSN"), help="Neon connection string (or $NEON_DSN)")
    ap.add_argument("--full", action="store_true", help="full re-baseline (drop+recreate+COPY all)")
    args = ap.parse_args()
    if not args.dsn:
        sys.exit("error: provide Neon DSN via --dsn or $NEON_DSN")
    if not Path(args.db).exists():
        sys.exit(f"error: SQLite not found: {args.db}")

    t0 = time.time()
    sl = sqlite_conn(args.db)
    # channel_binding in the URL can break psycopg's scram path on some setups; sslmode=require is enough.
    pg = psycopg.connect(args.dsn, autocommit=False)
    try:
        with pg.cursor() as cur:
            cur.execute(DDL)
        pg.commit()
        mode = "full" if args.full else "incremental"
        pushed = full_sync(sl, pg, args.dsn) if args.full else incremental_sync(sl, pg)
        fams = pg.execute("SELECT COUNT(*) FROM patent_family").fetchone()[0]
        secs = time.time() - t0
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO _sync_meta (mode, families, rows_pushed, seconds, note) VALUES (%s,%s,%s,%s,%s)",
                (mode, fams, pushed, secs, Path(args.db).name))
        pg.commit()
        print(f"\nDONE [{mode}]: pushed {pushed:,} rows; Neon now mirrors {fams:,} families "
              f"in {secs/60:.1f} min.", flush=True)
    finally:
        pg.close()
        sl.close()


if __name__ == "__main__":
    main()
