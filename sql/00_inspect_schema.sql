-- Step 0: confirm the live schema/format before the big extract.
-- Cheap (LIMIT 20). Lets us verify how IPC codes and harmonized countries are formatted
-- in patents-public-data so the Python normalizer in concordance.py matches them.
SELECT
  publication_number,
  family_id,
  filing_date,
  ARRAY(SELECT code FROM UNNEST(ipc)) AS ipc_codes,
  ARRAY(SELECT AS STRUCT name, country_code FROM UNNEST(assignee_harmonized)) AS assignees,
  ARRAY(SELECT AS STRUCT name, country_code FROM UNNEST(inventor_harmonized)) AS inventors
FROM `patents-public-data.patents.publications`
WHERE family_id != '-1' AND ARRAY_LENGTH(ipc) > 0
LIMIT 20;
