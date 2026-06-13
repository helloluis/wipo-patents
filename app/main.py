"""
wipo-patents web app — public, read-only query UI over the Europe SQLite dataset.

Run locally:   uvicorn app.main:app --reload   (from repo root, venv active)
DB path:       WIPO_DB env var, default data/europe.sqlite

Performance model: the data is static and read-only, so every endpoint result is cached
in-process (shared across requests; run with a single uvicorn worker). Country/company
filters are driven from the assignee index (fast); field/year filters hit patent_family
directly. The landing-page views are warmed at startup.
"""
import csv
import io
import os
import sqlite3
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("WIPO_DB", str(ROOT / "data" / "europe.sqlite"))

app = FastAPI(title="WIPO Patents — Europe")


@app.middleware("http")
async def _cache_headers(request, call_next):
    # HTML/CSS/JS: revalidate every load (cheap via ETag) so deploys propagate immediately
    # instead of being stuck in aggressive mobile caches.
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".css", ".js")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

# ---- tiny static-data cache (correct because the DB never changes) -------------------
_CACHE = OrderedDict()
_CACHE_MAX = 1000


def cached(key, fn):
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    val = fn()
    _CACHE[key] = val
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return val


def db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def base(field, country, q, y0, y1):
    """Return (join, where_sql, params) yielding DISTINCT matching families efficiently.

    Country/company → a deduped family_id subquery off the assignee index.
    Field/year      → direct predicates on patent_family.
    """
    join, where, params = "", [], []
    sub, sparams = [], []
    if country:
        sub.append("country_code = ?"); sparams.append(country)
    if q:
        sub.append("name LIKE ?"); sparams.append(f"%{q.upper()}%")
    if sub:
        join = (f"JOIN (SELECT family_id FROM family_assignee WHERE {' AND '.join(sub)} "
                f"GROUP BY family_id) m ON m.family_id = f.family_id")
        params += sparams
    if field:
        where.append("f.primary_field_number = ?"); params.append(field)
    if y0:
        where.append("f.filing_year >= ?"); params.append(y0)
    if y1:
        where.append("f.filing_year <= ?"); params.append(y1)
    return join, (("WHERE " + " AND ".join(where)) if where else ""), params


# ---- meta (static facets) ------------------------------------------------------------
_META = {}


def _compute_meta():
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


@app.get("/api/meta")
def meta():
    if not _META:
        _META.update(_compute_meta())
    return _META


# ---- endpoints -----------------------------------------------------------------------
@app.get("/api/trend")
def trend(dim: str = "year", field: int = 0, country: str = "", q: str = "",
          y0: int = 0, y1: int = 0):
    unfiltered = not (field or country or q or y0 or y1)

    def run():
        con = db()
        join, where, params = base(field, country, q, y0, y1)
        if dim == "country" and unfiltered:
            # served from the materialized table — instant
            sql, params = ("SELECT country_code AS label, n_families AS n FROM country_stats "
                           "ORDER BY n_families DESC LIMIT 15"), []
        elif dim == "field":
            sql = (f"SELECT fd.field_name AS label, COUNT(*) AS n FROM patent_family f {join} "
                   f"JOIN field fd ON fd.field_number = f.primary_field_number {where} "
                   f"GROUP BY f.primary_field_number ORDER BY n DESC LIMIT 15")
        elif dim == "country":
            glue = "AND" if where else "WHERE"
            sql = (f"SELECT a.country_code AS label, COUNT(DISTINCT f.family_id) AS n "
                   f"FROM patent_family f {join} JOIN family_assignee a ON a.family_id = f.family_id "
                   f"{where} {glue} a.country_code != '' GROUP BY a.country_code ORDER BY n DESC LIMIT 15")
        else:
            sql = (f"SELECT f.filing_year AS label, COUNT(*) AS n FROM patent_family f {join} {where} "
                   f"GROUP BY f.filing_year ORDER BY f.filing_year")
        rows = [dict(r) for r in con.execute(sql, params)]
        con.close()
        return {"dim": dim, "data": rows}
    return cached(("trend", dim, field, country, q, y0, y1), run)


@app.get("/api/patents")
def patents(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
            limit: int = 50, offset: int = 0):
    def run():
        con = db()
        join, where, params = base(field, country, q, y0, y1)
        total = con.execute(
            f"SELECT COUNT(*) FROM patent_family f {join} {where}", params).fetchone()[0]
        rows = con.execute(
            f"""SELECT f.family_id, f.rep_publication, f.filing_year, f.primary_field_name,
                       f.granted, f.n_bwd_citations, f.n_publications
                FROM patent_family f {join} {where}
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
    return cached(("patents", field, country, q, y0, y1, limit, offset), run)


@app.get("/api/companies")
def companies(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
              limit: int = 25):
    unfiltered = not (field or country or q or y0 or y1)

    def run():
        con = db()
        if unfiltered:
            # served from the materialized table — instant
            rows = [{"name": r["name"], "country_code": r["country_code"], "families": r["n_families"]}
                    for r in con.execute(
                        "SELECT name, country_code, n_families FROM company_stats "
                        "ORDER BY n_families DESC LIMIT ?", [limit])]
            con.close()
            return {"rows": rows}
        join, where, params = base(field, country, q, y0, y1)
        glue = "AND" if where else "WHERE"
        sql = (f"SELECT a2.name, a2.country_code, COUNT(DISTINCT f.family_id) AS families "
               f"FROM patent_family f {join} JOIN family_assignee a2 ON a2.family_id = f.family_id "
               f"{where} {glue} a2.name != '' GROUP BY a2.name, a2.country_code "
               f"ORDER BY families DESC LIMIT ?")
        rows = [dict(r) for r in con.execute(sql, params + [limit])]
        con.close()
        return {"rows": rows}
    return cached(("companies", field, country, q, y0, y1, limit), run)


@app.get("/api/export.csv")
def export_csv(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0):
    con = db()
    join, where, params = base(field, country, q, y0, y1)
    CAP = 50000
    rows = con.execute(
        f"""SELECT f.family_id, f.rep_publication, f.filing_year, f.primary_field_name,
                   f.granted, f.n_publications, f.n_bwd_citations
            FROM patent_family f {join} {where}
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


@app.on_event("startup")
def _warm():
    # warm meta + default landing views in the background so the service is ready
    # immediately on (re)start; the first visitor during warm-up just computes live.
    import threading

    def job():
        try:
            meta()
            for dim in ("year", "field", "country"):
                trend(dim=dim)
            patents()
            companies()
        except Exception:
            pass

    threading.Thread(target=job, daemon=True).start()


# static frontend (mounted last so /api/* wins)
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
