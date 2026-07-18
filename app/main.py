"""
wipo-patents web app — public, read-only query UI over the patent dataset on Neon Postgres.

Run locally:   NEON_DSN='postgresql://.../neondb?sslmode=require' uvicorn app.main:app --reload
Data:          Neon Postgres (single source of truth; the website + Jannie's R/Stata share it).

Performance model: the data is static and read-only, so every endpoint result is cached
in-process (shared across requests; run with a single uvicorn worker). Filtered queries are
driven from the most selective index (family_class range / family_country); the landing-page
views are warmed at startup. Neon scale-to-zero means the FIRST query after an idle gap pays a
cold-start (~few s) while the compute resumes — acceptable for a single-user research tool.
"""
import csv
import io
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Hard cap on any single query's run time (Postgres statement_timeout) — a runaway/too-broad
# query is aborted instead of hanging the worker.
STMT_TIMEOUT_MS = 35_000

DSN = os.environ.get("NEON_DSN") or os.environ.get("WIPO_DSN")
if not DSN:
    raise RuntimeError("NEON_DSN env var (Neon Postgres connection string) is required")

# ---- AI research assistant (server-side proxy to Fireworks; API key never reaches the browser) ---
FIREWORKS_KEY = os.environ.get("FIREWORKS_API_KEY", "")
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
CHAT_MODEL = os.environ.get("CHAT_MODEL", "accounts/fireworks/models/qwen3p7-plus")

app = FastAPI(title="WIPO Patents — Worldwide")


