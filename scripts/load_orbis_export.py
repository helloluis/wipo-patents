#!/usr/bin/env python3
"""
Load an Orbis Intellectual Property patent-list export (.xlsx) into SQLite.

Handles the quirks of these exports:
  - The 'Results' sheet has a leading unnamed column holding the row number ("1.", "2." ...);
    a NEW patent starts wherever that cell is non-empty. Continuation rows carry alternate-
    language titles/abstracts for the SAME patent.
  - Date columns are Excel serials (days since 1899-12-30) -> converted to ISO dates.
  - openpyxl chokes on these files' stylesheet, so we parse the XLSX XML directly.

Usage:
  python scripts/load_orbis_export.py "Export 09_06_2026 23_10.xlsx" [more.xlsx ...] \
      --out data/orbis.sqlite
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EXCEL_EPOCH = date(1899, 12, 30)


def read_xlsx(path: Path):
    """Return {sheet_name: [ [cell, ...], ... ]} parsing XML directly (openpyxl-free)."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        t = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in t.findall(f"{NS}si"):
            shared.append("".join(n.text or "" for n in si.iter(f"{NS}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid2tgt = {r.get("Id"): r.get("Target") for r in rels}

    def cell_val(c):
        tp, v = c.get("t"), c.find(f"{NS}v")
        if tp == "s" and v is not None:
            return shared[int(v.text)]
        if tp == "inlineStr":
            isn = c.find(f"{NS}is")
            return "".join(n.text or "" for n in isn.iter(f"{NS}t")) if isn is not None else None
        return v.text if v is not None else None

    def col_idx(ref):  # "B12" -> 1 (zero-based column)
        letters = re.match(r"[A-Z]+", ref).group()
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    out = {}
    for s in wb.find(f"{NS}sheets"):
        name = s.get("name")
        tgt = rid2tgt[s.get(f"{RNS}id")]
        path_in = ("xl/" + tgt) if not tgt.startswith("/") else tgt[1:]
        data = ET.fromstring(z.read(path_in))
        rows = []
        for row in data.find(f"{NS}sheetData").findall(f"{NS}row"):
            cells = {}
            width = 0
            for c in row.findall(f"{NS}c"):
                ci = col_idx(c.get("r"))
                cells[ci] = cell_val(c)
                width = max(width, ci + 1)
            rows.append([cells.get(i) for i in range(width)])
        out[name] = rows
    return out


def sanitize(headers):
    seen, cols = {}, []
    for i, h in enumerate(headers):
        base = re.sub(r"[^0-9a-z]+", "_", (h or "").lower()).strip("_") or f"col_{i}"
        name = base
        k = seen.get(base, 0)
        while name in cols:
            k += 1
            name = f"{base}_{k}"
        seen[base] = k
        cols.append(name)
    return cols


def to_iso(serial):
    try:
        return (EXCEL_EPOCH + timedelta(days=int(float(serial)))).isoformat()
    except (TypeError, ValueError):
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS patent (
  publication_number TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS patent_text (
  publication_number TEXT, kind TEXT, language TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_pt_pub ON patent_text(publication_number);
"""


def ensure_columns(con, cols):
    existing = {r[1] for r in con.execute("PRAGMA table_info(patent)")}
    for c in cols:
        if c not in existing:
            con.execute(f'ALTER TABLE patent ADD COLUMN "{c}" TEXT')


def load_file(con, path: Path, stats):
    sheets = read_xlsx(path)
    results = sheets.get("Results")
    if not results:
        print(f"  ! {path.name}: no 'Results' sheet, skipping")
        return
    headers = sanitize(results[0])
    ensure_columns(con, headers)
    h = {name: i for i, name in enumerate(headers)}
    # Column groups
    date_cols = [c for c in headers if "date" in c and "language" not in c]
    seq_col = 0  # leading unnamed numbering column
    pub_col = h.get("publication_number")
    title_i, tlang_i = h.get("title"), h.get("title_language")
    abs_i, abslang_i = h.get("abstract"), h.get("abstract_language")

    def cell(row, i):
        return row[i] if (i is not None and i < len(row)) else None

    cur_pub = None
    for row in results[1:]:
        is_new = bool(cell(row, seq_col))
        if is_new:
            cur_pub = cell(row, pub_col)
            if not cur_pub:
                continue
            rec = {}
            for c in headers:
                val = cell(row, h[c])
                if c in date_cols:
                    iso = to_iso(val)
                    val = iso if iso else val
                rec[c] = val
            placeholders = ",".join("?" for _ in rec)
            con.execute(
                f'INSERT OR REPLACE INTO patent ({",".join(chr(34)+k+chr(34) for k in rec)}) VALUES ({placeholders})',
                list(rec.values()),
            )
            stats["patents"] += 1
        # capture title/abstract from this row (base or continuation) for the current patent
        if cur_pub:
            for kind, ti, li in (("title", title_i, tlang_i), ("abstract", abs_i, abslang_i)):
                txt = cell(row, ti)
                if txt:
                    con.execute(
                        "INSERT INTO patent_text VALUES (?,?,?,?)",
                        (cur_pub, kind, cell(row, li), txt),
                    )
                    stats["texts"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="data/orbis.sqlite")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out)
    con.executescript(SCHEMA)
    stats = {"patents": 0, "texts": 0}
    for f in args.files:
        print(f"Loading {f} ...")
        load_file(con, Path(f), stats)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM patent").fetchone()[0]
    print(f"\nDB {out}: {n:,} distinct patents, {stats['texts']:,} title/abstract rows")
    con.close()


if __name__ == "__main__":
    main()
