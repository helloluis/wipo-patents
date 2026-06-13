"""
WIPO patents retrieval pipeline: BigQuery (patents-public-data) -> SQLite.

Commands:
  inspect   Sample 20 rows to confirm live schema/IPC format (cheap).
  count     Exact family counts per filing year for [year-start, year-end] -> sizes the mirror.
  extract   Build the SQLite database (families, fields, countries, pre-agg cube).

All queries are cost-guarded with maximum_bytes_billed. Use --dry-run on any command to see
how many bytes it would scan/bill before spending anything.

Auth (one-time, on your machine):
  brew install --cask google-cloud-sdk
  gcloud auth application-default login
  gcloud config set project YOUR_PROJECT_ID      # any project with billing enabled; queries
                                                  # against public data stay within the 1 TB/mo free tier
Then:  export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from google.cloud import bigquery

from .concordance import Concordance

ROOT = Path(__file__).resolve().parent.parent.parent
SQL_DIR = ROOT / "sql"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_CONCORDANCE = ROOT / "data" / "concordance" / "ipc_technology_bootstrap.csv"

# Cost guard. patents-public-data.publications is multi-TB; selecting only the columns we need
# keeps a full extract in the low-hundreds of GB. 1 TB ceiling = hard stop against surprises.
MAX_BYTES = int(os.environ.get("WIPO_MAX_BYTES_BILLED", 1_100_000_000_000))  # ~1.1 TB

GiB = 1024 ** 3


def client() -> bigquery.Client:
    # Prefer an explicit env override; otherwise fall back to the project from
    # `gcloud config set project` (inferred via Application Default Credentials).
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    try:
        return bigquery.Client(project=project) if project else bigquery.Client()
    except Exception as e:  # no project configured anywhere
        sys.exit(
            "No Google Cloud project found. Run `gcloud config set project YOUR_PROJECT_ID` "
            f"or set GOOGLE_CLOUD_PROJECT. (underlying error: {e})"
        )


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def job_config(year_start: int, year_end: int, dry_run: bool, limit: int = 0):
    return bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("year_start", "INT64", year_start),
            bigquery.ScalarQueryParameter("year_end", "INT64", year_end),
        ],
        maximum_bytes_billed=MAX_BYTES,
        dry_run=dry_run,
        use_query_cache=True,
    )


def _maybe_dry(cli, sql, cfg, dry_run) -> bool:
    """If dry_run, print the bytes estimate and return True (caller should stop)."""
    if not dry_run:
        return False
    job = cli.query(sql, job_config=cfg)
    gb = job.total_bytes_processed / GiB
    print(f"DRY RUN: would scan ~{gb:,.1f} GiB (billed up to the {MAX_BYTES/1e12:.1f} TB cap).")
    return True


def cmd_inspect(args):
    cli = client()
    sql = read_sql("00_inspect_schema.sql")
    cfg = job_config(0, 0, args.dry_run)
    if _maybe_dry(cli, sql, cfg, args.dry_run):
        return
    for i, row in enumerate(cli.query(sql, job_config=cfg).result()):
        print(f"\n--- row {i} ---")
        print("publication_number:", row["publication_number"])
        print("family_id:", row["family_id"], " filing_date:", row["filing_date"])
        print("ipc_codes:", list(row["ipc_codes"])[:8])
        print("assignees:", [(a["name"], a["country_code"]) for a in row["assignees"]][:4])
        print("inventors:", [(v["name"], v["country_code"]) for v in row["inventors"]][:4])


def _estimate_storage(total_families: int) -> str:
    # Empirically ~300-450 bytes/family all-in for this lean (no-text) normalized model in SQLite,
    # including child rows (fields, applicant + inventor countries) and indexes. Use 400 as midpoint.
    lo = total_families * 300 / GiB
    hi = total_families * 450 / GiB
    return f"~{lo:,.1f}-{hi:,.1f} GiB for the SQLite mirror (plus a similar transient export)"


def cmd_count(args):
    cli = client()
    sql = read_sql("01_count_families.sql")
    cfg = job_config(args.year_start, args.year_end, args.dry_run)
    if _maybe_dry(cli, sql, cfg, args.dry_run):
        return
    total = 0
    print(f"{'year':>6} {'families':>14}")
    for row in cli.query(sql, job_config=cfg).result():
        total += row["families"]
        print(f"{row['filing_year']:>6} {row['families']:>14,}")
    print(f"{'TOTAL':>6} {total:>14,}")
    print("\nStorage estimate:", _estimate_storage(total))


def cmd_extract(args):
    cli = client()
    conc = Concordance.load(Path(args.concordance))
    sql = read_sql("02_extract_families.sql").rstrip().rstrip(";")
    if args.limit:
        sql += f"\nLIMIT {int(args.limit)}"
    cfg = job_config(args.year_start, args.year_end, args.dry_run)
    if _maybe_dry(cli, sql, cfg, args.dry_run):
        return

    db_path = Path(args.out)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and not args.append:
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    _load_fields(con, conc)

    print(f"Extracting families {args.year_start}-{args.year_end} -> {db_path} ...")
    n = 0
    rows = cli.query(sql, job_config=cfg).result(page_size=10_000)
    cur = con.cursor()
    for row in rows:
        _insert_family(cur, conc, row)
        n += 1
        if n % 100_000 == 0:
            con.commit()
            print(f"  {n:,} families")
    con.commit()
    print(f"Loaded {n:,} families. Building aggregate cube...")
    _build_aggregate(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('source','patents-public-data.patents.publications')")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('unit','DOCDB family, earliest filing year')")
    con.execute(f"INSERT OR REPLACE INTO meta VALUES ('year_range','{args.year_start}-{args.year_end}')")
    con.execute(f"INSERT OR REPLACE INTO meta VALUES ('concordance','{Path(args.concordance).name}')")
    con.commit()
    _print_db_summary(con)
    con.close()


def _load_fields(con, conc: Concordance):
    rows = [(fn, name, sec_no, sec_name) for fn, (name, sec_no, sec_name) in conc.fields.items()]
    con.executemany("INSERT OR REPLACE INTO field VALUES (?,?,?,?)", rows)
    con.commit()


def _insert_family(cur, conc: Concordance, row):
    family_id = row["family_id"]
    year = row["earliest_filing_year"]
    fields = conc.fields_for(row["ipc_codes"])
    apps = sorted({c for c in row["applicant_countries"] if c})
    invs = sorted({c for c in row["inventor_countries"] if c})

    cur.execute(
        "INSERT OR REPLACE INTO family VALUES (?,?,?,?,?)",
        (family_id, year, row["earliest_filing_date"], row["n_publications"], len(fields)),
    )
    if fields:
        w = 1.0 / len(fields)
        cur.executemany(
            "INSERT OR REPLACE INTO family_field VALUES (?,?,?)",
            [(family_id, fn, w) for fn in fields],
        )
    if apps:
        w = 1.0 / len(apps)
        cur.executemany(
            "INSERT OR REPLACE INTO family_applicant_country VALUES (?,?,?)",
            [(family_id, c, w) for c in apps],
        )
    if invs:
        w = 1.0 / len(invs)
        cur.executemany(
            "INSERT OR REPLACE INTO family_inventor_country VALUES (?,?,?)",
            [(family_id, c, w) for c in invs],
        )


def _build_aggregate(con):
    con.execute("DELETE FROM agg_year_field_applicant")
    con.execute(
        """
        INSERT INTO agg_year_field_applicant
        SELECT f.earliest_filing_year, ff.field_number, ac.country_code,
               SUM(ff.weight * ac.weight)            AS families_fractional,
               COUNT(DISTINCT f.family_id)           AS families_whole
        FROM family f
        JOIN family_field ff               ON ff.family_id = f.family_id
        JOIN family_applicant_country ac   ON ac.family_id = f.family_id
        GROUP BY f.earliest_filing_year, ff.field_number, ac.country_code
        """
    )
    con.commit()


def _print_db_summary(con):
    fam = con.execute("SELECT COUNT(*) FROM family").fetchone()[0]
    unmapped = con.execute("SELECT COUNT(*) FROM family WHERE n_fields = 0").fetchone()[0]
    print(f"\nDB summary: {fam:,} families  ({unmapped:,} with no mapped tech field)")
    print("Top fields by fractional family count:")
    for r in con.execute(
        """SELECT fd.field_name, ROUND(SUM(ff.weight),0) AS n
           FROM family_field ff JOIN field fd ON fd.field_number = ff.field_number
           GROUP BY ff.field_number ORDER BY n DESC LIMIT 8"""
    ):
        print(f"  {r[1]:>12,.0f}  {r[0]}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="wipo-patents", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="sample live rows (cheap)")
    pi.add_argument("--dry-run", action="store_true")
    pi.set_defaults(func=cmd_inspect)

    pc = sub.add_parser("count", help="exact family counts per year (sizes the mirror)")
    pc.add_argument("--year-start", type=int, default=2000)
    pc.add_argument("--year-end", type=int, default=2026)
    pc.add_argument("--dry-run", action="store_true")
    pc.set_defaults(func=cmd_count)

    pe = sub.add_parser("extract", help="build the SQLite database")
    pe.add_argument("--year-start", type=int, default=2000)
    pe.add_argument("--year-end", type=int, default=2026)
    pe.add_argument("--out", default=str(ROOT / "data" / "wipo_patents.sqlite"))
    pe.add_argument("--concordance", default=str(DEFAULT_CONCORDANCE))
    pe.add_argument("--limit", type=int, default=0, help="cap rows for a smoke test (0 = all)")
    pe.add_argument("--append", action="store_true", help="keep existing DB instead of recreating")
    pe.add_argument("--dry-run", action="store_true")
    pe.set_defaults(func=cmd_extract)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
