# Working agreement: BigQuery (moove-platform-testing-data)

These rules apply to all sessions/agents working in this repo, whenever BigQuery
is involved (default project: `moove-platform-testing-data`, e.g. the
`calc` / `calc_out` / `calc_tmp` / `eyal_scratch` / `eyal_stage` datasets used
by the monthly speed-limits pipeline, `calc.monthly_pipeline(year, month)`).

- **Always report GB spent/queried.** After running any BigQuery command
  (via the BigQuery MCP tools or `bq`), summarize `totalBytesBilled` /
  `totalBytesProcessed` (converted to GB/MB) for the user.
- **Never drop or overwrite a table without asking first** — this includes
  `DROP TABLE`, `CREATE OR REPLACE TABLE`, `TRUNCATE`, or any `INSERT`/`UPDATE`/
  `DELETE` against an existing table. Always confirm with the user before
  executing any such statement.
- **Never write a stored procedure** (`CREATE PROCEDURE` / `CREATE OR REPLACE
  PROCEDURE`).
- **Never write a function** (`CREATE FUNCTION` / `CREATE OR REPLACE FUNCTION`,
  including JS or SQL UDFs).
- **When asked for code (SQL or otherwise), display it — do not execute it
  and do not save/deploy it into BigQuery** (no running it as a job beyond a
  safe read-only preview the user has asked for, no creating routines/tables
  from it) unless the user explicitly asks you to run/create it.
- **Always use implicit (unqualified) project references** in BigQuery code —
  write `dataset.table` / `dataset.routine`, not
  `moove-platform-testing-data.dataset.table` — so the same code can be
  deployed as-is into other projects (e.g. staging, production) by just
  changing the session/job's default project. Only qualify a reference with
  an explicit project name when the code genuinely must point at a different,
  specific project (e.g. `moove-archimedes-staging.bucket_in....`). This
  includes the project prefix on `CREATE PROCEDURE`/`CREATE FUNCTION`
  declaration lines themselves — leave those unqualified too, unless the
  routine must be deployed into a specific non-default project.
