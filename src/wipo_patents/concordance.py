"""
IPC -> WIPO technology-field mapping.

Loads the concordance CSV (bootstrap from the WIPO PDF, or the authoritative PATSTAT
TLS901 export) and maps a raw IPC code to one or more of the 35 technology fields using
most-specific-match-wins: maingroup (H04N-003) > subclass (H01B) > 3-char prefix (F21#).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

# Raw IPC codes in patents-public-data look like "H04N  3/00" / "H04N0003/00" / "H04N 3/00".
# IPC structure is  SUBCLASS(4) + MAIN-GROUP + "/" + SUBGROUP, e.g. H04N 3/00 = subclass H04N,
# main group 3, subgroup 00. We must take the main group (the part BEFORE "/"), not merge it
# with the subgroup -- so we split on "/" rather than stripping it.
_SUBCLASS = re.compile(r"^([A-H]\d{2}[A-Z])")


def normalize_ipc(code: str):
    """Return (subclass, maingroup|None) or (None, None) if unparseable."""
    if not code:
        return None, None
    s = code.upper().replace(" ", "")
    m = _SUBCLASS.match(s)
    if not m:
        return None, None
    subclass = m.group(1)
    rest = s[len(subclass):].split("/", 1)[0]  # main-group portion, before any subgroup
    gm = re.match(r"0*(\d+)", rest)
    maingroup = int(gm.group(1)) if gm else None
    return subclass, maingroup


@dataclass
class Concordance:
    # subclass -> field_number
    by_subclass: dict = dc_field(default_factory=dict)
    # (subclass, maingroup) -> field_number
    by_maingroup: dict = dc_field(default_factory=dict)
    # 3-char prefix -> field_number
    by_prefix3: dict = dc_field(default_factory=dict)
    # field_number -> (field_name, sector_number, sector_name)
    fields: dict = dc_field(default_factory=dict)

    @classmethod
    def load(cls, csv_path: Path) -> "Concordance":
        c = cls()
        with Path(csv_path).open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                fn = int(r["field_number"])
                c.fields[fn] = (r["field_name"], int(r["sector_number"]), r["sector_name"])
                mt, mv = r["match_type"], r["match_value"]
                if mt == "subclass":
                    c.by_subclass[mv] = fn
                elif mt == "maingroup":
                    c.by_maingroup[(mv, int(r["maingroup"]))] = fn
                elif mt == "prefix3":
                    c.by_prefix3[mv] = fn
        if len(c.fields) != 35:
            raise ValueError(f"expected 35 fields, got {len(c.fields)} from {csv_path}")
        return c

    def field_for(self, ipc_code: str):
        """Most-specific-wins mapping of one IPC code to a field number (or None)."""
        subclass, maingroup = normalize_ipc(ipc_code)
        if subclass is None:
            return None
        if maingroup is not None and (subclass, maingroup) in self.by_maingroup:
            return self.by_maingroup[(subclass, maingroup)]
        if subclass in self.by_subclass:
            return self.by_subclass[subclass]
        return self.by_prefix3.get(subclass[:3])

    def fields_for(self, ipc_codes) -> set:
        """Distinct field numbers for a family's set of IPC codes."""
        out = set()
        for code in ipc_codes:
            fn = self.field_for(code)
            if fn is not None:
                out.add(fn)
        return out
