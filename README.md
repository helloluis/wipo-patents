# wipo-patents

A public web app + data pipeline for analysing **European patent activity by WIPO technology
field**, for a UCL SSEES dissertation. Live at **wipo.b11.dev**.

The dataset: **2.32M granted European patent families, 2000–2026**, sourced free from
[Google Patents public data](https://cloud.google.com/blog/topics/public-datasets/google-patents-public-datasets-connecting-public-paid-and-private-patent-data)
on BigQuery, classified into the **35 WIPO technology fields**, with harmonized **company names
and countries**. Query it by field, country, company, and year, with charts and CSV export.

## Quickstart (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# get the DB: rebuild it (below) or download the release asset into data/europe.sqlite
WIPO_DB=data/europe.sqlite uvicorn app.main:app --port 8000
open http://127.0.0.1:8000
```

Deploying to the VPS: see [DEPLOY.md](DEPLOY.md).

## Data strategy (why two sources)

The dissertation links **company financials to patenting**. That has two layers:

- **Patents + company names + WIPO fields + dates + citations** → Google BigQuery, free and
  unlimited. This is what powers the app (`scripts/extract_europe.py` → `data/europe.sqlite`).
- **BvD company IDs, ultimate ownership, valuation scores, financials** → **Orbis IP** only
  (Moody's/Bureau van Dijk, via UCL). Not in BigQuery. Orbis's web export is capped at 100
  rows/file, so bulk firm-financial data comes via a UCL-library/BvD request; we ingest those
  exports with `scripts/load_orbis_export.py`. The two layers join by company name.

Known caveats baked into the data: it's **granted-only** (recent filing years taper as patents
await grant); **company-name harmonization is imperfect** (e.g. a firm appearing with and without
a country code — matters for the Orbis name-join); prolific **individual inventors** are present
(and would be excluded by Orbis's firm-financial filter); `family_assignee` keeps non-European
co-applicants on European families.

## Repository

```
app/                     FastAPI app + static frontend (the wipo.b11.dev UI)
scripts/
  extract_europe.py      BigQuery → data/europe.sqlite (the app's dataset)
  load_orbis_export.py   Orbis IP .xlsx export → data/orbis.sqlite
  parse_concordance.py   WIPO IPC→35-field concordance (official PDF → CSV)
src/wipo_patents/        reusable pipeline lib (concordance, BigQuery, schema)
sql/                     BigQuery query templates (inspect / count / extract)
data/concordance/        the IPC→WIPO-field concordance (versioned; DB files are gitignored)
```

## Reproducing the dataset

```bash
# needs a Google Cloud project (BigQuery free tier covers it; ~53 GiB scanned)
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project-id
python scripts/extract_europe.py --out data/europe.sqlite      # ~15 min, ~1.1 GB
```

The SQLite file is intentionally **not** committed (see `.gitignore`); it's preserved as a
release asset and is fully reproducible from the script above.
