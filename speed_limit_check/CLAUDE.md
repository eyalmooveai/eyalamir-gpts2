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
  `BUCKET_LOCATION=US`, `INVOKER_EMAIL=eyal@moove.ai`.
