# Concordance: IPC → 35 WIPO technology fields

Two interchangeable sources. The pipeline reads whichever CSV you point `--concordance` at;
both produce the same columns (`field_number, field_name, sector_number, sector_name,
match_type, match_value, maingroup, ...`).

## 1. `ipc_technology_bootstrap.csv` — bootstrap (default, in repo)

Parsed from the official WIPO PDF (Schmoch 2008, "Concept of a Technology Classification for
Country Comparisons", Table 2) by `scripts/parse_concordance.py`. Regenerate with:

```bash
python3 scripts/parse_concordance.py
```

Tested mapping behaviour (see `scripts/`): the maingroup splits resolve correctly
(H04N audio-visual vs telecom, G06Q vs G06, G01N-033 vs G01N). **Known approximation:**
subgroup-range entries (the B01D split between Chemical engineering #23 and Environmental
technology #24) are collapsed to the whole subclass. Good enough to prove the workflow;
swap in the PATSTAT version below for thesis-grade numbers.

Source PDF: https://www.wipo.int/documents/2948119/3215563/wipo_ipc_technology.pdf

## 2. `ipc_technology_patstat.csv` — authoritative (recommended for the final run)

EPO PATSTAT ships the exact, pre-resolved concordance as table `TLS901_TECHN_FIELD_IPC`
(~750 rows). It's tiny — exporting it is well within the free-trial limits and is a good
first PATSTAT exercise. In PATSTAT Online's SQL editor:

```sql
SELECT techn_field_nr, techn_sector, techn_field, ipc_maingroup_symbol
FROM tls901_techn_field_ipc
ORDER BY techn_field_nr, ipc_maingroup_symbol;
```

Export to CSV, then convert to our column layout (a small adapter script can do this —
`ipc_maingroup_symbol` like `H04N   1/00` maps to match_type=maingroup, match_value=H04N,
maingroup=1). Then run the extract with `--concordance data/concordance/ipc_technology_patstat.csv`.