class _CacheHeaders:
    """Pure-ASGI middleware adding Cache-Control:no-cache to html/css/js so deploys propagate.
    Pure ASGI (NOT BaseHTTPMiddleware) so it never buffers streaming responses like /api/chat."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        p = scope.get("path", "")
        tag = p == "/" or p.endswith((".html", ".css", ".js"))

        async def send_wrap(message):
            if tag and message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"cache-control", b"no-cache"))
            await send(message)

        await self.app(scope, receive, send_wrap)


app.add_middleware(_CacheHeaders)

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
    # per-request connection (autocommit read-only). statement_timeout is set as a role default
    # (ALTER ROLE app_ro SET statement_timeout) — Neon's pooler rejects it as a startup param.
    # connect_timeout covers a Neon cold-start resume; dict_row makes rows dict-accessible.
    return psycopg.connect(DSN, autocommit=True, row_factory=dict_row, connect_timeout=30)


def _range(s):
    """A typed IPC/CPC class prefix → an indexable [lo, hi) range so the family_class index is
    used ('F21' → ['F21','F22'))."""
    p = s.upper().replace(" ", "")
    return p, p[:-1] + chr(ord(p[-1]) + 1)


def base(field, country, q, y0, y1, status="", ipc="", cpc=""):
    """Return (join, where_sql, params) yielding matching families efficiently.

    Driver selection: the most selective indexed filter drives (a JOIN subquery off its index);
    the rest become EXISTS probes. IPC/CPC use a range scan on family_class; country uses the
    family_country index — as the driver when it's the only selective filter, otherwise as an
    EXISTS so the narrower class/field filter can drive instead.
    """
    joins, jparams, wheres, wparams = [], [], [], []
    gflag = 1 if status == "granted" else 0 if status == "application" else None
    class_present = bool(ipc or cpc)
    for scheme, val in (("ipc", ipc), ("cpc", cpc)):
        if val:
            lo, hi = _range(val)
            joins.append(f"JOIN (SELECT DISTINCT family_id FROM family_class WHERE scheme = '{scheme}' "
                         f"AND code >= %s AND code < %s) m{scheme} ON m{scheme}.family_id = f.family_id")
            jparams += [lo, hi]
    if country:
        cw, cp = ["country_code = %s"], [country]
        if gflag is not None:
            cw.append("granted = %s"); cp.append(gflag)
        if y0:
            cw.append("filing_year >= %s"); cp.append(y0)
        if y1:
            cw.append("filing_year <= %s"); cp.append(y1)
        if class_present or field:
            cw[0] = "fcn.country_code = %s"  # qualify for the correlated EXISTS
            wheres.append(f"EXISTS (SELECT 1 FROM family_country fcn WHERE fcn.family_id = f.family_id "
                          f"AND {' AND '.join(cw)})")
            wparams += cp
        else:
            joins.append(f"JOIN (SELECT DISTINCT family_id FROM family_country "
                         f"WHERE {' AND '.join(cw)}) mco ON mco.family_id = f.family_id")
            jparams += cp
    if q:
        joins.append("JOIN (SELECT family_id FROM family_assignee WHERE name LIKE %s "
                     "GROUP BY family_id) mq ON mq.family_id = f.family_id")
        jparams.append(f"%{q.upper()}%")
    if field:
        wheres.append("f.primary_field_number = %s"); wparams.append(field)
    if status == "granted":
        wheres.append("f.granted = 1")
    elif status == "application":
        wheres.append("f.granted = 0")
    if y0:
        wheres.append("f.filing_year >= %s"); wparams.append(y0)
    if y1:
        wheres.append("f.filing_year <= %s"); wparams.append(y1)
    join = " ".join(joins)
    where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    return join, where, jparams + wparams


def _candidate(field, country, q, y0, y1, status, ipc, cpc):
    """Family-level candidate set for a fast COUNT: (sql, params, needs_pf).

    `sql` is the INTERSECT of the class/country/company filters selecting DISTINCT family_id,
    with status+year folded into family_country when a country filter is present. Counting this
    directly avoids joining the 7.86M-row patent_family (which, on Neon's networked storage, means
    thousands of page fetches). `needs_pf` is True when a patent_family column is still required
    (a field filter, or status/year with no country candidate to carry them) — then fall back to
    the base() join. Returns (None, [], True) when there is no family-level filter at all.
    """
    gflag = 1 if status == "granted" else 0 if status == "application" else None
    parts, params = [], []
    for scheme, val in (("ipc", ipc), ("cpc", cpc)):
        if val:
            lo, hi = _range(val)
            parts.append("SELECT DISTINCT family_id FROM family_class "
                         "WHERE scheme = %s AND code >= %s AND code < %s")
            params += [scheme, lo, hi]
    carries_yr = False
    if country:
        cw, cp = ["country_code = %s"], [country]
        if gflag is not None:
            cw.append("granted = %s"); cp.append(gflag)
        if y0:
            cw.append("filing_year >= %s"); cp.append(y0)
        if y1:
            cw.append("filing_year <= %s"); cp.append(y1)
        parts.append(f"SELECT DISTINCT family_id FROM family_country WHERE {' AND '.join(cw)}")
        params += cp
        carries_yr = True
    if q:
        parts.append("SELECT family_id FROM family_assignee WHERE name LIKE %s GROUP BY family_id")
        params.append(f"%{q.upper()}%")
    if not parts:
        return None, [], True
    needs_pf = bool(field) or (not carries_yr and (gflag is not None or y0 or y1))
    sql = "(" + " INTERSECT ".join(parts) + ")"
    return sql, params, needs_pf


# ---- meta (static facets) ------------------------------------------------------------
_META = {}


def _compute_meta():
    con = db()
    fields = con.execute(
        "SELECT field_number, field_name, sector_name FROM field ORDER BY field_number").fetchall()
    # Countries from the materialized country_stats — a live GROUP BY over the 100M+ row assignee
    # table would exceed the query timeout on the global dataset.
    countries = [r["country_code"] for r in con.execute(
        "SELECT country_code FROM country_stats WHERE country_code <> '' ORDER BY n_families DESC")]
    # Headline scalars (families, distinct companies, year range) are precomputed once at load time
    # into meta_stats — COUNT(DISTINCT name) over 100M+ rows is far too slow to run per cold cache.
    m = con.execute("SELECT total_families, total_companies, year_min, year_max "
                    "FROM meta_stats LIMIT 1").fetchone()
    # distinct subclass codes per scheme (precomputed into class_index — DISTINCT over the 190M+ row
    # family_class table is far too slow live) — drive the IPC/CPC autocomplete datalists
    ipc_classes = [r["code"] for r in con.execute(
        "SELECT code FROM class_index WHERE scheme='ipc' ORDER BY code")]
    cpc_classes = [r["code"] for r in con.execute(
        "SELECT code FROM class_index WHERE scheme='cpc' ORDER BY code")]
    con.close()
    return {"fields": fields, "countries": countries,
            "year_min": m["year_min"], "year_max": m["year_max"],
            "total_families": m["total_families"], "total_companies": m["total_companies"],
            "ipc_classes": ipc_classes, "cpc_classes": cpc_classes}


@app.get("/api/meta")
def meta():
    if not _META:
        _META.update(_compute_meta())
    return _META


# ---- endpoints -----------------------------------------------------------------------
@app.get("/api/trend")
def trend(dim: str = "year", field: int = 0, country: str = "", q: str = "",
          y0: int = 0, y1: int = 0, status: str = "", ipc: str = "", cpc: str = ""):
    unfiltered = not (field or country or q or y0 or y1 or status or ipc or cpc)

    def run():
        con = db()
        join, where, params = base(field, country, q, y0, y1, status, ipc, cpc)
        if unfiltered and dim == "year":
            # materialized — a live GROUP BY over the full patent_family is too slow at 81M rows
            sql, params = ("SELECT filing_year AS label, granted, pending "
                           "FROM year_stats ORDER BY filing_year"), []
        elif unfiltered and dim == "field":
            sql, params = ("SELECT field_name AS label, n FROM field_stats "
                           "ORDER BY n DESC LIMIT 15"), []
        elif unfiltered and dim == "country":
            sql, params = ("SELECT country_code AS label, n_families AS n FROM country_stats "
                           "ORDER BY n_families DESC LIMIT 15"), []
        elif dim == "field":
            sql = (f"SELECT fd.field_name AS label, COUNT(*) AS n FROM patent_family f {join} "
                   f"JOIN field fd ON fd.field_number = f.primary_field_number {where} "
                   f"GROUP BY fd.field_name ORDER BY n DESC LIMIT 15")
        elif dim == "country":
            glue = "AND" if where else "WHERE"
            sql = (f"SELECT a.country_code AS label, COUNT(DISTINCT f.family_id) AS n "
                   f"FROM patent_family f {join} JOIN family_assignee a ON a.family_id = f.family_id "
                   f"{where} {glue} a.country_code != '' GROUP BY a.country_code ORDER BY n DESC LIMIT 15")
        elif dim == "year" and country and not (field or q or ipc or cpc):
            # fast path: family_country already carries granted+filing_year → pure index aggregate.
            cw, params = ["country_code = %s"], [country]
            if status == "granted":
                cw.append("granted = 1")
            elif status == "application":
                cw.append("granted = 0")
            if y0:
                cw.append("filing_year >= %s"); params.append(y0)
            if y1:
                cw.append("filing_year <= %s"); params.append(y1)
            sql = (f"SELECT filing_year AS label, SUM(granted) AS granted, "
                   f"SUM(1 - granted) AS pending FROM family_country "
                   f"WHERE {' AND '.join(cw)} GROUP BY filing_year ORDER BY filing_year")
        else:  # year — split by grant status for the stacked filed-vs-granted view
            sql = (f"SELECT f.filing_year AS label, "
                   f"SUM(f.granted) AS granted, SUM(1 - f.granted) AS pending "
                   f"FROM patent_family f {join} {where} "
                   f"GROUP BY f.filing_year ORDER BY f.filing_year")
        rows = [dict(r) for r in con.execute(sql, params)]
        con.close()
        return {"dim": dim, "data": rows}
    return cached(("trend", dim, field, country, q, y0, y1, status, ipc, cpc), run)


@app.get("/api/patents")
def patents(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
            status: str = "", ipc: str = "", cpc: str = "", limit: int = 50, offset: int = 0):
    cols = ("f.family_id, f.rep_publication, f.filing_year, f.primary_field_name, "
            "f.granted, f.n_bwd_citations, f.n_publications, f.ipc_main, f.cpc_codes")

    def run():
        con = db()
        if country and not (field or q or ipc or cpc):
            # fast path: total + page driven off the family_country index (no assignee join).
            cw, cp = ["fc.country_code = %s"], [country]
            if status == "granted":
                cw.append("fc.granted = 1")
            elif status == "application":
                cw.append("fc.granted = 0")
            if y0:
                cw.append("fc.filing_year >= %s"); cp.append(y0)
            if y1:
                cw.append("fc.filing_year <= %s"); cp.append(y1)
            cwsql = " AND ".join(cw)
            total = con.execute(
                f"SELECT COUNT(*) AS n FROM family_country fc WHERE {cwsql}", cp).fetchone()["n"]
            rows = con.execute(
                f"""SELECT {cols} FROM family_country fc
                    JOIN patent_family f ON f.family_id = fc.family_id
                    WHERE {cwsql} ORDER BY fc.filing_year DESC, fc.family_id LIMIT %s OFFSET %s""",
                cp + [limit, offset]).fetchall()
        else:
            join, where, params = base(field, country, q, y0, y1, status, ipc, cpc)
            cand_sql, cand_params, needs_pf = _candidate(field, country, q, y0, y1, status, ipc, cpc)
            if cand_sql is not None and not needs_pf:
                # count the family-level candidate directly — no patent_family page fetches
                total = con.execute(
                    f"SELECT COUNT(*) AS n FROM {cand_sql} c", cand_params).fetchone()["n"]
            else:
                total = con.execute(
                    f"SELECT COUNT(*) AS n FROM patent_family f {join} {where}", params).fetchone()["n"]
            rows = con.execute(
                f"""SELECT {cols} FROM patent_family f {join} {where}
                    ORDER BY f.filing_year DESC, f.family_id LIMIT %s OFFSET %s""",
                params + [limit, offset]).fetchall()
        fam_ids = [r["family_id"] for r in rows]
        owners = {}
        if fam_ids:
            for r in con.execute(
                "SELECT family_id, name, country_code FROM family_assignee WHERE family_id = ANY(%s)",
                [fam_ids]):
                owners.setdefault(r["family_id"], []).append(
                    {"name": r["name"], "country": r["country_code"]})
        con.close()
        out = []
        for r in rows:
            d = dict(r)
            d["assignees"] = owners.get(r["family_id"], [])
            out.append(d)
        return {"total": total, "limit": limit, "offset": offset, "rows": out}
    return cached(("patents", field, country, q, y0, y1, status, ipc, cpc, limit, offset), run)


@app.get("/api/patent")
def patent(id: str):
    """Everything we hold on one family — powers the detail lightbox."""
    def run():
        con = db()
        row = con.execute("SELECT * FROM patent_family WHERE family_id = %s", [id]).fetchone()
        if not row:
            con.close()
            return {"error": "not found"}
        d = dict(row)
        d["assignees"] = con.execute(
            "SELECT name, country_code FROM family_assignee WHERE family_id = %s", [id]).fetchall()
        d["fields"] = con.execute(
            """SELECT fd.field_number, fd.field_name, fd.sector_name
               FROM family_field ff JOIN field fd ON fd.field_number = ff.field_number
               WHERE ff.family_id = %s ORDER BY fd.field_number""", [id]).fetchall()
        d["inventor_countries"] = [r["country_code"] for r in con.execute(
            "SELECT country_code FROM family_inventor_country WHERE family_id = %s", [id])]
        con.close()
        return d
    return cached(("patent", id), run)


@app.get("/api/companies")
def companies(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
              status: str = "", ipc: str = "", cpc: str = "", limit: int = 25):
    unfiltered = not (field or country or q or y0 or y1 or status or ipc or cpc)

    def run():
        con = db()
        if unfiltered:
            rows = [{"name": r["name"], "country_code": r["country_code"], "families": r["n_families"]}
                    for r in con.execute(
                        "SELECT name, country_code, n_families FROM company_stats "
                        "ORDER BY n_families DESC LIMIT %s", [limit])]
            con.close()
            return {"rows": rows}
        join, where, params = base(field, country, q, y0, y1, status, ipc, cpc)
        glue = "AND" if where else "WHERE"
        sql = (f"SELECT a2.name, a2.country_code, COUNT(DISTINCT f.family_id) AS families "
               f"FROM patent_family f {join} JOIN family_assignee a2 ON a2.family_id = f.family_id "
               f"{where} {glue} a2.name != '' GROUP BY a2.name, a2.country_code "
               f"ORDER BY families DESC LIMIT %s")
        rows = [dict(r) for r in con.execute(sql, params + [limit])]
        con.close()
        return {"rows": rows}
    return cached(("companies", field, country, q, y0, y1, status, ipc, cpc, limit), run)


def _iso(d):
    """YYYYMMDD int -> 'YYYY-MM-DD' (or '' if missing)."""
    return f"{d//10000:04d}-{d//100%100:02d}-{d%100:02d}" if d else ""


@app.get("/api/export.csv")
def export_csv(field: int = 0, country: str = "", q: str = "", y0: int = 0, y1: int = 0,
               status: str = "", ipc: str = "", cpc: str = ""):
    con = db()
    join, where, params = base(field, country, q, y0, y1, status, ipc, cpc)
    CAP = 50000
    rows = con.execute(
        f"""SELECT f.family_id, f.rep_publication, f.rep_application, f.filing_year,
                   f.priority_date, f.filing_date, f.publication_date, f.grant_date, f.granted,
                   f.primary_field_number, f.primary_field_name, f.ipc_main, f.ipc_codes,
                   f.cpc_codes, f.n_publications, f.member_publications, f.n_bwd_citations
            FROM patent_family f {join} {where}
            ORDER BY f.filing_year DESC, f.family_id LIMIT {CAP}""", params).fetchall()
    fam_ids = [r["family_id"] for r in rows]
    owners = {}
    if fam_ids:
        for r in con.execute(
            "SELECT family_id, name, country_code FROM family_assignee WHERE family_id = ANY(%s)",
            [fam_ids]):
            owners.setdefault(r["family_id"], []).append(f'{r["name"]} ({r["country_code"]})')
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["family_id", "publication_number", "application_number",
                "priority_date", "filing_date", "publication_date", "grant_date", "filing_year",
                "granted", "wipo_field_number", "wipo_field", "ipc_main", "ipc_codes", "cpc_codes",
                "n_family_members", "family_member_publications", "n_backward_citations",
                "applicants"])
    for r in rows:
        w.writerow([r["family_id"], r["rep_publication"], r["rep_application"],
                    _iso(r["priority_date"]), _iso(r["filing_date"]), _iso(r["publication_date"]),
                    _iso(r["grant_date"]), r["filing_year"], r["granted"],
                    r["primary_field_number"], r["primary_field_name"], r["ipc_main"],
                    r["ipc_codes"], r["cpc_codes"], r["n_publications"], r["member_publications"],
                    r["n_bwd_citations"], "; ".join(owners.get(r["family_id"], []))])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=wipo_patents_us_europe.csv"})


# ---- assistant-generated CSV downloads (persisted on the server until deleted) -------
# The assistant asks for a file by emitting a ```csv fenced JSON spec (see CHAT_SYSTEM);
# the frontend POSTs it here. Files live in DOWNLOAD_DIR as {id}.csv + {id}.json sidecar.
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR")
                    or Path(__file__).resolve().parent.parent / "data" / "downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DL_ROW_CAP = 500_000    # assistant exports run in the background, so they can be large
MAX_DL_FILES = 200       # disk-full guard for this public endpoint
_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Columns the assistant may request → SQL on "patent_family f". Keys double as the CSV
# headers and the SELECT aliases, so psycopg dict rows key on them directly.
# "applicants" is special: aggregated from family_assignee after the main query.
EXPORT_COLUMNS = OrderedDict([
    ("family_id", "f.family_id"),
    ("publication_number", "f.rep_publication AS publication_number"),
    ("application_number", "f.rep_application AS application_number"),
    ("priority_date", "f.priority_date"),
    ("filing_date", "f.filing_date"),
    ("publication_date", "f.publication_date"),
    ("grant_date", "f.grant_date"),
    ("filing_year", "f.filing_year"),
    ("granted", "f.granted"),
    ("wipo_field_number", "f.primary_field_number AS wipo_field_number"),
    ("wipo_field", "f.primary_field_name AS wipo_field"),
    ("ipc_main", "f.ipc_main"),
    ("ipc_codes", "f.ipc_codes"),
    ("cpc_codes", "f.cpc_codes"),
    ("n_family_members", "f.n_publications AS n_family_members"),
    ("family_member_publications", "f.member_publications"),
    ("n_backward_citations", "f.n_bwd_citations AS n_backward_citations"),
    ("n_forward_citations", "COALESCE(fc.n_forward_citations, 0) AS n_forward_citations"),
    ("applicants", None),
])
_DATE_COLS = {"priority_date", "filing_date", "publication_date", "grant_date"}


def _safe_name(name):
    """Display/download filename: ASCII only, no path tricks, always ends in .csv."""
    name = re.sub(r"[^A-Za-z0-9 ._\-()]", "", str(name or "")).strip().strip(".")[:80].strip()
    if not name:
        name = "patents"
    return name if name.lower().endswith(".csv") else name + ".csv"


def _dl_meta(fid):
    try:
        return json.loads((DOWNLOAD_DIR / f"{fid}.json").read_text())
    except Exception:
        return None


def _dl_list():
    """All stored files, newest first (reverse chronological for the Downloads section).
    Includes queued/running/failed jobs (their .csv doesn't exist yet / any more)."""
    out = []
    for p in DOWNLOAD_DIR.glob("*.json"):
        m = _dl_meta(p.stem)
        if not m:
            continue
        csvp = DOWNLOAD_DIR / f"{p.stem}.csv"
        if csvp.exists():
            m["size"] = csvp.stat().st_size
        out.append(m)
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def _dl_not_found():
    return JSONResponse({"error": "not found"}, status_code=404)


class _DlSpec(BaseModel):
    name: str = ""
    columns: list[str] = []
    filters: dict = {}
    limit: int = 0


class _DlRename(BaseModel):
    name: str = ""


@app.get("/api/downloads")
def downloads_list():
    return {"files": _dl_list()}


@app.post("/api/downloads")
def downloads_create(spec: _DlSpec, request: Request):
    """Validate the spec, persist a queued job, return immediately — generation runs on the
    background worker (large exports can take minutes; the UI polls /api/downloads)."""
    if not _rate_ok(_client_ip(request), _DL_HITS, limit=10, window=60):
        return JSONResponse({"error": "Too many files — please wait a minute."}, status_code=429)
    columns = list(dict.fromkeys(spec.columns))
    if not columns or any(c not in EXPORT_COLUMNS for c in columns):
        return JSONResponse({"error": "columns must be a non-empty subset of: "
                             + ", ".join(EXPORT_COLUMNS)}, status_code=400)

    def _int(v, lo=0, hi=2100):
        try:
            return max(lo, min(hi, int(v or 0)))
        except (TypeError, ValueError):
            return 0

    f = spec.filters if isinstance(spec.filters, dict) else {}
    if len(_dl_list()) >= MAX_DL_FILES:
        return JSONResponse({"error": f"Storage is full ({MAX_DL_FILES} files) — "
                             "delete some in the Downloads section first."}, status_code=429)
    fid = secrets.token_hex(8)
    meta = {"id": fid, "name": _safe_name(spec.name), "status": "queued", "columns": columns,
            "limit": max(1, min(DL_ROW_CAP, spec.limit)) if spec.limit else DL_ROW_CAP,
            "filters": {k: v for k, v in {
                "field": _int(f.get("field"), 0, 99),
                "country": re.sub(r"[^A-Za-z]", "", str(f.get("country") or ""))[:3].upper(),
                "q": str(f.get("q") or "")[:200],
                "y0": _int(f.get("y0")), "y1": _int(f.get("y1")),
                "status": f.get("status") if f.get("status") in ("granted", "application") else "",
                "ipc": re.sub(r"[^A-Za-z0-9]", "", str(f.get("ipc") or ""))[:20].upper(),
                "cpc": re.sub(r"[^A-Za-z0-9]", "", str(f.get("cpc") or ""))[:20].upper(),
            }.items() if v},
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (DOWNLOAD_DIR / f"{fid}.json").write_text(json.dumps(meta))
    _DL_JOBS.put(dict(meta))
    return meta


# ---- background CSV generation (single worker: serializes DB load) -------------------
_DL_JOBS = queue.SimpleQueue()


def _dl_write_batch(w, batch, columns, con):
    """Write one chunk of rows; resolves applicants for just these family_ids (the ANY() lookup
    costs ~1.5 ms/family on Neon's networked storage, so batching bounds each statement)."""
    owners = {}
    if "applicants" in columns:
        fam_ids = [r["family_id"] for r in batch]
        for r in con.execute(
            "SELECT family_id, name, country_code FROM family_assignee WHERE family_id = ANY(%s)",
            [fam_ids]):
            owners.setdefault(r["family_id"], []).append(f'{r["name"]} ({r["country_code"]})')
    for r in batch:
        w.writerow(["; ".join(owners.get(r["family_id"], [])) if c == "applicants"
                    else _iso(r[c]) if c in _DATE_COLS else r[c] for c in columns])
    return len(batch)


def _dl_run(job):
    fid = job["id"]
    metap = DOWNLOAD_DIR / f"{fid}.json"
    if not metap.exists():
        return                                    # deleted before the worker got to it
    m = _dl_meta(fid); m["status"] = "running"; metap.write_text(json.dumps(m))
    tmp = DOWNLOAD_DIR / f"{fid}.csv.tmp"
    con = None
    try:
        f = job["filters"]
        columns = job["columns"]
        sql_cols = [EXPORT_COLUMNS[c] for c in columns if c != "applicants"]
        if "applicants" in columns and "family_id" not in columns:
            sql_cols.append("f.family_id")        # keys the applicant aggregation
        join, where, params = base(f.get("field", 0), f.get("country", ""), f.get("q", ""),
                                   f.get("y0", 0), f.get("y1", 0), f.get("status", ""),
                                   f.get("ipc", ""), f.get("cpc", ""))
        if "n_forward_citations" in columns:
            join += " LEFT JOIN forward_citations fc ON fc.family_id = f.family_id"
        # NOT autocommit: one explicit transaction pins the Neon pooler backend, so SET LOCAL
        # sticks (role default is 35 s) and the server-side cursor stays valid between fetches.
        # No ORDER BY — sorting a multi-million-row candidate set blows any statement timeout;
        # analysis files don't need row order (she sorts in R/Stata/Excel).
        con = psycopg.connect(DSN, row_factory=dict_row, connect_timeout=30)
        con.execute("SET LOCAL statement_timeout = '180000'")
        cur = con.cursor(name=f"dl_{fid}")
        cur.itersize = 5000
        cur.execute(f"SELECT {', '.join(sql_cols)} FROM patent_family f {join} {where} "
                    f"LIMIT {job['limit']}", params)
        n = 0
        with open(tmp, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(columns)
            batch = []
            for row in cur:
                batch.append(row)
                if len(batch) >= 10_000:
                    n += _dl_write_batch(w, batch, columns, con)
                    batch.clear()
            if batch:
                n += _dl_write_batch(w, batch, columns, con)
        con.commit()
        con.close(); con = None
        if not metap.exists():                    # deleted while running
            tmp.unlink(missing_ok=True)
            return
        os.replace(tmp, DOWNLOAD_DIR / f"{fid}.csv")   # atomic: never a half-written CSV
        m = _dl_meta(fid) or {}
        m.update(status="done", rows=n,
                 finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        metap.write_text(json.dumps(m))
    except Exception:
        tmp.unlink(missing_ok=True)
        m = _dl_meta(fid)
        if m is not None:
            m.update(status="failed",
                     error="generation failed — try narrower filters or fewer columns")
            metap.write_text(json.dumps(m))
    finally:
        if con is not None:
            con.close()


def _dl_worker():
    while True:
        job = _DL_JOBS.get()
        try:
            _dl_run(job)
        except Exception:
            pass                                  # _dl_run records failures itself


def _dl_sweep_stale():
    """At (re)start: in-memory queue is empty, so any job still marked queued/running died with
    the last process — mark it failed and drop its partial file."""
    for p in DOWNLOAD_DIR.glob("*.json"):
        m = _dl_meta(p.stem)
        if m and m.get("status") in ("queued", "running"):
            m.update(status="failed", error="interrupted by a server restart — please re-create it")
            p.write_text(json.dumps(m))
    for t in DOWNLOAD_DIR.glob("*.csv.tmp"):
        t.unlink(missing_ok=True)


_dl_sweep_stale()
threading.Thread(target=_dl_worker, daemon=True).start()


@app.api_route("/api/downloads/{fid}/file", methods=["GET", "HEAD"])
def downloads_file(fid: str):
    if not _ID_RE.match(fid or ""):
        return _dl_not_found()
    p, m = DOWNLOAD_DIR / f"{fid}.csv", _dl_meta(fid)
    if not p.exists() or not m:
        return _dl_not_found()
    return FileResponse(p, media_type="text/csv", filename=m.get("name") or "patents.csv")


@app.patch("/api/downloads/{fid}")
def downloads_rename(fid: str, body: _DlRename):
    if not _ID_RE.match(fid or ""):
        return _dl_not_found()
    m = _dl_meta(fid)
    if not m:
        return _dl_not_found()
    m["name"] = _safe_name(body.name)
    (DOWNLOAD_DIR / f"{fid}.json").write_text(json.dumps(m))
    return m


@app.delete("/api/downloads/{fid}")
def downloads_delete(fid: str):
    if not _ID_RE.match(fid or ""):
        return _dl_not_found()
    paths = [DOWNLOAD_DIR / f"{fid}.csv", DOWNLOAD_DIR / f"{fid}.json",
             DOWNLOAD_DIR / f"{fid}.csv.tmp"]
    existed = any(p.exists() for p in paths)
    for p in paths:
        p.unlink(missing_ok=True)
    return {"ok": True} if existed else _dl_not_found()


CHAT_SYSTEM = """You are the built-in research assistant on the WIPO Patents website (wipo.b11.dev). \
You help a researcher (an economics master's student) explore a global patent dataset and connect to \
it from R and Stata. Be concise, warm, and practical. Prefer showing working, copy-paste-ready code.

THE DATASET — live, read-only PostgreSQL on Neon (schema: public):
Global patent families, 1930–2026, ~81.4 million families, built from Google Patents public data.
Tables:
- patent_family — one row per family. Columns: family_id (text, PK); rep_publication, rep_application; \
priority_date, filing_date, publication_date, grant_date (INTEGERS in YYYYMMDD form, e.g. 20230115); \
filing_year (int); granted (1 = granted patent, 0 = pending application); n_publications; \
n_bwd_citations; primary_field_name (one of the 35 WIPO technology fields); ipc_main; ipc_codes; \
cpc_codes (ipc/cpc codes are semicolon-joined text).
- family_assignee — (family_id, name, country_code): applicants/owners; a family may have several; \
country_code is ISO-2. (Only ~26% of families have a harmonized applicant country; pre-2000 is sparser.)
- family_class — (family_id, scheme ['ipc'|'cpc'], code): 4-char subclass, e.g. 'F21V'. Filter by \
prefix with an indexable range: code >= 'F21' AND code < 'F22' (or code LIKE 'F21%').
- family_country — (country_code, granted, filing_year, family_id): precomputed for fast country + \
year + status filtering and by-year aggregates.
- field — the 35 WIPO fields (field_number, field_name, sector_name).
- forward_citations — (family_id, n_forward_citations): FAMILY-LEVEL forward citations = number of \
distinct later patent families that cite this family, excluding self-citations (the standard \
innovation-impact metric). Join on family_id; families absent here have 0. n_backward_citations is \
already on patent_family; forward citations only exist in this table.
- country_stats, company_stats, year_stats, field_stats, class_index, meta_stats — precomputed aggregates.
A ready-made bulk file exists — granted US+EU patents 1930–present with forward citations, ~7.9M rows:
https://wipo.b11.dev/downloads/us_eu_granted_1930_present.csv.gz (327 MB gzipped). Point her there if \
she wants the whole granted set as one download rather than querying.
IMPORTANT: dates are integers YYYYMMDD. In R convert with as.Date(as.character(x), "%Y%m%d"). \
The DB is hosted in Singapore, so ALWAYS aggregate in SQL (GROUP BY / counts) and pull back summaries \
— never SELECT * the whole 81M rows.
COMMON TECH-AREA MAPPINGS (the 35 WIPO fields are broad; for specific tech use IPC/CPC codes): \
AI / machine learning ≈ CPC or IPC subclass 'G06N' (and the WIPO field is 'Computer technology'); \
semiconductors ≈ 'H01L'; batteries/energy storage ≈ 'H01M'; clean/green tech ≈ CPC 'Y02'; \
pharmaceuticals ≈ 'A61K'; lighting ≈ 'F21'. Do NOT invent a WIPO field name like 'Artificial \
Intelligence' — it doesn't exist; filter on the family_class code instead (fc.code >= 'G06N' AND fc.code < 'G06O').

CONNECTING (read-only login jannie_ro):
host = ep-cold-truth-aoi12n77-pooler.c-2.ap-southeast-1.aws.neon.tech ; database = neondb ; \
user = jannie_ro ; port = 5432 ; sslmode = require.
NEVER print a real password. Always use the placeholder <YOUR_PASSWORD> and tell her to paste her own \
jannie_ro password. R: use DBI + RPostgres. Stata: use the jdbc command with the PostgreSQL JDBC driver.

R connection template:
```r
library(DBI)
con <- dbConnect(RPostgres::Postgres(),
  host="ep-cold-truth-aoi12n77-pooler.c-2.ap-southeast-1.aws.neon.tech",
  dbname="neondb", user="jannie_ro", password="<YOUR_PASSWORD>", sslmode="require")
df <- dbGetQuery(con, "SELECT filing_year, SUM(granted) AS granted FROM family_country WHERE country_code='CN' GROUP BY filing_year ORDER BY filing_year")
```
Stata connection template:
```stata
jdbc connect, jar("postgresql-42.7.4.jar") driverclass("org.postgresql.Driver") ///
  url("jdbc:postgresql://ep-cold-truth-aoi12n77-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require") ///
  user("jannie_ro") password("<YOUR_PASSWORD>")
jdbc load, clear exec("SELECT filing_year, COUNT(*) AS n FROM patent_family GROUP BY filing_year ORDER BY filing_year")
```

THE WEBSITE FILTERS (left sidebar) — you can also guide her to use these instead of code:
WIPO technology field (35 fields); Patent status (granted / pending / all); Applicant country; \
Company contains (name search); IPC class and CPC class (type a prefix like F21 or Y02E); From/To year \
(1930–2026); then Apply. The chart toggles By year / By field / By country. Clicking a publication \
number opens a detail lightbox. There's a CSV download (capped at 50,000 rows). Applied filters are \
saved in the URL, so views are shareable/bookmarkable.

CREATING DOWNLOADABLE CSV FILES — when she asks for a CSV / spreadsheet / export / "data file" of \
something, you can create one directly: output ONE fenced block tagged csv containing ONLY a JSON object:
```csv
{"name": "ai_patents_china_2010s.csv", "limit": 50000,
 "columns": ["family_id", "publication_number", "filing_year", "granted", "wipo_field", "ipc_codes", "n_forward_citations", "applicants"],
 "filters": {"field": 0, "country": "CN", "q": "", "y0": 2010, "y1": 2019, "status": "", "ipc": "", "cpc": ""}}
```
Rules: "columns" must come ONLY from this list — family_id, publication_number, application_number, \
priority_date, filing_date, publication_date, grant_date, filing_year, granted, wipo_field_number, \
wipo_field, ipc_main, ipc_codes, cpc_codes, n_family_members, family_member_publications, \
n_backward_citations, n_forward_citations, applicants. "filters" mirror the website filters \
(field = WIPO field number 1–35 or 0; country = ISO-2 code; q = company name contains; y0/y1 = \
filing-year range, 0 = unbounded; status = "granted" | "application" | ""; ipc/cpc = class prefixes). \
Row cap is 500,000 — if she needs more, suggest splitting by year ranges or point her to the bulk file. \
Files are generated in the BACKGROUND: small ones finish in seconds; hundreds of thousands of rows can \
take a few minutes — the file shows "Preparing…" in the Downloads section until it's ready. Rows are \
NOT sorted (she can sort in R/Stata/Excel). The "applicants" column needs a per-family lookup and makes \
BIG exports much slower — suggest it for smaller sets. Emit at most ONE csv block per reply, only when \
she actually wants a file, and keep all prose OUTSIDE the block. The file appears in the "Downloads" \
section at the bottom of the page — tell her that. Do NOT emit a csv block for code she should run \
herself (R/Stata/SQL still go in normal code blocks).

HOW TO HELP:
- R/Stata questions → give copy-paste-ready code in ```r or ```stata fenced blocks, then a one-line note.
- "How do I see/find X" → suggest the website filters AND/OR a ready SQL/R query, whichever is easier.
- Keep it focused and correct; don't invent columns or tables beyond those listed above."""

# simple per-IP fixed-window rate limit to bound cost on these public endpoints
_CHAT_HITS = {}
_DL_HITS = {}


def _rate_ok(ip, bucket, limit=15, window=60):
    now = time.time()
    hits = [t for t in bucket.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        bucket[ip] = hits
        return False
    hits.append(now)
    bucket[ip] = hits
    return True


def _client_ip(request):
    return (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip() \
        or (request.client.host if request.client else "?")


@app.post("/api/chat")
async def chat(request: Request):
    if not FIREWORKS_KEY:
        return JSONResponse({"error": "assistant not configured"}, status_code=503)
    if not _rate_ok(_client_ip(request), _CHAT_HITS):
        return JSONResponse({"error": "Too many messages — please wait a minute."}, status_code=429)
    try:
        body = await request.json()
        incoming = body.get("messages", [])
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    # sanitise: keep only user/assistant turns, cap history + per-message length
    msgs = [{"role": m["role"], "content": str(m["content"])[:6000]}
            for m in incoming[-12:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs:
        return JSONResponse({"error": "no message"}, status_code=400)

    def gen():
        payload = json.dumps({
            "model": CHAT_MODEL,
            "messages": [{"role": "system", "content": CHAT_SYSTEM}] + msgs,
            "stream": True, "max_tokens": 1400, "temperature": 0.3,
        }).encode()
        req = urllib.request.Request(FIREWORKS_URL, data=payload, headers={
            "Authorization": f"Bearer {FIREWORKS_KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                    except Exception:
                        continue
                    if delta:
                        yield delta
        except Exception as e:
            yield f"\n\n_(assistant error: {type(e).__name__} — check the Fireworks key/credits.)_"

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.on_event("startup")
def _warm():
    # warm meta + default landing views in the background so the service is ready
    # immediately on (re)start; the first visitor during warm-up just computes live.
    def job():
        try:
            meta()
            for dim in ("year", "field", "country"):
                trend(dim=dim)
            patents()
        except Exception:
            pass

    threading.Thread(target=job, daemon=True).start()


# static frontend (mounted last so /api/* wins)
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
