"""
wipo-patents web app — public, read-only query UI over the Europe SQLite dataset.

Run locally:   uvicorn app.main:app --reload   (from repo root, venv active)
DB path:       WIPO_DB env var, default data/europe.sqlite
"""
import csv
import io
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("WIPO_DB", str(ROOT / "data" / "europe.sqlite"))

app = FastAPI(title="WIPO Patents — Europe")


def db():
    # read-only, immutable: fastest concurrent serving, no locking
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def filters(field, country, q, y0, y1):
    """Build (extra_join, where_sql, params). Joins assignee only when needed."""
    joins, where, params = "", [], []
    need_assignee = bool(country or q)
    if need_assignee:
        joins = "JOIN family_assignee a ON a.family_id = f.family_id"
    if country:
        where.append("a.country_code = ?")
        params.append(country)
    if q:
        where.append("a.name LIKE ?")
        params.append(f"%{q.upper()}%")
    if field:
        where.append("f.primary_field_number = ?")
        params.append(field)
    if y0:
        where.append("f.filing_year >= ?")
        params.append(y0)
    if y1:
        where.append("f.filing_year <= ?")
        params.append(y1)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return joins, where_sql, params, need_assignee


@app.get("/api/meta")
def meta():
    con = db()
    fields = [dict(r) for r in con.execute(
        "SELECT field_number, field_name, sector_name FROM field ORDER BY field_number")]
    countries = [r[0] for r in con.execute(
        """SELECT country_code FROM family_assignee WHERE country_code != ''
           GROUP BY country_code ORDER BY COUNT(*) DESC""")]
    yr = con.execute("SELECT MIN(filing_year), MAX(filing_year) FROM patent_family").fetchone()
    tot = con.execute("SELECT COUNT(*) FROM patent_family").fetchone()[0]
    ncomp = con.execute("SELECT COUNT(DISTINCT name) FROM family_assignee").fetchone()[0]
    con.close()
    return {"fields": fields, "countries": countries,
            "year_min": yr[0], "year_max": yr[1], "total_families": tot, "total_companies": ncomp}


@app.get("/api/trend")
def trend(dim: str = "year", field: int = 0, country: str = "", q: str = "",
          y0: int = 0, y1: int = 0):
    con = db()
    joins, where, params, need_assignee = filters(field, country, q, y0, y1)
    cnt = "COUNT(DISTINCT f.family_id)" if need_assignee else "COUNT(*)"
    if dim == "field":
        sql = (f"SELECT fd.field_name AS label, {cnt} AS n FROM patent_family f {joins} "
               f"JOIN field fd ON fd.field_number = f.primary_field_number {where} "
               f"GROUP BY f.primary_field_number ORDER BY n DESC LIMIT 15")
    elif dim == "country":
        j = joins or "JOIN family_assignee a ON a.family_id = f.family_id"
        sql = (f"SELECT a.country_code AS label, COUNT(DISTINCT f.family_id) AS n "
               f"FROM patent_family f {j} {where} "
               f"{'AND' if where else 'WHERE'} a.country_code != '' "
               f"GROUP BY a.country_code ORDER BY n DESC LIMIT 15")
    else:  # year
        sql = (f"SELECT f.filing_year AS label, {cnt} AS n FROM patent_family f {joins} {where} "
               f"GROUP BY f.filing_year ORDER BY f.filing_year")
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return {"dim": dim, "data": rows}


@app.get("/api/patents")
def patents(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
            limit: int = 50, offset: int = 0):
    con = db()
    joins, where, params, need_assignee = filters(field, country, q, y0, y1)
    total = con.execute(
        f"SELECT COUNT(DISTINCT f.family_id) FROM patent_family f {joins} {where}", params
    ).fetchone()[0]
    rows = con.execute(
        f"""SELECT DISTINCT f.family_id, f.rep_publication, f.filing_year,
                   f.primary_field_name, f.granted, f.n_bwd_citations, f.n_publications
            FROM patent_family f {joins} {where}
            ORDER BY f.filing_year DESC, f.family_id LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    fam_ids = [r["family_id"] for r in rows]
    owners = {}
    if fam_ids:
        ph = ",".join("?" * len(fam_ids))
        for r in con.execute(
            f"SELECT family_id, name, country_code FROM family_assignee WHERE family_id IN ({ph})",
            fam_ids):
            owners.setdefault(r["family_id"], []).append(
                {"name": r["name"], "country": r["country_code"]})
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["assignees"] = owners.get(r["family_id"], [])
        out.append(d)
    return {"total": total, "limit": limit, "offset": offset, "rows": out}


@app.get("/api/companies")
def companies(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
              limit: int = 25):
    con = db()
    # always join assignee here; field/year filters still apply to f
    _, where, params, _ = filters(field, "", "", y0, y1)
    extra, eparams = [], []
    if country:
        extra.append("a.country_code = ?"); eparams.append(country)
    if q:
        extra.append("a.name LIKE ?"); eparams.append(f"%{q.upper()}%")
    extra.append("a.name != ''")
    w = where + (" AND " if where else "WHERE ") + " AND ".join(extra)
    sql = (f"SELECT a.name, a.country_code, COUNT(DISTINCT f.family_id) AS families "
           f"FROM patent_family f JOIN family_assignee a ON a.family_id = f.family_id {w} "
           f"GROUP BY a.name, a.country_code ORDER BY families DESC LIMIT ?")
    rows = [dict(r) for r in con.execute(sql, params + eparams + [limit])]
    con.close()
    return {"rows": rows}


@app.get("/api/export.csv")
def export_csv(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0):
    con = db()
    joins, where, params, _ = filters(field, country, q, y0, y1)
    CAP = 50000
    rows = con.execute(
        f"""SELECT DISTINCT f.family_id, f.rep_publication, f.filing_year, f.primary_field_name,
                   f.granted, f.n_publications, f.n_bwd_citations
            FROM patent_family f {joins} {where}
            ORDER BY f.filing_year DESC, f.family_id LIMIT {CAP}""", params).fetchall()
    fam_ids = [r["family_id"] for r in rows]
    owners = {}
    if fam_ids:
        ph = ",".join("?" * len(fam_ids))
        for r in con.execute(
            f"SELECT family_id, name, country_code FROM family_assignee WHERE family_id IN ({ph})",
            fam_ids):
            owners.setdefault(r["family_id"], []).append(f'{r["name"]} ({r["country_code"]})')
    con.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["family_id", "publication", "filing_year", "wipo_field", "granted",
                "n_publications", "n_backward_citations", "companies"])
    for r in rows:
        w.writerow([r["family_id"], r["rep_publication"], r["filing_year"], r["primary_field_name"],
                    r["granted"], r["n_publications"], r["n_bwd_citations"],
                    "; ".join(owners.get(r["family_id"], []))])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=wipo_patents_europe.csv"})


# static frontend (mounted last so /api/* wins)
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
