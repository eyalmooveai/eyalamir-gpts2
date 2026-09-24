# speed_limit_check - working conventions

- This app is "Archimedes", MooveAI's data quality hub - one Flask app
  (`app.py`), one deployment, four pages: `/` (hub, model catalog),
  `/speed-limits` (Speed-Limits Quality - nationwide BigQuery metrics),
  `/sign-checker` (the original per-segment Street View sign checker),
  `/speed-limits-evaluator` (Speed-Limits Evaluator - batch/concurrent
  version of the sign checker, see below). Keep it this way - do not
  split into separate apps/deployments unless explicitly asked to (this
  was tried and explicitly reverted: "stop -- i want them all in one
  app, one deployment").
- **Speed-Limits Evaluator** (`/speed-limits-evaluator`,
  `batch_evaluator.py`): runs up to `segment_count` (default 1000)
  candidate segments for one state through the *same* per-segment logic
  the sign checker uses, concurrently (default 10 workers, user-settable)
  instead of one at a time. Built by extracting `run_pipeline`'s
  per-candidate loop body in `find_bad_speed_limit.py` into a standalone
  `_process_one_candidate()` - `run_pipeline` itself now just calls that
  once per candidate serially (behavior verified unchanged via a direct
  test - walk_all stop-at-match semantics, attempt ordering/statuses, all
  preserved), and `batch_evaluator.py` calls the same function from a
  `ThreadPoolExecutor`. Deliberately did NOT add a new rate limiter for
  this: `find_bad_speed_limit.py`'s Street View throttle
  (`_streetview_throttle_lock`, a process-global `threading.Lock`) already
  serializes every worker thread's Street View calls correctly regardless
  of concurrency - don't reintroduce per-thread-only throttling here, the
  existing global lock is what makes concurrency safe against Google's
  rate limits.
  - **Durability**: status/results are NOT just in-memory - they're
    written to `output/_batch_jobs/<batch_id>/status.json` (+ `results.csv`
    on completion, + an `index.json` listing every run) and mirrored to
    GCS the same way every other cache in this app is
    (`gcs_cache_pull`/`gcs_cache_push`). A status page always reads fresh
    from there, not from any in-memory job dict - that's what makes
    "leave and come back later" actually work. The persisted status
    includes the run's full `config` (criteria, walk/side/heading/fov
    settings) too, not just headline fields - needed for the "compare
    against a later run over the same streets" use case this was built
    for; don't strip that down to save space.
  - **NOT resumable across an instance restart** - if the one Cloud Run
    instance dies mid-batch (e.g. scaled to zero from idling), that
    batch's background thread dies with it; status.json is left at its
    last written snapshot, not silently continued elsewhere. Fixing the
    "idle instance gets recycled mid-batch" half of this needs
    `--min-instances=1` in `deploy.sh`/`gcloud run deploy` - **not
    currently added**, since it's a standing-cost commitment (an always-on
    instance) that wasn't explicitly signed off on. Ask before adding it,
    don't add it silently just because a batch run would benefit.
  - **Cancellation** is in-memory only (`app.py`'s `BATCH_CANCEL_EVENTS`,
    a `threading.Event` per running batch) - works for stopping a batch
    that's actually running in the current process; cannot stop one
    that's "running" only because the process that started it died
    without updating its status (same instance-restart gap as above).
  - **"Who ran it"** comes from the `X-Goog-Authenticated-User-Email`
    header IAP sets on every forwarded request (`app.py`'s
    `_requester_email()`) - reads as `"unknown"` for local dev, where
    there's no IAP in front. This only works because IAP is already live
    (see the access-model notes below) - don't build a separate
    auth/identity mechanism for this.
  - Images use the exact same per-segment `gcs_cache_push` calls as the
    single-run sign checker (same `_process_one_candidate` code, just
    called concurrently) - the post-run "sync to GCS" step
    (`batch_evaluator._sync_images_to_gcs`) is a best-effort sweep/safety
    net, not a second caching mechanism.
  - `Dockerfile`'s `COPY` line must include `batch_evaluator.py` - same
    "don't forget the new module" mistake this file already warns about
    for `quality_metrics.py`/`bq_cache.py`.
  - **The launch form's cost/time estimator learns from real run
    history, not a fixed formula** - it started as a pure guessed-formula
    estimate (positions/segment × headings × sides), which turned out to
    be badly wrong in practice (a real 10-segment NC run took >60s
    against a ~12s guess). Fixed by instrumenting actual usage:
    `find_bad_speed_limit.py` has process-wide, thread-safe counters
    (`reset_api_call_counts`/`get_api_call_counts`/`_count_api_call`) at
    the two real billed-call sites - inside `_throttle_streetview_call()`
    (every real Street View request, metadata+image alike - cache hits
    never reach it) and inside `_get_ocr_data()` right before the actual
    Vision call (same cache-hit exclusion). `run_batch` resets them
    before its ThreadPoolExecutor starts, captures them (plus real
    wall-clock `elapsed_seconds`) into `status.json` - both at the end
    AND periodically during the run, so a page you're watching shows
    real numbers climbing, not just a final tally. Don't remove this
    instrumentation or route around it with a new counting mechanism -
    it's the single source of truth `run_history_stats()` learns from.
  - `batch_evaluator.run_history_stats()` aggregates every completed
    run's real `elapsed_seconds`/call-counts into per-segment rates,
    bucketed by (walk_segment, side_mode, headings_count), plus a pooled
    "overall" bucket (key `walk_segment: None`) for configs with no exact
    match. Passed to `evaluator.html` as `history_stats` (JSON embedded
    in the page); its JS estimator prefers an exact config match, falls
    back to the overall bucket, and only uses the original hardcoded
    formula guess when `history_stats` is empty (no runs completed yet).
    This means the estimate genuinely gets more accurate the more the
    page is used - don't "fix" an estimate that looks off without first
    checking whether it's actually using history yet (the estimator box
    always says which basis it used).
  - **Per-segment results carry `functional_class` plus the segment's
    OSM/HERE/inferred/observed-average/freeflow speed values**, not just
    segment_id/status - `batch_evaluator._row_context()` pulls these from
    `CandidateAttempt.row` (or the raw candidate row for an `error`
    result, which has no `attempt`) into each `status["results"]` entry.
    `batch_evaluator.breakdown_by_functional_class()` groups those same
    results by `functional_class` (segments checked/matched/no-sign/
    no-coverage/errors/match rate per class) - computed on the fly from
    `results`, not persisted separately, so it can't drift out of sync
    with the results list it's derived from. `evaluator_status.html`
    mirrors this exact grouping logic in JS (`render()`'s `fcBuckets`
    block) so the live-polling view matches the server-rendered initial
    one - if `_row_context`'s field set ever changes, update both the
    Jinja results-table columns AND that JS block together.
  - **Every result (single sign-checker run and each Evaluator segment)
    records when Google captured the Street View image it's based on**
    (`find_bad_speed_limit.ImageDetail.capture_date`, "YYYY-MM" - from the
    Street View *metadata* response's own `date` field, read off the same
    metadata call `streetview_coverage()` already makes before fetching
    images at a position - not a second billed request, and not something
    derivable from the image bytes themselves). `batch_evaluator._image_capture_date(attempt, match)`
    picks the matched image's date when there's a match, else the first
    image tried for that segment - used by both `_build_csv_row`
    (`streetview_capture_date` column) and the per-segment `status["results"]`
    entries (`evaluator_status.html`'s Results table, another Jinja/JS
    pair to keep in sync - see above). `result.html` shows it both for
    the winning match and per-image in the lightbox. Can be blank/"unknown"
    - Google doesn't always return a capture date.
  - **Two hard caps, both enforced server-side (never trust the HTML
    form's `min`/`max` alone - those are just UX hints)**:
    `batch_evaluator.MAX_SEGMENT_COUNT` (1000) clamps `segment_count` in
    `app.py`'s `evaluator_start()` (`max(1, min(MAX_SEGMENT_COUNT, ...))`,
    same pattern as the existing `concurrency` clamp); and
    `batch_evaluator.DAILY_COST_CAP_USD` ($600/user/UTC-day) is enforced
    in two places - `daily_spend_for_user(email)` sums every batch
    `email` started today's real `actual_streetview_calls`/
    `actual_vision_calls` (completed AND currently-running runs alike,
    since a running batch's status.json carries live counts too), at
    `STREETVIEW_RATE_PER_1000`/`VISION_RATE_PER_1000`. `evaluator_start()`
    checks it synchronously before even creating a `BatchConfig` (refuses
    with a plain error page if already at/over cap), and `run_batch()`
    checks it again right before writing its own first status (defense
    against two submissions racing past the app.py check) AND
    periodically during the run (every 10 segments, same cadence as the
    existing status/usage writes) - the instant the *real* total for that
    user crosses the cap, it self-cancels (`cancel_event.set()`, distinct
    from a user-triggered Cancel via `status["hit_daily_cap"]`) rather
    than running to completion. This is a soft real-time stop, not an
    atomic one: workers already in flight when the cap is crossed still
    finish, so a single run can overshoot by up to
    `config.concurrency` segments' worth of cost - acceptable, same
    bound the Cancel button already has. Always computed from real
    measured usage, never an upfront estimate - the JS launch-form
    estimator (which now reads `STREETVIEW_RATE_PER_1000`/
    `VISION_RATE_PER_1000` from the template context instead of its own
    hardcoded copy, to avoid the two drifting apart) just warns if the
    estimate would exceed the user's remaining budget; it doesn't gate
    anything itself. `evaluator.html`'s launch page also shows the
    requester's spend-so-far-today next to the estimate.
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
- The Quality page's "Table" dropdown (`quality_metrics.list_evaluable_tables`,
  driven by `TABLE_SOURCES`) lists live tables/views across BOTH
  `calc_out` (matching `speed_limits_US*`) and `archimedes_api` (matching
  `speed_limits_infer*`) via `INFORMATION_SCHEMA.TABLES` per dataset -
  don't hardcode the table list, don't drop either dataset, and don't
  assume a fixed set of year/months exist. Confirmed live as of
  2026-09-18: `calc_out.speed_limits_US_2026_02`,
  `calc_out.speed_limits_US_2026_08`,
  `calc_out.speed_limits_US_2026_08_details`,
  `calc_out.speed_limits_US_latest`, `archimedes_api.speed_limits_infer`,
  `archimedes_api.speed_limits_infer_details` (the latter two are VIEWs
  over `calc_out.speed_limits_US_latest`, not base tables). Only
  `calc_out`'s `_details` variant has all six metrics' columns
  (`speed_limit_infer_mph_corrected`, `speed_limit_osm_mph`,
  `speed_limit_here_mph`, `speed_AVG_mph`, `freeflow_mph`) - every other
  option, `archimedes_api`'s two views included, is missing at least
  `speed_limit_here_mph`/`freeflow_mph` (and `_2026_02` is also missing
  `speed_limit_infer_mph_corrected`). Selecting one of those in the
  dropdown does NOT surface a raw BigQuery "Unrecognized name" error
  anymore - `quality_metrics.list_table_columns`/`missing_columns_report`
  check the selected table's actual columns (against every
  `QUALITY_METRICS` entry's `required_columns`, plus `infer_field` where
  used) synchronously, before starting the async metrics job, and
  `speed_limits_quality()` in app.py renders that as a friendly, amber
  `.badge.warn` explanation (`quality.html`'s `warning` branch) naming the
  missing column(s), the affected metric(s), and instructing the user to
  pick a different table - no query is even attempted in that case. Keep
  this in sync if a metric's SQL or `required_columns` changes: a metric
  referencing a new column without updating `required_columns` would let
  a bad table slip past this check and hit BigQuery's own error again. The
  dropdown's option value is `"<dataset>.<table>"` since the two sources
  span different datasets - `QualityFilters.dataset` is no longer
  hardcoded to `DEFAULT_DATASET` for this page, it comes from the selected
  option.
- The "inferred speed limit" column (`QualityFilters.infer_field`, what
  all four diff_* metrics compare against) also varies by table, same as
  the table list itself - don't hardcode
  `speed_limit_infer_mph_corrected`. Confirmed live as of 2026-09-23:
  `speed_limits_US_2026_02` has `speed_limit_infer_mph`/`_new2`/`_new3`
  but NOT `_corrected`; `speed_limits_US_2026_08`(`_details`) has
  `speed_limit_infer_mph`/`_corrected` but not `_new2`/`_new3`;
  `speed_limits_US_latest` has `_corrected`/`_new2`/`_new3` but not the
  bare `speed_limit_infer_mph`. `quality_metrics.list_infer_fields`
  discovers this live per selected table
  (`INFORMATION_SCHEMA.COLUMNS`, `column_name LIKE 'speed_limit_infer%'`)
  - the Quality page's "Inferred field being evaluated" dropdown reflects
  whatever the current table actually has, defaulting to
  `DEFAULT_INFER_FIELD` ("...mph_corrected") only when present.
- **Speed-Limits Quality is fully async now, not synchronous-at-page-load**:
  the page shell (filters, table/field dropdowns) renders immediately;
  the aggregate query (and breakdown query) run in a background thread
  (`app.py`'s `QUALITY_JOBS`, mirroring the sign checker's own `JOBS`
  pattern) with the browser polling `/speed-limits/status/<job_id>` then
  fetching `/speed-limits/fragment/<job_id>` once done - no full page
  reload. This was a direct fix for the page taking several seconds to
  load: don't revert to computing `fetch_quality_metrics`/breakdown
  synchronously inside the `speed_limits_quality()` route itself.
- **Every BigQuery call this page makes is cached** via `bq_cache.py`
  (`cache_key`/`cached_query`, disk + GCS, same mechanism
  `find_bad_speed_limit.py`'s `gcs_cache_pull`/`gcs_cache_push` already
  provide) - `fetch_quality_metrics` keys on the selected table's own
  `bigquery.Client.get_table(...).modified` timestamp (so a request only
  re-queries BigQuery once the table it reads has actually changed, not
  on every page load), while `list_evaluable_tables`/`list_infer_fields`
  use a flat 5-minute TTL (`SCHEMA_CACHE_TTL_SECONDS`) since there's no
  single "modified" signal for a schema-shaped listing. `bq_cache.py` is
  a new module `Dockerfile`'s `COPY` line must include - don't forget it
  the way `quality_metrics.py` itself was once nearly forgotten there.

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
- **Access model: IAP, live and confirmed working** at
  `https://archimedes.moove.ai`, open to `domain:moove.ai` (everyone at
  the company) - confirmed 2026-09: instant access from an already-signed-in
  Chrome session, a Google sign-in prompt in incognito, and a correct
  "you don't have access" for a personal (non-moove.ai) Google account.
  Devops set this up **simpler than the manual load-balancer/Serverless-NEG
  runbook this file used to describe**: Cloud Run's native `--iap`
  integration, no separate static IP/DNS/managed-cert/NEG/backend-service
  chain needed for a single Cloud Run service. Don't reintroduce
  `--no-allow-unauthenticated` in `deploy.sh` - it would 403 the
  setIamPolicy call IAP itself now owns and abort the script under
  `set -euo pipefail`. Who's actually let in is controlled by
  `roles/iap.httpsResourceAccessor` on the service, not `roles/run.invoker` -
  `gcloud run services proxy` and the direct `.run.app` URL are the
  *pre-IAP* story, not how this is used day to day anymore (`deploy.sh`
  still offers an optional `INVOKER_EMAIL`/`run.invoker` grant as a
  fallback path, but it isn't what gates access once IAP is on).
  **`--iap` itself is alpha-track only as of this writing** (stable and
  beta both reject it) - explicitly decided NOT to use alpha/beta for
  this deploy (not vetted enough), so `deploy.sh`'s `gcloud run deploy`
  is plain stable-track with no `--iap` flag at all, on the theory that
  IAP is a persistent service-level setting a plain deploy doesn't reset
  (same as `--ingress` or other settings it isn't explicitly passed) -
  this is a reasonable but *unverified* assumption, not confirmed via
  gcloud docs or testing, so double-check `archimedes.moove.ai` still
  prompts sign-in after every deploy rather than trusting this blindly.
  If IAP ever needs to be (re-)enabled and the Console's "Security" tab
  isn't used instead, that still requires alpha - there's currently no
  stable-track way to do it from this script.
- **Terraform now owns this project's IAM grants** - bigquery.dataViewer,
  bigquery.jobUser, storage.objectAdmin on the cache bucket,
  secretmanager.secretAccessor, and run.invoker for INVOKER_EMAIL. Run
  `deploy.sh` for this project with `SKIP_IAM_GRANTS=1` going forward -
  without it, the script re-attempts grants Terraform already owns
  (harmless/additive, but noisy: it'll report them as failures needing an
  admin when they're actually already in place via Terraform).
  `deploy.sh` also had a bad role name fixed: `roles/cloudvision.user`
  doesn't exist (Vision ships no `roles/cloudvision.*` predefined role at
  all) - it's `roles/serviceusage.serviceUsageConsumer` now (Vision API
  calls are gated on `serviceusage.services.use` against the quota
  project).
