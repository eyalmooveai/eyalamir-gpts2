# speed_limit_check - working conventions

- This app is "Archimedes", MooveAI's data quality hub - one Flask app
  (`app.py`), one deployment, three pages: `/` (hub, model catalog),
  `/speed-limits` (Speed-Limits Quality - nationwide BigQuery metrics),
  `/sign-checker` (the original per-segment Street View sign checker,
  linked from the Quality page). Keep it this way - do not split into
  separate apps/deployments unless explicitly asked to (this was tried
  and explicitly reverted: "stop -- i want them all in one app, one
  deployment").
- The hub's model catalog (`HUB_MODELS` in `app.py`) is a curated list of
  MooveAI's model products (Speed Limits, Lanes, Construction Zones,
  Accident Prediction, Accident Detection), not BigQuery ML models -
  there are currently zero actual BQML models in this project. Only
  "Speed Limits" has a built tool; the rest are intentional placeholders.
- Nationwide speed-limit data lives in
  `calc_out.speed_limits_US_<YEAR>_<MONTH>_details` (confirmed to exist
  for 2026-08, same schema as the per-state `_details` tables). Confirmed
  column names differ from casual naming: it's `speed_AVG_mph` (not
  `speed_avg`) and `freeflow_mph` (not `freeflow_speed_avg`); `state` and
  `functional_class` are real columns, usable directly for filtering.
  There's also an `archimedes_api` dataset with `speed_limits_infer(_details)`
  views over `calc_out.speed_limits_US_latest` - those don't select
  `speed_limit_here_mph`/`freeflow_mph`, which the Quality page needs, so
  it queries the dated `_details` table directly instead.
- The Quality page's "Table" dropdown (`quality_metrics.list_speed_limits_us_tables`)
  lists live `calc_out` tables matching `speed_limits_US*` via
  `INFORMATION_SCHEMA.TABLES` - don't hardcode the table list or assume a
  fixed set of year/months exist. Confirmed live as of 2026-09-18:
  `speed_limits_US_2026_02`, `speed_limits_US_2026_08`,
  `speed_limits_US_2026_08_details`, `speed_limits_US_latest`. Only the
  `_details` variant has all six metrics' columns
  (`speed_limit_infer_mph_corrected`, `speed_limit_osm_mph`,
  `speed_limit_here_mph`, `speed_AVG_mph`, `freeflow_mph`) - the bare
  `_<YEAR>_<MONTH>` and `_latest` tables are each missing at least
  `speed_limit_here_mph`/`freeflow_mph` (and `_2026_02` is also missing
  `speed_limit_infer_mph_corrected`). Selecting one of those in the
  dropdown is expected to surface a BigQuery "Unrecognized name" error in
  the page's error card - this is normal, not a bug to fix.

- `~/Claude/MooveAI/` already exists on the machine this is worked on and
  is where all local checkouts/deployments of this repo live - the repo
  is checked out at `~/Claude/MooveAI/eyalamir-gpts2/`, with
  `~/Claude/MooveAI/keys.env` sitting as a sibling one level above it
  (matches `DEFAULT_KEYS_FILE` in `find_bad_speed_limit.py` and
  `keys.env.example`). Don't `mkdir` it or suggest cloning fresh - assume
  it's already there and just `cd ~/Claude/MooveAI/eyalamir-gpts2` (not
  `~/eyalamir-gpts2` or any other location), then `git pull`.
- Cloud Run deployment: `speed_limit_check/deploy.sh` (see its header and
  README.md's "Deploying to Cloud Run" section). Known values already used
  for this project's deployment: `PROJECT_ID=moove-platform-testing-data`,
  `REGION=us-central1`, `BUCKET_NAME=archimedes-control`,
  `BUCKET_LOCATION=US`, `INVOKER_EMAIL=eyal@moove.ai`. Deployed service
  URL: `https://speed-limit-check-233134271134.us-central1.run.app`.
- Access model: Cloud Run's own IAM auth (`--no-allow-unauthenticated`),
  used via `gcloud run services proxy speed-limit-check --region
  us-central1` (needs `roles/run.invoker` granted per user on the
  service, a separate grant from the runtime service account's own
  roles - same "different resource, different setIamPolicy permission"
  gap `deploy.sh` already handles for the service account). A real
  browser-clickable URL with Google sign-in would need Identity-Aware
  Proxy behind a full HTTPS Load Balancer (reserved static IP, managed
  SSL cert needing a real domain, Serverless NEG, backend service,
  forwarding rule) - explicitly deferred as not worth the added
  infrastructure/cost for now, even though a domain is available for it
  ("open-web") if this gets revisited later. Don't build the load
  balancer/IAP setup unless asked again.
