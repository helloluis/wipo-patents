#!/usr/bin/env python3
"""
Parse the official WIPO IPC -> Technology-field concordance (Schmoch 2008, "Table 2")
into a clean rules CSV that the pipeline can join against BigQuery IPC codes.

Source text: data/concordance/wipo_table2_raw.txt  (extracted from the official WIPO PDF
  https://www.wipo.int/documents/2948119/3215563/wipo_ipc_technology.pdf  via `pdftotext -layout`)

Output: data/concordance/ipc_technology_bootstrap.csv with columns
  field_number, field_name, sector_number, sector_name, match_type, match_value, maingroup, note

This is the BOOTSTRAP concordance so the repo runs end-to-end immediately. The authoritative,
pre-resolved version is EPO PATSTAT's TLS901_TECHN_FIELD_IPC table -- see data/concordance/README.md
for how to export it and swap it in for the production run.

Matching model (applied later in concordance.py): for a given IPC code we derive its
subclass (4 chars, e.g. "H04N") and main group (int, e.g. 3). We pick the MOST SPECIFIC
matching rule:  maingroup (e.g. H04N-003)  >  subclass (e.g. H01B)  >  prefix3 (e.g. F21#).
That ordering reproduces the WIPO "(G06# not G06Q)" / "(G01N not G01N-033)" splits without
needing brittle exclusion logic, because the carved-out code is assigned to another field at
a more specific level (G06Q -> field 7, G01N-033 -> field 11).
"""
import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "concordance" / "wipo_table2_raw.txt"
OUT = ROOT / "data" / "concordance" / "ipc_technology_bootstrap.csv"

# Stable, canonical field + sector names (WIPO/Schmoch 35-field, 5-sector scheme).
SECTORS = {
    1: "Electrical engineering",
    2: "Instruments",
    3: "Chemistry",
    4: "Mechanical engineering",
    5: "Other fields",
}
# field_number -> (field_name, sector_number)
FIELDS = {
    1:  ("Electrical machinery, apparatus, energy", 1),
    2:  ("Audio-visual technology", 1),
    3:  ("Telecommunications", 1),
    4:  ("Digital communication", 1),
    5:  ("Basic communication processes", 1),
    6:  ("Computer technology", 1),
    7:  ("IT methods for management", 1),
    8:  ("Semiconductors", 1),
    9:  ("Optics", 2),
    10: ("Measurement", 2),
    11: ("Analysis of biological materials", 2),
    12: ("Control", 2),
    13: ("Medical technology", 2),
    14: ("Organic fine chemistry", 3),
    15: ("Biotechnology", 3),
    16: ("Pharmaceuticals", 3),
    17: ("Macromolecular chemistry, polymers", 3),
    18: ("Food chemistry", 3),
    19: ("Basic materials chemistry", 3),
    20: ("Materials, metallurgy", 3),
    21: ("Surface technology, coating", 3),
    22: ("Micro-structure and nano-technology", 3),
    23: ("Chemical engineering", 3),
    24: ("Environmental technology", 3),
    25: ("Handling", 4),
    26: ("Machine tools", 4),
    27: ("Engines, pumps, turbines", 4),
    28: ("Textile and paper machines", 4),
    29: ("Other special machines", 4),
    30: ("Thermal processes and apparatus", 4),
    31: ("Mechanical elements", 4),
    32: ("Transport", 4),
    33: ("Furniture, games", 5),
    34: ("Other consumer goods", 5),
    35: ("Civil engineering", 5),
}

# IPC token shapes appearing in the WIPO table:
#   F21#        -> 3-char subclass prefix (all subclasses starting F21)
#   H01B        -> a full 4-char subclass
#   H04N-003    -> subclass + main group 3
#   B01D-01##   -> subclass + subgroup range (bootstrap: treated as whole subclass; see note)
TOKEN_RE = re.compile(r"[A-H]\d{2}[A-Z]?(?:-\d+#*)?#?")


