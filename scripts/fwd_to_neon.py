#!/usr/bin/env python3
"""
Compute FAMILY-LEVEL forward citations from BigQuery (patents-public-data.patents.publications)
and stream the per-family counts into Neon (public.forward_citations).

n_forward_citations(F) = number of DISTINCT citing patent families that cite any member of family F,
excluding self-citations (citing_family = F). This is the standard economics forward-citation measure.

  NEON_DSN=<owner> GOOGLE_APPLICATION_CREDENTIALS=/tmp/adc.json GOOGLE_CLOUD_PROJECT=... \
  python scripts/fwd_to_neon.py
"""
import os
import sys
import time

from google.cloud import bigquery, bigquery_storage
import psycopg

EU = ['AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','HU','IE','IT','LV','LT','LU','MT',
 'NL','PL','PT','RO','SK','SI','ES','SE','GB','CH','IS','LI','NO','AL','BA','ME','MK','RS','XK','RU',
 'UA','BY','MD','GE','AM','AZ','TR','AD','MC','SM','VA']
ORIGINS = "(" + ",".join(f"'{c}'" for c in (EU + ['US'])) + ")"

SQL = f"""
WITH pub AS (
  SELECT family_id, publication_number, citation, assignee_harmonized, priority_date, filing_date, grant_date
  FROM `patents-public-data.patents.publications`
  WHERE family_id IS NOT NULL AND family_id != '-1'
),
our AS (
  SELECT family_id FROM pub GROUP BY family_id
  HAVING LOGICAL_OR(grant_date>0)
     AND CAST(FLOOR(COALESCE(MIN(IF(priority_date>0,priority_date,NULL)),MIN(IF(filing_date>0,filing_date,NULL)))/10000) AS INT64) BETWEEN 1930 AND 2026
     AND LOGICAL_OR(EXISTS(SELECT 1 FROM UNNEST(assignee_harmonized) a WHERE a.country_code IN {ORIGINS}))
),
edges AS (
  SELECT family_id AS citing_family, c.publication_number AS cited_pub
  FROM pub, UNNEST(citation) c WHERE c.publication_number IS NOT NULL AND c.publication_number != ''
),
cited_map AS (
  SELECT publication_number, family_id FROM pub WHERE family_id IN (SELECT family_id FROM our)
)
SELECT cm.family_id AS family_id, COUNT(DISTINCT e.citing_family) AS n_forward_citations
FROM edges e JOIN cited_map cm ON cm.publication_number = e.cited_pub
WHERE e.citing_family != cm.family_id
GROUP BY cm.family_id
"""


def main():
    dsn = os.environ.get("NEON_DSN")
    if not dsn:
        sys.exit("error: NEON_DSN (owner) required")
    cli = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    print("Running BigQuery forward-citation graph query (billions of edges)...", flush=True)
    t0 = time.time()
    result = cli.query(SQL, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=60 * 1024**3)).result()
    print(f"  query done in {(time.time()-t0)/60:.1f} min; streaming to Neon...", flush=True)

    pg = psycopg.connect(dsn, autocommit=False)
    with pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.forward_citations;")
        cur.execute("CREATE TABLE public.forward_citations (family_id TEXT PRIMARY KEY, "
                    "n_forward_citations INTEGER);")
    pg.commit()

    bqs = bigquery_storage.BigQueryReadClient()
    n = 0
    for batch in result.to_arrow_iterable(bqstorage_client=bqs):
        c = batch.to_pydict()
        rows = list(zip(c["family_id"], c["n_forward_citations"]))
        with pg.cursor() as cur:
            with cur.copy("COPY public.forward_citations (family_id, n_forward_citations) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
        n += len(rows)
        if n % 500_000 < len(rows):
            print(f"    {n:,} families", flush=True)
    pg.commit()
    with pg.cursor() as cur:
        cur.execute("GRANT SELECT ON public.forward_citations TO app_ro, jannie_ro, wren_ro;")
        cur.execute("ANALYZE public.forward_citations;")
    pg.commit()
    pg.close()
    print(f"DONE: loaded {n:,} families with >=1 forward citation in {(time.time()-t0)/60:.1f} min.", flush=True)


if __name__ == "__main__":
    main()