def parse_blocks(text: str):
    """Yield (field_number, block_text) for each of the 35 field rows."""
    # Field rows begin with the field number at the start of a line, e.g. "1 Electrical..."
    # Collect every line until the next field-number line (or end).
    lines = text.splitlines()
    starts = []  # (field_number, line_index)
    for i, ln in enumerate(lines):
        m = re.match(r"\s*(\d{1,2})\s+\S", ln)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 35 and (not starts or n == starts[-1][0] + 1):
                starts.append((n, i))
    for idx, (n, li) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        yield n, "\n".join(lines[li:end])


def classify(token: str):
    """Return (match_type, match_value, maingroup, note) for one IPC token."""
    note = ""
    if "#" in token and "-" not in token:
        # F21#, H02#, G06#  -> 3-char subclass prefix
        return "prefix3", token[:3], "", note
    if "-" in token:
        subclass, rest = token.split("-", 1)
        if "#" in rest:
            # subgroup range (e.g. B01D-01##, E01F-01#): bootstrap maps the whole subclass.
            note = "subgroup-range collapsed to subclass in bootstrap; PATSTAT TLS901 resolves exactly"
            return "subclass", subclass, "", note
        return "maingroup", subclass, str(int(rest)), note
    if len(token) == 4:
        return "subclass", token, "", note
    if len(token) == 3:
        return "prefix3", token, "", note
    return "subclass", token, "", "unexpected token shape"


def main():
    text = RAW.read_text(encoding="utf-8", errors="replace")
    rows = []
    seen_fields = set()
    for field_number, block in parse_blocks(text):
        seen_fields.add(field_number)
        field_name, sector_no = FIELDS[field_number]
        # The right column wraps, sometimes mid-token at a hyphen (e.g. "H04N-\n001").
        # Glue hyphen-line-breaks so split tokens like "H04N-001" reunite before tokenizing.
        block = re.sub(r"-\s*\n\s*", "-", block)
        # Walk tokens left-to-right, flipping an "exclude" flag on `not` and off on `)`.
        stream = block.replace("(", " ( ").replace(")", " ) ")
        negate = False
        seen_tok = set()
        for raw_tok in re.split(r"[\s,]+", stream):
            if raw_tok == "":
                continue
            low = raw_tok.lower()
            if low == "not":
                negate = True
                continue
            if raw_tok == ")":
                negate = False
                continue
            if raw_tok == "(":
                continue
            if not TOKEN_RE.fullmatch(raw_tok):
                continue  # field-name words, page numbers, etc.
            if negate:
                continue  # carved out here; assigned to another field at a finer level
            if raw_tok in seen_tok:
                continue
            seen_tok.add(raw_tok)
            mtype, mval, mgrp, note = classify(raw_tok)
            rows.append(
                dict(
                    field_number=field_number,
                    field_name=field_name,
                    sector_number=sector_no,
                    sector_name=SECTORS[sector_no],
                    match_type=mtype,
                    match_value=mval,
                    maingroup=mgrp,
                    note=note,
                    source_token=raw_tok,
                )
            )

    missing = set(FIELDS) - seen_fields
    if missing:
        raise SystemExit(f"ERROR: did not find field rows for {sorted(missing)} in {RAW}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "field_number", "field_name", "sector_number", "sector_name",
        "match_type", "match_value", "maingroup", "note", "source_token",
    ]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Sanity report
    by_field = {}
    for r in rows:
        by_field.setdefault(r["field_number"], 0)
        by_field[r["field_number"]] += 1
    print(f"Wrote {len(rows)} rules across {len(seen_fields)} fields -> {OUT}")
    print("Rules per field:")
    for n in sorted(by_field):
        print(f"  {n:>2} {FIELDS[n][0][:34]:<34} {by_field[n]:>3} rules")


if __name__ == "__main__":
    main()
