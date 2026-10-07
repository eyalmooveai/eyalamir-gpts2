# speed_limit_check - working conventions

- This app is "Archimedes", MooveAI's platform for data science on
  movement data - a window into how MooveAI's road-intelligence models
  work, their results, and their performance (not just a "quality
  checker" - "checking quality vs. ground truth" is one specific lens
  this platform gives you into a model, not the whole point of it).
  One Flask app (`app.py`), one deployment, seven pages: `/` (hub, the
  model catalog - just that, see the "Standard site chrome" bullet's
  Explore note below for why it's deliberately NOT also the Explore
  page), `/explore` (the live sample map), `/speed-limits` (Speed-Limits
  Quality - nationwide BigQuery metrics), `/sign-checker` (the original
  per-segment Street View sign checker), `/speed-limits-evaluator`
  (Speed-Limits Evaluator - batch/concurrent version of the sign
  checker, see below), `/agent` (the standalone "Ask Archimedes" AI
  agent page), `/deploy` (a static deployment-info reference page). Keep
  it this way - do not split into separate apps/deployments unless
  explicitly asked to (this was tried and explicitly reverted: "stop --
  i want them all in one app, one deployment").
- **Branding: a `static/` folder (Flask's default static route, no extra
  route code needed) holds two real brand assets, both committed to git
  as regular files, not generated at request time.**
  - `static/favicon.svg` (+ `favicon-32.png`/`apple-touch-icon.png`
    fallbacks) is Moove's own ring/infinity mark - the path data was
    extracted verbatim from the company's official logo file
    (`MooveAI LOGO SVG.svg`, pulled from Drive), just the icon without
    the wordmark text, in Moove's brand teal `#00b5ad`. Wired into every
    page via `templates/layout.html`'s `<head>` (`<link rel="icon">`
    etc.) - don't invent a different mark or guess at Moove's colors;
    if this ever needs regenerating, re-extract from the official Drive
    asset rather than approximating by eye.
  - `static/archimedes-mark.svg` is a deliberate MELD of the two marks,
    not just a matching color: it's Moove's own two-ring path (the same
    one `favicon.svg` uses, kept whole and unaltered on the left) with
    its right ring's hole filled in as a disc carrying an Archimedean
    spiral (r = a + b*theta, generated parametrically - see the file's
    inline comment) in white - one ring reads as Moove's, the other as
    Archimedes' own. The two ring centers were measured empirically
    (pixel-centroid analysis of the rendered favicon, not hand-derived
    from the path's bezier numbers) before placing the spiral. Its
    natural aspect ratio is ~1.75:1 (wide), not square - `.hero-mark` in
    this same file is `height: 60px; width: auto` for that reason; don't
    force it back to a fixed square box without re-checking it doesn't
    squish. Used in the hub page's hero and nowhere else yet. The plain
    Moove ring mark (`favicon.svg`) is used everywhere else branding
    appears (the tab icon, the footer) - only the hub hero's own logo is
    the melded one, since that's specifically Archimedes' own mark, not
    a second favicon.
  - Both PNG fallbacks were rasterized once via headless Chromium
    (Playwright, `executablePath: '/opt/pw-browsers/chromium'`) and
    committed as static files - there's no server-side SVG-to-PNG
    rendering at runtime, so regenerating them (if the SVGs ever change)
    is a one-off manual step, not something `app.py` does.
  - `static/models/*.svg` (`speed_limits.svg`, `lanes.svg`,
    `construction_zones.svg`, `accident_prediction.svg`,
    `accident_detection.svg`) - one illustration per `HUB_MODELS` entry,
    shown as a banner image on its hub card (`.model-banner-img`,
    `object-fit: cover` at a fixed height) via the `icon` key. **These are
    hand-authored SVG scenes, not photos or model-generated images** -
    this environment has no image-generation tool (no DALL-E/Imagen
    access), and a Drive search for real photography Moove already owned
    turned up nothing usable (only logos, screenshots, and academic
    chart exports - see the session that added these for the full
    search). Asked to make them "real-world images (generated)" and
    then, once that was confirmed infeasible here, told to "upgrade the
    icons" instead - so don't mistake these for a placeholder that still
    needs replacing with something more real; richer illustration is the
    agreed-on outcome for now. Each one shares one visual language (sky
    gradient + rolling hill + a perspective road built from a shared
    `base_scene()`-style helper, receding dashed center line, a small
    consistent color palette - asphalt gray, grass green, Moove teal,
    warning orange/yellow) with a literal scene depicting its model: a
    "SPEED 55" sign on a post for Speed Limits, three cars in distinct
    lanes for Lanes, cones + a striped barrier + a diamond warning sign
    for Construction Zones, a car with radar-style forecast arcs and a
    highlighted risk patch ahead for Accident Prediction, two collided
    cars with an impact starburst for Accident Detection. Keep both
    patterns - the shared background language AND the one-scene-per-model
    literalism - if a new model is ever added, rather than reusing a
    generic placeholder. `.model-card.placeholder .model-banner-img`
    desaturates+dims the "coming soon" ones via CSS
    (`filter: grayscale(45%); opacity: 0.75`) rather than needing a
    second muted copy of each asset. If you regenerate one of these,
    watch out for SVG elliptical-arc sweep-flag direction (`A rx ry 0
    large-arc-flag sweep-flag x y`) - an earlier (now-replaced) version
    of the Speed Limits glyph's gauge arc first came out drawing the
    wrong (minor) arc through the bottom of the dial because of this,
    caught by actually rendering it (headless Chromium screenshot) and
    looking, not by reasoning about the path text - the same lesson
    applies to any new arc math in these scenes.
- **Standard site chrome - top nav, footer, account avatar - all live in
  `layout.html`'s `<body>`, once, so every page gets the same one
  automatically (no per-template copy to keep in sync).**
  - **Top nav**: 5 links - Quality, Explore, Agent, Deploy, Run - each a
    literal 1:1 page name/route (`/speed-limits`, `/explore`, `/agent`,
    `/deploy`, `/speed-limits-evaluator`). **`/` (the hub) is
    deliberately NOT one of the five nav targets and carries no active
    nav state of its own** - it's reached via the brand mark/wordmark at
    the far left of the nav, same as any conventional "logo goes home"
    pattern, not via a tab. This was explicitly corrected once already:
    Explore's live sample map briefly lived at the top of `/` itself,
    and got moved out to its own `/explore` page because "the main page
    should not have explore, it should have the models" - don't
    re-merge them; `/` (`hub()`, `hub.html`) stays just the model
    catalog, `/explore` (`explore_page()`, `explore.html`) stays just the
    sample map (+ a couple of "more ways to explore" links). Active-state
    highlighting for all five is server-side (`request.path == '/...'`
    in `layout.html`) - a plain per-page match, no client-side hash
    detection needed since none of the five nav targets are anchors
    anymore. If Sign Checker (`/sign-checker`) ever needs its own
    top-level nav slot, that's a 6th item, not a repurposing of one of
    these five - the user asked for exactly these five.
  - **`/agent`** (`app.py`'s `agent_page()`, `templates/agent.html`) is a
    standalone home for the Custom test/"Ask Archimedes" AI-agent box
    (see custom_metrics.py) - NOT a duplicate implementation. The box's
    markup+JS live once, in `templates/_ask_archimedes.html`, and both
    `quality.html` and `agent.html` `{% include %}` it - the user
    explicitly asked to keep the box on the Quality page too when adding
    the standalone page, so both stay, sharing one implementation rather
    than forking into two copies that could drift. The include is
    self-contained (its own `escapeHtml`, its own `#ask-archimedes-state-list`
    datalist so it doesn't collide with `quality.html`'s own `#state-list`)
    and needs only a `form#quality-filters-form` containing a
    `select#table` somewhere on the including page - `agent.html`'s own
    "Data source" card supplies a minimal one (just a table picker; the
    fuller Quality page's states/fc/zip/county filters aren't required
    for a comparison to validate, they only narrow which segments count).
    A table is required even for a pure Q&A question, since
    `/speed-limits/custom-test(/sample)` needs one to fetch
    `available_columns` to validate any comparison against - there's no
    page-independent default. `app.py`'s `_table_options_and_default()`
    (used by the Quality page, `/agent`, and the hub's sample map alike)
    factors out that "pick the live default table, live-list every option"
    logic - don't re-duplicate it a fourth time if another page ever
    needs a table picker. **`agent.html`'s card order is Ask Archimedes
    first, Data source second** ("put 'Ask Archimedes' as the top thing to
    do in the page" was explicit) - the opposite of the original build
    order, where Data source came first since the box technically depends
    on it. That dependency is fine either way: `_ask_archimedes.html`'s
    own JS only touches `#table`/`#quality-filters-form` from inside
    functions (`runCustomTest()` etc.), never at script-parse time, so
    which card the browser lays out first doesn't matter - don't assume
    the Data source card has to precede the include in the template just
    because it's referenced by ID.
  - **`/deploy`** (`app.py`'s `deploy_info()`, `templates/deploy_info.html`)
    is a deliberately static reference page - real, already-documented
    facts (service name/project/region/bucket, the IAP access model, the
    `deploy.sh` invocation) restated for in-app visibility, NOT a live
    status dashboard. This app has no wired-up Cloud Run/GCP Admin API
    calls to report real revision/traffic/health, and the page says so
    explicitly ("not a live dashboard") rather than implying it's
    monitoring anything - don't quietly turn this into something that
    looks live without actually wiring up real monitoring calls behind
    it, that would be misleading.
  - **Account avatar** (top right of the nav, every page): a generated
    initials-on-a-color-disc avatar, never a real profile photo - IAP
    (see `_requester_email()`) only ever forwards the signed-in user's
    *email* via `X-Goog-Authenticated-User-Email`, never a picture, so
    there is no real photo this app could show even if it tried; showing
    a generated avatar is the honest choice, not a placeholder for a
    "real" image to add later. `app.py`'s `_user_initials()`/
    `_user_avatar_color()` derive both deterministically from the email
    (a fixed palette, hashed - same person always gets the same color,
    not a new random one per page load) and are injected into every
    template via the `_inject_nav_context()` context processor, alongside
    `current_year` for the footer's copyright line - add any future
    "every page needs this" value there rather than passing it through
    every individual `render_template()` call. Local dev (no IAP) shows a
    plain "?" avatar and a "not signed in" note instead of a broken
    email/sign-out link - checked via `nav_user_signed_in`. "Sign out"
    links to IAP's own standard `/_gcp_iap/clear_login_cookie` path (a
    documented Google convention, not a guessed URL) - there's no
    app-level session/login of its own to sign out of, IAP owns all of
    that.
  - **Footer**: brand + `moove.ai`/Explore/Deploy links + a copyright
    line, same on every page. Kept deliberately modest (no fabricated
    Privacy/Terms/social-links rows) - this is an internal tool without
    public-facing policy pages, so a marketing-site-style footer would
    just be inventing links to pages that don't exist.
  - `layout.html` also carries a `--moove-teal` CSS variable kept
    deliberately separate from `--accent` (this app's own
    long-established UI blue, used for every button/link) - brand
    touches use teal, the functional UI didn't get re-themed. (The
    footer itself is documented under "Standard site chrome" above - it
    grew from this one-liner into a fuller brand+links+copyright row.)
  - `Dockerfile`'s `COPY` line needs `static/` - same "don't forget the
    new thing" mistake this file already warns about for new Python
    modules.
  - The hub page (`hub.html`) is the one page with a custom
    `{% block header %}` override (a hero: mark + title + tagline +
    one-paragraph description) instead of the plain
    breadcrumb/h1/subtitle every other page gets from `layout.html`'s
    default `header` block - if hub.html's hero ever needs to change,
    edit its own `header` block, not `layout.html`'s default (which every
    other page still uses as-is).
  - **`/explore` is a live sample map (its own top card, "Live sample")
    with a filters card underneath it ("Pick what to explore")**, not a
    screenshot, a static illustration, or a map fixed to "vs. OSM,
    nationwide". It started as the latter ("put an initial
    OSM-comparison sample map at the top of the [Explore] page", and
    when a first version put this on the hub page instead, explicitly
    corrected - see the Top nav bullet above), was then generalized to
    take filters ("Explore should let me change what to present/explore
    with the same map visualization"), and finally had those two split
    into separate cards with the map first ("reverse order -- map on
    top, pick what to explore underneath") - keep the map card first in
    `explore.html`, the filters card second; don't re-merge them into
    one card or flip the order back. `#explore-filters-form`
    (table/metric/states/mismatch-threshold, same `method="get" action=
    "/explore"` full-page-reload pattern as the Quality page's own main
    filters form, not a client-side-only control) submits back to
    `explore_page()` in `app.py`, which validates each param
    (`metric` against `QUALITY_METRIC_KEYS`, falling back to `diff_osm`
    on anything unrecognized rather than 500ing; `param1` parsed as a
    float then coerced back to a bare int like `10` instead of `10.0`
    when it has no fractional part, for a clean number-input display)
    and re-derives `sample_map_infer_field` for *whichever* table is
    selected via the same `list_infer_fields()` call the Quality page
    uses - don't let this get stuck reusing the previous table's infer
    field after a table switch. `table_options`/`selected_table_option`
    still come from the same `_table_options_and_default()` helper the
    Quality page and `/agent` also use. `explore.html`'s inline script
    auto-fetches `/speed-limits/sample-mismatches` on page load using
    whatever table/infer_field/metric/states/param1 the form just
    submitted (no separate button - it's meant to already be there when
    you arrive, same as before) - the exact same endpoint and
    Leaflet-marker-drawing logic as the Quality page's own "Map: where
    the worst mismatches are" issue map, just reading its query params
    from the page's own filters instead of hardcoded nationwide/`diff_osm`
    defaults. The status message and every marker popup pull the
    metric's display name from the `<select>`'s own selected `<option>`
    text (not a second hardcoded metric-name lookup) and show the
    selected states or "nationwide" when left blank, so "vs. OSM" isn't
    baked into the copy anymore either. If the table/infer_field lookup
    fails (e.g. no BigQuery credentials in local dev), `explore_page()`
    still catches it and passes `sample_map_error` through - the card
    shows that message instead of attempting a doomed fetch, never a raw
    500. Verified with a stubbed-Leaflet Node/Playwright test (this
    sandbox has no network access to the real Leaflet CDN) confirming
    markers actually get drawn and popups are populated correctly with
    the dynamic metric/states text, not just that the card renders.
- **`speed_limit_here_mph` is always wrapped in `ROUND()`, everywhere it's
  referenced.** It's stored as a precise km/h->mph conversion (e.g.
  `24.860161591050343`, confirmed live - not `25`), unlike every other
  speed column here (OSM's, the inferred ones, observed avg, freeflow),
  which are clean values. Left unrounded, that conversion noise reads as
  spurious mismatch magnitude anywhere it's compared, and shows up as an
  ugly non-round number anywhere it's displayed. Fixed at the SQL level,
  not just for display, since the noise was real error in the actual
  selection/mismatch math, not just cosmetic: `find_bad_speed_limit.CRITERIA_DEFS`'s
  `here_osm_agree`/`infer_vs_here`/`infer_corrected_vs_here` entries (sql
  and, where present, order_expr) and `quality_metrics.QUALITY_METRICS`'s
  `diff_here` entry (sql and magnitude_expr) all wrap it in `ROUND(...)`.
  `find_bad_speed_limit.build_candidates_query`/`fetch_candidate_by_id`
  additionally do `SELECT * EXCEPT (geom) REPLACE (ROUND(speed_limit_here_mph)
  AS speed_limit_here_mph), ...` so the *returned* row already carries
  the rounded value too - everything downstream (CSV, templates,
  `attempt.row`) gets it rounded for free, no separate display-side
  rounding needed. If a new query references `speed_limit_here_mph`
  directly (rather than through these existing helpers), wrap it in
  `ROUND()` there too - confirmed this matters with real numbers (a raw
  33.8% "vs. HERE" mismatch rate in the Quality page's aggregate for NC,
  vs. a much lower rate once rounded).
- **Every `here_segment_id` shown anywhere in the app links to Street
  View** - a plain, no-API-key
  `https://www.google.com/maps?q=&layer=c&cbll=<lat>,<lon>` link (same
  URL shape everywhere, no shared helper needed - each template is
  standalone, like `escapeHtml` is already duplicated per template).
  Covers: the Evaluator's Results table (`evaluator_status.html`, both
  the Jinja-rendered version and the JS `render()` mirror - a fourth
  Jinja/JS pair to keep in sync, alongside the results table, FC
  breakdown, and map already documented above) and its CSV export
  (`batch_evaluator._build_csv_row`'s new `streetview_url` column, via
  `_streetview_url(lat, lon)`); the sign checker's "Candidates tried"
  list (`result.html`, alongside the existing "Explore in Google Maps"
  button, not instead of it); and every marker popup on every map this
  app has (Evaluator status/preview, sign checker result/preview,
  Quality's issue map) - five separate popup-building call sites, each
  needing its own added `bits.push(...)` line since none of them share
  a common renderer.
- **Zip code / county filtering, in all three tools** (sign checker,
  Evaluator, Quality) - `find_bad_speed_limit.build_geo_filter_sql(
  zip_codes, counties, states, geom_column="geom")` is the single shared
  implementation (`quality_metrics.build_quality_query` and
  `find_bad_speed_limit.build_candidates_query`/`fetch_candidates` both
  call it), since none of this app's own tables have a zip/county column
  - it's a real spatial join against `bigquery-public-data.geo_us_boundaries`
  (`zip_codes`/`counties`), which does. **Must use
  `ST_INTERSECTS(geom, (SELECT ST_UNION_AGG(...) FROM boundary WHERE
  ...))` - a scalar subquery pre-unioning the matching polygon(s) into
  one geography - not a correlated `EXISTS(...ST_INTERSECTS...)`/JOIN
  against the boundary table.** The EXISTS form was the first thing
  tried and looked reasonable, but BigQuery's optimizer rewrites it into
  a LEFT SEMI JOIN and then rejects it ("cannot be used without a
  condition that is an equality of fields from both sides") since a
  spatial predicate alone isn't an equality condition - confirmed by
  actually running both forms against this project's real tables (not
  just reasoning about it) before picking the one that works. County
  names aren't unique nationwide (many states have a "Washington
  County") - `STATE_FIPS_CODES` (a fixed USPS-code -> FIPS mapping, not
  looked up live) scopes the county match to whatever state(s) are
  already in play, since `bigquery-public-data.geo_us_boundaries.counties`
  has no 2-letter state code column of its own; zip codes are already
  globally unique and don't need this. This is a real, materially more
  expensive query than this app's other (plain column-equality) filters
  - don't add it to a query path that doesn't already scope itself to a
  state or a handful of states first. Every candidates-cache key
  (`find_bad_speed_limit._candidates_cache_key`) and quality-metrics
  cache key (`quality_metrics.fetch_quality_metrics`, via
  `dataclasses.asdict(QualityFilters)`) includes zip_codes/counties -
  don't let a new caching path forget them, or a filtered and
  unfiltered request could wrongly share a cached result.
- **"Preview candidates on map"** (Sign Checker's `index.html` and the
  Evaluator's `evaluator.html`, both near their submit button) - a
  preview-only projection of the roads the current form's state/
  criteria/zip/county filters would select, before committing to an
  actual run. `app.py`'s shared `_preview_candidates_response()` backs
  both `/sign-checker/preview-candidates` and
  `/speed-limits-evaluator/preview-candidates` - the same
  `fetch_candidates()` call a real run makes, capped at
  `PREVIEW_MAX_SEGMENTS` (300, independent of the real run's own
  segment_count/candidates cap) so the preview query and map stay cheap
  and responsive regardless of how large the actual run is configured
  for. Only the fields that affect *which* segments get selected are
  read (state/year/month/project/dataset/criteria/zip_codes/counties) -
  walk/side/heading/fov settings don't matter here, they only affect how
  each already-selected segment is later checked. This is explicitly
  preview-only, not a picker: clicking a marker just shows that
  candidate's info, there's no click-to-select/deselect - a deliberate
  scope decision, not a missing feature.
- **Quality page issue map** ("Map: where the worst mismatches are" card,
  right below the Filters form in `quality.html`) - "visualize where the
  issues/accuracies are," the last of the originally-requested mapping
  capabilities. The Quality page's main query is a pure aggregate
  (COUNT/SUM, see `build_quality_query`) - it has no per-row geometry to
  plot, so this needed a genuinely different query shape, not an option
  bolted onto the existing one:
  `quality_metrics.build_sample_mismatches_query(f, metric_key, limit)`
  is row-level, one metric's own condition (from `QUALITY_METRICS`,
  reusing the exact same `"sql"` each metric's stat tile/aggregate
  already uses - never a second copy of the condition), ordered by that
  metric's own `"magnitude_expr"` descending (worst offenders first) and
  capped at `PREVIEW_MAX_SEGMENTS`. `_common_filter_where_parts(f)`
  factors out the states/functional_classes/zip_codes/counties portion
  of the WHERE clause shared with `build_quality_query`, so the two can
  never silently disagree about what a filter selection means. Served
  by `GET /speed-limits/sample-mismatches` (metric + the same filter
  query params as the main page), triggered on demand by a button (not
  loaded with the rest of the page - it's its own BigQuery query, no
  reason to pay for it before someone asks). Markers are sized by that
  segment's own mismatch magnitude relative to the *sample's own*
  min/max (not a fixed scale) - "how bad" varies a lot by metric (a
  15mph freeflow overshoot isn't the same severity as a 60mph one), so a
  fixed radius scale would either be too flat or clip on every request.
  - **Every metric explains what selecting it means, right in the
    picker** ("for each of the comparable [metrics] ... explain what
    they mean upon selection" was explicit) - `QUALITY_METRICS` entries
    in `quality_metrics.py` each got a one-sentence `"description"` (e.g.
    the two "implausible" metrics' descriptions explicitly say "a
    data-quality check, not a comparison" and name which threshold param
    they read, since those two use `@param2` while the four "disagrees
    with X" metrics use `@param1` - easy to miss from the option text
    alone). Both `app.py` routes that build `quality_metric_choices`
    (`explore_page()` and `speed_limits_quality()`) now pass `key`/
    `name`/`description` per metric, not just `key`/`name` - keep both
    in sync if a third metric-picker page is ever added, rather than
    letting one drift back to name-only options. Both templates render
    it the same way: each `<option>` carries a `data-description`
    attribute (Jinja's default autoescaping handles the apostrophes in
    the text - e.g. "OpenStreetMap's" - so no manual escaping needed),
    and a `<p>` right under the `<select>` (`#metric-description` on
    `explore.html`, `#map-metric-description` on `quality.html`'s own
    `#map-metric` picker) is populated from the selected option's
    `dataset.description` on page load and on every `change` - two
    separate small JS blocks, not a shared helper, matching this app's
    existing per-template `escapeHtml`-style convention rather than
    introducing a new shared-JS file for one function.
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
  - **This same map pattern also lives on the single-run Sign Checker's
    result page** (`result.html`'s "Map" card, right above "Candidates
    tried") - every candidate tried, colored the same way (no `error`
    case there, since a single-run job either finishes with attempts or
    surfaces its error on the whole job). `app.py`'s `result_page()`
    builds a separate plain-dict `map_points_js` list rather than
    reusing `attempts_view` for this - `CandidateAttempt` is a
    dataclass, not JSON-serializable via Jinja's `|tojson` as-is.
  - **The status page's Map card** (`evaluator_status.html`, right after
    the main summary card) plots every checked segment at the position
    actually checked (`seg_summary["lat"/"lon"]`, added in `run_batch`'s
    main loop - `attempt.lat/lon` for match/no_sign_read/no_coverage, the
    candidate row's `centroid_lat/lon` for an `error` result since there's
    no `attempt` in that case), colored by status
    (match=`--ok`/no_sign_read=`--warn`/no_coverage=`--muted`/error=`--err`,
    read from the CSS vars at runtime rather than a third hardcoded copy).
    Uses Leaflet + OpenStreetMap tiles (same free, no-API-key pattern
    already used in `result.html`'s lightbox map), not Google Maps JS -
    deliberate, since this can plot up to 1000 markers and doesn't need
    Street View. Markers are cleared and fully redrawn on every poll (same
    2s cadence as the rest of the page); map bounds are auto-fit only
    once, the first time there's at least one point, so a later poll
    doesn't yank a user's manual pan/zoom back out. `renderMap()` is
    called both from the initial page load (server-rendered
    `status.results`, embedded as `initialResults`) and from `render()`'s
    poll path - a third Jinja/JS pair alongside the FC breakdown and
    results table that needs updating together if `seg_summary`'s shape
    changes.
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

- **Custom test** (the Speed-Limits Quality page's "Custom test" card,
  plus the same textbox on the Evaluator's and Sign Checker's launch
  forms) - lets a user write their own comparison instead of picking
  from the six built-in `QUALITY_METRICS`, either a direct expression or
  plain English translated by Gemini, and actually run Street View
  verification against whatever it selects. All of it lives in the new
  `custom_metrics.py`, built around one non-negotiable invariant:
  **never let user-controlled text reach a BigQuery query string
  directly, LLM-translated or not.** `validate_custom_expression()` is a
  hand-rolled tokenizer + recursive-descent parser (not regex, not an
  external SQL parser) over a narrow grammar (comparisons/AND/OR/NOT;
  +-*/ and `ABS()`/`ROUND()`) against a fixed allowlist of columns
  (`COLUMN_TYPES` - deliberately excludes string/metadata columns). The
  final SQL is re-serialized from the validated parse tree, never a
  slice of the original input - that re-serialization, not the grammar
  restriction alone, is what actually prevents injection (a grammar
  check on the input text with the input text itself then used as the
  query would still be exploitable). `resolve_custom_criterion()` is the
  entry point every caller should use: it tries `validate_custom_expression()`
  on the raw text first, and only if that fails calls
  `classify_custom_test_text()` (Gemini, structured output) and then runs
  *its* output back through the exact same `validate_custom_expression()`
  before using it. Gemini's output is never trusted directly; it's just
  another candidate string for the same validator raw user text goes
  through. Every endpoint (`app.py`'s `/speed-limits/custom-test(/sample)`,
  `evaluator_start()`, `run()`) re-derives the validated SQL from the
  original free-text input on every request - none of them accept an
  already-validated SQL string from the client, since that would let a
  client skip validation entirely by just claiming its SQL is already
  safe. `speed_limit_here_mph` gets the same `ROUND()` special-case here
  as everywhere else in this app (see above) - `_parse_column` rewrites
  any reference to it into `ROUND(speed_limit_here_mph)` automatically,
  so a custom test can't reintroduce the km/h-conversion noise the rest
  of the app already fixed. The parser has an explicit `_MAX_NESTING_DEPTH`
  guard (40) at every self-recursion point, not just BigQuery's own
  length/complexity limits - confirmed a deeply-nested-parens input
  (`'(' * 200 + '1=1' + ')' * 200`, well under the 500-char length cap)
  raises Python's own `RecursionError` without it. `find_bad_speed_limit.py`
  (`build_candidates_query`/`fetch_candidates`/`run_pipeline`),
  `batch_evaluator.BatchConfig`, and `quality_metrics.py` (new
  `build_custom_metric_query`/`build_custom_sample_query` and their
  `fetch_*` wrappers) all take an already-validated `custom_criterion_sql`
  parameter, documented at each call site as "must already be the output
  of `custom_metrics.validate_custom_expression()`" - never re-derive it
  from raw text partway down the call stack, and never widen any of
  these to accept a caller-supplied SQL string that skipped validation.
  `requirements.txt` needs `pydantic` and `google-genai` for this - a
  hard import in `custom_metrics.py`'s `classify_custom_test_text()` -
  actually a *lazy* import inside that one function, not a module-level
  one, so a broken/missing `google-genai` install only breaks the LLM
  fallback path, not the whole app - direct expressions keep working
  even then. Tested against `google-genai` 2.25.0 (the `Client(vertexai=
  True, project=, location=)`, `GenerateContentConfig(response_schema=,
  thinking_config=)`, and `.parsed` mechanics all confirmed by installing
  it and reading its actual source/introspecting it live, not recalled
  from training - this SDK is young enough, and this environment's
  training-cutoff-to-now gap large enough, that guessing its API shape
  from memory alone would have been a real risk); `requirements.txt`
  pins only a loose `>=1.0.0` floor like this app's other dependencies,
  so a future install may resolve a newer release - re-verify the same
  three mechanics against release notes if `classify_custom_test_text()`
  ever starts behaving differently after a dependency bump.
  `Dockerfile`'s `COPY` line needs `custom_metrics.py` - same "don't
  forget the new module" mistake this file already warns
  about twice above.
  - **Authenticates via Vertex AI + Application Default Credentials, not
    an API key.** This was a deliberate pivot from an earlier Anthropic
    (Claude) API-key-based implementation: the key had to live in Secret
    Manager with its own `secretmanager.secretAccessor` grant, and this
    project's IAM is Terraform-owned with `deploy.sh` always run with
    `SKIP_IAM_GRANTS=1` (see below) - a *brand-new* secret's accessor
    grant isn't something Terraform already knows to make, so the first
    real deploy attempt with it silently kept the old revision (no
    working key) instead of erroring loudly, which is what actually
    prompted the switch ("this is too complex"). Vertex AI Gemini needs
    no secret at all: `custom_metrics.classify_custom_test_text()`
    constructs `google.genai.Client(vertexai=True, project=project,
    location=_GEMINI_LOCATION)` and authenticates as whatever identity
    Application Default Credentials resolves to - the same mechanism
    this app's BigQuery/GCS/Vision calls already use. Locally that's
    `gcloud auth application-default login`; on Cloud Run it's the
    runtime service account, which needs exactly one additional grant,
    `roles/aiplatform.user` on the project (see `deploy.sh`) - a single
    project-level IAM role, not a secret-plus-accessor combination to
    keep in sync. `project` is threaded through from `app.py` as
    `DEFAULT_PROJECT` (the same constant `list_evaluable_tables()` etc.
    already use) - not read from an env var, matching how this module
    always took its GCP config as an explicit parameter rather than
    reaching into `os.environ` itself. `_GEMINI_LOCATION` ("us-central1")
    and `_GEMINI_MODEL` ("gemini-2.5-flash") are module constants in
    `custom_metrics.py`, not configurable via env var - flash, not pro,
    was chosen deliberately: this is a narrow classification/translation
    task (pick one of 3 `kind`s, translate into a tiny fixed grammar, or
    answer from a short fixed background text), not something needing
    Pro-tier reasoning, and flash is cheaper and supports fully disabling
    thinking (`thinking_config=types.ThinkingConfig(thinking_budget=0)`) -
    Pro cannot disable thinking entirely, which risks the visible-JSON
    output budget being silently eaten by invisible thinking tokens on a
    task that doesn't benefit from thinking at all. If Gemini's model
    lineup has moved on by the time this is touched again, check Google's
    current model-lifecycle docs before bumping `_GEMINI_MODEL` blindly -
    model IDs churn faster than this comment will stay accurate.
    Structured output goes through `response_mime_type="application/json"`
    + `response_schema=_ClassifiedResponse` (the same pydantic model as
    before) and comes back as `response.parsed` - **but google-genai
    leaves `.parsed` silently `None` on a validation/JSON-decode failure
    instead of raising**, unlike the old Anthropic SDK's `.parsed_output`;
    `classify_custom_test_text()` explicitly checks for `None` and raises
    a clear `RuntimeError` (distinguishing a `MAX_TOKENS` finish reason
    from a generic bad-output case) - don't remove that check on the
    assumption `.parsed` is always populated when a schema is given, it
    isn't. **This actually happened in production** - a real "give me an
    example query to test" request hit `MAX_TOKENS` at the original
    `max_output_tokens=2048`, surfaced correctly as exactly that clear
    error rather than a silent failure, which is how it got caught and
    fixed instead of just failing quietly. The cause wasn't thinking
    tokens eating the budget (`thinking_budget=0` is documented as fully
    disabling thinking, not just discouraging it, and the error message
    itself named the output budget, not thinking, as what was
    exhausted) - it was the visible JSON output itself coming out longer
    than 2048 tokens for that prompt, despite the system prompts asking
    for "1-4 sentences"/"one-sentence" answers; LLMs don't always obey a
    brevity instruction as strictly as a hard token ceiling would.
    `gemini_client.call_structured()`'s `max_output_tokens` default is
    `8192` now (was `2048`) - a pure safety-margin increase (Vertex AI
    bills actual tokens generated, not the ceiling, so raising this has
    no cost unless a response genuinely needs it) shared by all three
    callers (`classify_custom_test_text()`, `bq_sql_console.suggest_sql_fix()`,
    `answer_with_drive_context()`) since they all construct a
    `GenerateContentConfig` through this one helper. If `MAX_TOKENS`
    shows up again after this, that's a sign the root cause is
    verbosity, not budget size - tighten the prompts' length
    instructions rather than keep raising the ceiling as an everything
    fix. Errors are `google.genai.errors.APIError` (its `ClientError`/
    `ServerError` subclasses cover all 4xx/5xx - there's no separate
    typed auth/rate-limit exception the way the old Anthropic SDK had,
    so rate-limiting is instead detected by checking `e.code == 429`) and
    `google.auth.exceptions.DefaultCredentialsError`/`RefreshError` for
    missing/invalid ADC - confirmed by actually running a Vertex AI call
    with no ADC configured (this sandbox has none) and observing exactly
    `DefaultCredentialsError`, the same exception this app's other
    Google-Cloud call sites already catch for the identical reason.
  - **The box also answers general questions about Moove/Archimedes**
    ("what is Moove?", "what does the Evaluator's cost cap mean?") -
    a third response kind alongside "ran a comparison". This is a
    genuinely different code path from expression translation, not a
    relaxation of the SQL-safety grammar: `custom_metrics.classify_custom_test_text()`
    asks Gemini to pick one of `kind="expression"` (translate, same as
    before), `kind="answer"` (answer directly from the fixed
    `_MOOVE_BACKGROUND`/`_ARCHIMEDES_BACKGROUND` text in that module -
    sourced from Moove's own "Turing Technical Overview" doc and this
    repo's own README/CLAUDE.md, not invented), or `kind="unsupported"`.
    `resolve_custom_criterion()` (the one entry point) returns a
    `CustomTestResult` with that `kind` - an `"answer"` is plain text and
    **is never passed through `validate_custom_expression()` or spliced
    into any query**; only a `kind="expression"` result's SQL is (and
    still goes through the exact same validator raw text does, same as
    always). `resolve_custom_criterion_as_expression()` wraps this for
    the Evaluator/Sign Checker's own Custom test box, which has no
    "answer" concept (nothing to select Street View candidates with for
    a plain question) - it raises a clear `ExpressionError` pointing the
    user at the Quality page's box instead if `text` classifies as a
    question there. The two Quality-page routes
    (`/speed-limits/custom-test(/sample)`) both return `{"kind": "answer",
    "answer": ...}` (no BigQuery call at all) or `{"kind": "expression",
    ...}` (the pre-existing shape, unchanged) - `quality.html`'s JS
    branches on `data.kind` to show a distinct teal `.answer-block` for a
    question vs. the match-count/SQL/map controls for a comparison.
    Keep the background text factual and short if it's ever updated - it
    goes verbatim into the system prompt Gemini answers from.
  - **An `"answer"` gets a second, optional enrichment pass grounded in
    live Google Drive search**, not just the fixed background text above.
    ("use all the information in my GDrive to answer questions about
    Archimedes" - and, once a first look showed the relevant Drive
    folders are large multi-level trees (not a handful of docs) and a
    narrower "fold a few specific docs into the fixed background text"
    alternative was offered, explicitly: "I actually want everything -
    build real search, not a static blob".) **Read `drive_search.py`'s
    own module docstring first** - the scope boundary it describes (not
    a hardcoded folder-ID list, not a recursive tree walk) is a
    deliberate design decision, not an oversight. Shape of the flow:
    `classify_custom_test_text()` (above) still runs first and produces
    a usable `kind="answer"` from the fixed background text alone, same
    as before - `resolve_custom_criterion()` then tries to do *better*,
    calling `drive_search.search_drive(text)` and, if it finds anything,
    a second Gemini call (`custom_metrics.answer_with_drive_context()`,
    through the same shared `gemini_client.py` helper) that prefers the
    Drive excerpts over the generic background text when they're
    specific and relevant. **Every failure mode falls back to the
    already-produced background-text answer, never fails the whole
    request**: no Drive results, `DriveNotConfigured` (ADC/scope/API
    issue), or the follow-up Gemini call itself failing all just keep
    the original answer - Drive grounding is strictly an enrichment,
    confirmed by `test_drive_grounded_answer.py`'s CASE 2-4 pattern of
    "the fallback path still returns 200, not an error".
    - **Scope is "whatever the service account can see," not a
      hardcoded list of 3 folders.** Drive's own full-text search
      (`files.list(q="fullText contains '...'")`) already only returns
      files the calling identity has access to - so once the service
      account (`speed-limit-check-runner@...`) is shared as a Viewer on
      a folder (Data Science/Development/Product, as of this writing -
      a one-time manual step in Drive's own UI, since Drive sharing is
      a Workspace permission, not a GCP IAM role `deploy.sh` can grant),
      everything under it becomes searchable automatically, no code
      change needed - and the same is true if it's ever shared on
      *more* folders later. Don't "fix" this by hardcoding the 3 current
      folder IDs into a query filter; that would silently stop tracking
      reality the moment sharing changes.
    - **Text extraction is real, by file type, not Drive's search-result
      snippet alone** - Google-native Docs/Slides/Sheets go through
      Drive's own `files.export(mimeType="text/plain")` (no parsing
      library needed, Drive converts server-side); uploaded `.docx`/
      `.pptx` are downloaded (`files.get_media` +
      `MediaIoBaseDownload`) and parsed with `python-docx`/`python-pptx`
      - added specifically because a real look at the "Archimedes"
      subfolder under Data Science found exactly these two formats
      (3 `.pptx` plan decks + 1 `.docx` platform plan, zero
      Google-native files) - supporting only native export would have
      extracted nothing from the single most relevant folder. **PDFs
      are a known, documented gap, not a silent one** - `_fetch_snippet()`
      just returns an empty string for a PDF today (the file's
      title/link still show up as a result, just with no body text fed
      to Gemini); add a PDF text-extraction library there if that turns
      out to matter rather than reaching for it preemptively.
    - **Sources are shown, not just used** - `search_drive()`'s results
      (title + `webViewLink`) are threaded all the way through
      (`CustomTestResult.sources` -> both `/speed-limits/custom-test(/sample)`
      JSON responses' `"sources"` field -> `_ask_archimedes.html`'s
      `#custom-test-answer-sources`, rendered as clickable links under
      the answer) specifically so a reader can verify/open the actual
      doc an answer came from, not just trust a possibly-stale summary.
      `sources` is `None`/absent whenever Drive grounding didn't
      actually change anything (no results, or any fallback case above)
      - don't show a "Sources:" line for the plain background-text
      answer, that would misattribute it.
    - `requirements.txt` needs `google-api-python-client` (the Drive v3
      client), `python-docx`, and `python-pptx` for this - verified
      against the actually-installed versions (2.x discovery client,
      python-docx 1.x, python-pptx 1.0.2) by round-tripping real
      in-memory `.docx`/`.pptx` files through the extraction functions,
      not assumed from memory, same discipline this file already
      documents for `google-genai`. `Dockerfile`'s `COPY` line needs
      `drive_search.py` - same "don't forget the new module" mistake
      this file already warns about more than once above. `deploy.sh`
      enables `drive.googleapis.com` but - deliberately - grants no new
      IAM role for it, since there isn't one to grant; see that script's
      own header comment for the one manual Drive-sharing step this
      still needs.
  - **A fourth `kind="example"` lets someone ask for a sample
    comparison instead of typing their own** ("give me an example of a
    query and feed that into the textbox as a template to start with" was
    explicit) - typing "give me an example"/"show me a sample
    query"/"I don't know what to type" classifies as `kind="example"`,
    not `kind="unsupported"`. Gemini puts a realistic example in
    `_ClassifiedResponse.example` (same column/operator rules as
    `kind="expression"`, and told to vary which columns it picks across
    requests rather than defaulting to the same one every time) plus a
    one-sentence `explanation`. `resolve_custom_criterion()` still runs
    that example through `validate_custom_expression()` as a sanity check
    before trusting it - **but, unlike `kind="expression"`, returns the
    original example text, not the re-serialized SQL** (`CustomTestResult.example`,
    a new field alongside `.answer`) - the point is to show the user
    something that reads like what they'd type themselves and can edit,
    not parenthesized validator output; the validation call is purely a
    "don't show Gemini's mistake" check, never executed either way. An
    `"example"` is exactly as inert as an `"answer"`: **never passed
    through to BigQuery, never auto-run** - `_ask_archimedes.html`'s JS
    only ever *populates the text box* with it (`textEl.value =
    data.example`, then focuses the box with the cursor at the end) so
    the user still has to hit Run/Enter themselves, going through the
    exact same validation a hand-typed comparison would.
    `resolve_custom_criterion_as_expression()` (the Evaluator/Sign
    Checker's box) rejects `kind="example"` the same way it already
    rejects `kind="answer"` - a clear redirect message, since there's
    nothing to select Street View candidates with for an example
    request either.
  - **The box runs on Enter, not just the "Run" button click** ("make it
    respond to <CR> (execute)" was explicit) - a `keydown` listener on
    `#custom-test-text` in `_ask_archimedes.html` calls the same
    `runCustomTest()` on a plain Enter (`e.preventDefault()` first, so it
    doesn't also insert a newline), while Shift+Enter still inserts one -
    the same convention chat-style text boxes use, chosen because a
    multi-line plain-English question or comparison is plausible here,
    unlike a typical single-line search box.
  - **When Vertex AI credentials aren't configured, `resolve_custom_criterion()`'s
    error message leads with that reason, not the raw grammar-parse
    error.** A real user typed a plain-English question ("what is the
    current model in production?") with no LLM available in that
    deployment (originally: no `ANTHROPIC_API_KEY`; the same principle
    carries over to Vertex AI ADC now), and got "Unrecognized character
    '?' at position 39 - only column names, numbers, ..." as the
    headline, with the actually-useful "not configured" reason buried in
    a trailing parenthetical - read as a confusing parser bug rather than
    the simple, fixable config gap it was. `classify_custom_test_text()`
    raises a dedicated `_CredentialsNotConfigured` (a `RuntimeError`
    subclass) specifically for the ADC-missing case, and
    `resolve_custom_criterion()` catches that separately from a generic
    `RuntimeError` so it can lead with the actionable reason - "Natural-
    language translation and general Q&A aren't available here -
    Vertex AI credentials aren't configured here. (If you meant a direct
    comparison: ...)" - with the direct-parse detail demoted to that
    trailing parenthetical, still there for the minority case of someone
    who actually typed a near-valid expression, just not the first thing
    they read. Don't collapse `_CredentialsNotConfigured` back into a
    plain `RuntimeError` without re-reading the bug report in this file's
    git history first - that's what the distinction is *for*.

- **The Custom test box also runs real SQL - a SEPARATE module
  (`bq_sql_console.py`), a SEPARATE trust model, never the narrow-grammar
  path.** ("check if this is a SQL query and do what's needed with it...
  check if it is malformed and advise on how to fix it given tables and
  their schemas in BQ and the procedures that one can run (CALL) in BQ",
  plus "always estimate the amount of GBs and money that a BQ query will
  take... if the estimate passes $10, ask the user for permission again"
  - both explicit.) **Read `bq_sql_console.py`'s own module docstring
  before touching this** - it exists specifically to keep this feature's
  trust model from being confused with `custom_metrics.py`'s: that module
  protects against untrusted/ambiguous text becoming SQL the person
  didn't intend (re-serialized from a validated parse tree, never the
  original text); this one is for someone who has explicitly written
  real SQL they intend to run themselves, where the open questions are
  authorization and cost, not injection. Never route narrow-comparison
  text through this module, and never loosen `custom_metrics.py`'s own
  grammar on the theory that "well, we run real SQL elsewhere now" - the
  two are deliberately different code paths for a reason.
  - **Routing**: `app.py`'s `_sql_console_response(text)` runs BEFORE
    `_validate_quality_custom_text()` on both `/speed-limits/custom-test`
    routes (the sample/map endpoint never needed it - see below) -
    `bq_sql_console.sql_statement_kind(text)` is a plain "what's the
    first keyword" check (SELECT/WITH -> "select", CALL -> "call", else
    `None`), not a parser, and deliberately not a safety boundary (see
    the module docstring) - it only decides which of the two completely
    different downstream paths handles this text. This has to run first:
    a real `SELECT ... FROM ...` would otherwise reach
    `validate_custom_expression()` and fail with a confusing "Unknown
    column 'SELECT'" instead of ever reaching the SQL console.
    `/speed-limits/custom-test/sample` (the "show on a map" button)
    deliberately does NOT get this routing - that button only ever
    appears for `kind="expression"` results (never for `sql_results`/
    `sql_cost_estimate`/`sql_error`), so raw SQL text can never reach it
    through the normal UI flow; a raw `curl` POST of SQL text to that
    endpoint still degrades harmlessly through the old comparison/Gemini
    path (fails clearly, doesn't crash) since there's no sensible "plot
    an arbitrary result set on a segment map" behavior to build for it.
  - **Cost estimate before every execution, via a free BigQuery dry run**
    (`bq_sql_console.estimate_query_cost()` - `bigquery.QueryJobConfig(
    dry_run=True, use_query_cache=False)`, which validates the SQL and
    reports `total_bytes_processed` without running or billing anything).
    `BQ_ON_DEMAND_PRICE_PER_TIB_USD = 6.25` is BigQuery's on-demand price
    at the time this was written, hardcoded since this app makes no live
    pricing-API call to keep it honest automatically - re-verify against
    the real BigQuery pricing page before trusting it if it's been a
    while, same caution this file already gives `BQ_ON_DEMAND_PRICE_PER_TIB_USD`'s
    own comment. Above `COST_CONFIRMATION_THRESHOLD_USD` (10.0, the
    user's own number), `run_sql_console()` returns `kind="cost_estimate"`
    (gb/cost_usd, no query run yet) instead of executing - the frontend
    (`_ask_archimedes.html`) shows it as a `.badge.warn` with a "Yes, run
    it" button that resubmits the *same* text with a `confirmed=1` form
    field; `app.py`'s `_sql_console_response()` reads that into
    `run_sql_console(..., confirmed=...)`. A cheap query (at/under the
    threshold) just runs immediately on the first submit - no needless
    extra click for something that was never going to cost real money.
  - **`CALL` (stored procedures) is a SEPARATE gate from cost, and
    stricter**: `PRIVILEGED_SQL_USERS` is a hardcoded allowlist (`{"eyal@moove.ai",
    "justin@moove.ai"}` as of this writing) checked via the same
    `_requester_email()`/IAP-header identity every other per-user
    feature in this app already uses (the Evaluator's daily cost cap,
    "who ran it") - checked FIRST, before any BigQuery call at all, so a
    non-privileged user's CALL attempt never even reaches a dry run.
    Hardcoded rather than an env var/config file deliberately: who can
    run a procedure that might mutate production data is a rare,
    consequential decision that should show up as a reviewed code
    change, not a silent config edit. **A privileged user's CALL still
    always needs the confirmation click, regardless of estimated cost**
    (`needs_confirmation = kind == "call" or estimate.cost_usd >
    COST_CONFIRMATION_THRESHOLD_USD`) - the real risk of CALL isn't
    billing, it's side effects (arbitrary procedural SQL, potentially
    including DML), so a $0.001 CALL still gets the same "are you sure"
    step a $50 SELECT would. Don't ever make CALL skip confirmation on
    the theory that a cheap dry run implies it's safe - cost and safety
    are unrelated for a procedure.
  - **Whole-project schema discovery for Gemini's fix advice** - the
    explicit ask was "given tables and their schemas in BQ and the
    procedures that one can run (CALL) in BQ", scoped (per the
    conversation that decided this) to literally every dataset/table/
    procedure the project has, not just this app's own `calc_out`/
    `archimedes_api` tables the rest of Archimedes already knows about.
    `fetch_project_schema_summary()` loops `client.list_datasets()` and
    runs two `INFORMATION_SCHEMA` metadata queries per dataset (COLUMNS,
    ROUTINES filtered to `routine_type = 'PROCEDURE'`) - metadata-only,
    so free/cheap regardless of table size, but still TTL-cached
    (`SCHEMA_CACHE_TTL_SECONDS`, same pattern `quality_metrics.py`
    already uses for its own narrower schema lookups) since it's still a
    real loop of synchronous BigQuery calls, and capped at
    `_MAX_SCHEMA_TEXT_CHARS` so a project with many datasets can't blow
    up the Gemini prompt - truncated with a visible note, never silently.
    Deliberately a per-dataset loop, not a single project/region-level
    `` `region-us`.INFORMATION_SCHEMA `` query, even though BigQuery
    supports the latter - the loop form works regardless of which
    region(s) this project's datasets actually live in, which wasn't
    worth assuming correctly on the first try. Only called on the error
    path (a malformed query), never on a successful one - a normal
    SELECT/WITH/CALL never pays for a schema fetch.
  - **Fix advice is Gemini, via the same `gemini_client.py` helper
    `custom_metrics.py` uses** (factored out of `custom_metrics.py`'s
    former `classify_custom_test_text()` into its own module specifically
    because this feature needed the identical Vertex AI Client/
    structured-output/credentials-and-rate-limit-error mechanics a
    second time - see `gemini_client.py`'s own module docstring for why
    duplicating it instead would have been exactly the kind of drift this
    file warns against elsewhere). `suggest_sql_fix()` sends the failed
    SQL, BigQuery's own error text, and the project schema summary above
    to Gemini, asking for a 1-3 sentence explanation plus an optional
    `suggested_sql` - **never validated or auto-run**, exactly like an
    "example" (see above): `_ask_archimedes.html`'s "Use this suggestion"
    button only loads it into the text box, same as the example-kind
    flow, so it still goes through a real cost estimate (and the
    confirmation gate, if it crosses the threshold) when the user
    actually submits it. If Gemini itself isn't available (no ADC, rate
    limited, ...), `run_sql_console()` still returns the raw BigQuery
    error on its own (`_error_with_fix_advice()` catches that separately
    and degrades to `fix_explanation=None`) - a missing LLM should never
    turn a real, actionable BigQuery error into a blank screen.
  - **Results rendering is a generic table, not the narrow comparison
    path's match-count/map UI** - a raw SELECT/WITH can return any shape
    of result, so `_ask_archimedes.html` builds a plain `<table>` from
    `columns`/`rows` at request time (`renderSqlResultsTable()`), reusing
    this app's existing bare `table`/`td`/`th` CSS in `layout.html`
    rather than inventing a new table style. `MAX_RESULT_ROWS` (500) caps
    what's fetched/returned independently of `quality_metrics.PREVIEW_MAX_SEGMENTS`
    (300, the narrow path's own "sample points on a map" cap) - different
    caps for a different kind of result, not something that should be
    unified just because the numbers are similar. BigQuery row values
    (date/datetime/Decimal/bytes, ...) aren't directly JSON-serializable
    by Flask's `jsonify()` - `bq_sql_console._json_safe()` stringifies
    anything that isn't already a JSON-primitive before the route ever
    calls `jsonify()`, since this module is the one that actually knows
    these are BigQuery values; don't push that conversion up into `app.py`.
  - `requirements.txt` needs nothing new for this (`google-cloud-bigquery`
    already pulls in `google-api-core`, whose `GoogleAPICallError`/
    `BadRequest` this module catches) - but `Dockerfile`'s `COPY` line
    needs both new modules, `gemini_client.py` and `bq_sql_console.py` -
    same "don't forget the new module" mistake this file already warns
    about more than once above.

- **Model Registry Compare sidebar - browse archived speed-limit model
  variants by state/period, then compare 2-3 of them, shown on every
  page.** Originally built as a flat "pick two" list over
  `calc_archive.model_registry_results` ("I now want to integrate the
  whole scheme from the 'speed limits improvements' as a library of
  models... I want only models that already exist in the archive to be
  available for comparisons in the UI", then "I want to see the full
  table names in the sidebar... that sidebar should feed all the tabs"),
  then REDESIGNED from scratch after a colleague's email described a
  purpose-built BigQuery backend (`calc.model_registry_list_states`/
  `list_periods`/`list_tags`/`compare_models`) plus a clickable mockup
  (a Claude artifact) for a cascading state -> period -> 2-3 tags -> a
  shared column flow - specifically so this panel never has to list the
  full registry table once it grows past thousands of rows. **Read
  `model_library.py`'s own module docstring before touching this** - it
  explains two load-bearing discrepancies between what that email
  described and what's actually deployed, both confirmed live against
  BigQuery rather than taken on faith:
  - **`calc.model_registry_list_common_columns` does not exist.**
    `calc.INFORMATION_SCHEMA.ROUTINES` was queried directly and only
    four of the five procedures the email described are actually
    there (`list_states`/`list_periods`/`list_tags`/`compare_models` -
    no `list_common_columns`). `model_library.list_common_columns()`
    computes the same thing itself instead, via a per-table
    `INFORMATION_SCHEMA.COLUMNS` query (scoped to the 2-3 specific
    tables someone just picked, never a dataset-wide scan) - not a
    guess at what a missing procedure might have done, just the same
    "intersect the common numeric columns" logic written directly.
    Filters to `INT64`/`FLOAT64`/`NUMERIC`/`BIGNUMERIC` only, which
    naturally excludes `here_segment_id` (STRING, the join key itself),
    `geom` (GEOGRAPHY), and other non-metric columns without needing a
    hand-picked allowlist.
  - **`compare_models()` here does NOT call `calc.model_registry_compare_models`.**
    Tested live: a dry run of `CALL calc.model_registry_compare_models(...)`
    reports `total_bytes_processed=0`, because that procedure's real
    query is built at runtime via `EXECUTE IMMEDIATE` inside
    `calc.model_registry_relate_columns` - a dry run can't see through
    dynamic SQL constructed after the fact. That makes it impossible to
    give an honest cost estimate before running it, which this app's
    standing rule requires for anything scanning a 700K-35M row archived
    table (same `$10` dry-run/cost-gate flow as the SQL console -
    `bq_sql_console.estimate_query_cost`/`COST_CONFIRMATION_THRESHOLD_USD`,
    reused rather than reinvented). It would also hand back one row per
    `here_segment_id` - exactly what the email's own scale note warned
    against materializing client-side - meaning a second aggregation
    query would be needed regardless. `build_pairwise_comparison_sql()`
    builds the one literal, dry-runnable, already-aggregated query
    instead: the same join-on-`here_segment_id` methodology
    `model_registry_relate_columns` itself implements (and the same
    `COUNT`/`COUNTIF` agree-count/`AVG(ABS(...))` methodology the
    original user-supplied example query demonstrated), generalized from
    a single pair to every pairwise combination among 2 or 3 selected
    models in one query (aliases `a`/`b`/`c`, one join per extra model,
    `agree_ab`/`avg_abs_diff_ab`/`agree_ac`/... columns) - written
    directly rather than through a wrapper whose cost can't be seen in
    advance.
  - **`speed_limit_here_mph` is wrapped in `ROUND()` when chosen as the
    compare column; every other column isn't** - same established rule
    as `quality_metrics.py`/`find_bad_speed_limit.py` (search this file
    for "always wrapped in `ROUND()`"): it's the one column stored as a
    noisy, unrounded km/h->mph conversion, and comparing it unrounded
    would read as spurious disagreement. `model_library._NEEDS_ROUNDING`
    is the one place this is special-cased - don't blanket-`ROUND()`
    every column, that rule is specific to this one column for a
    specific, confirmed reason.
  - **A "model" is identified to the frontend by a stable key
    (`tag|state|year_num|month_num`), never by a raw table name** -
    `ModelEntry.key`, what the step-3 checkboxes use and what
    `/models/compare` takes in its `keys` array. The route always
    re-resolves every key against a FRESH `list_tags(state, year_num,
    month_num)` call, and the chosen column against a fresh
    `list_common_columns()` call, before building any SQL (`app.py`'s
    `models_compare()`/`models_columns()`) - a stale or forged key/column
    just fails the lookup instead of ever reaching
    `build_pairwise_comparison_sql()`'s table-name interpolation.
  - **Each step only ever fetches what the previous step narrowed to** -
    `GET /models/states` (all of them, small/cheap), `GET
    /models/periods?state=` (one state), `GET
    /models/tags?state=&year_num=&month_num=` (one state+period, at most
    a handful of rows, enriched with `predictions`/`errors`/
    `grievous_errors` via one more small query scoped by the same
    state+period+tag - never a full-table scan), `GET
    /models/columns?state=&year_num=&month_num=&tag=&tag=` (2-3 specific
    tables). The OLD flat approach (`discover_models()`, one `SELECT *
    FROM model_registry_results`) is gone entirely - it doesn't scale
    once the registry grows past a handful of rows, which is exactly
    what the redesign was for.
  - **The sidebar is included once, in `layout.html` itself, not
    per-page**, as a fixed-position overlay (not part of `.wrap`'s
    centered flow) with a persistent vertical "Models" tab, open by
    default above an 860px viewport, collapsed below it - open/closed
    state remembered in `localStorage` purely as a per-viewer
    convenience, never relied on for anything the server needs back.
  - **Steps 1 and 2 (state, period) auto-collapse once resolved; step 3
    (2-3 models) deliberately does NOT**, and this was a real bug caught
    by the Playwright walkthrough while building this, not a hypothetical:
    the first version called the same "mark done + collapse" helper for
    step 3 the instant 2 boxes were checked, which hid the step's own
    body - including the still-unchecked third checkbox - making it
    physically impossible to add a third model through the UI. 2 is the
    MINIMUM for step 3, not a finish line (3 is also valid), so it only
    gets the "done" styling (green step number) without collapsing;
    collapsing stays available manually (clicking the step header once
    it's marked done). If a future step is similarly "resolved but still
    open to more," don't reach for `setStepStatus(..., 'done')` - it
    collapses by design.
  - **"Use this model" (step 3, per row) feeds that model's identity into
    every other tab** - writes `{table, state, year_num, month_num,
    label}` to `localStorage['archimedes-active-model']` and applies it,
    via generic element-ID lookups (`#table`, `#state`, `#year`,
    `#month`), to whichever of those exist on the CURRENT page -
    `quality.html`/`explore.html`/`agent.html` share `#table` (a
    `<select>`; a fallback `<option>` is inserted first if the active
    table isn't already one of `table_options`, same pattern those
    templates already use for an arbitrary `filters.table` value), and
    `evaluator.html`/`index.html` share `#state`/`#year`/`#month`. The
    same `applyActiveModelToPage()` call also runs unconditionally on
    every page load (not just after clicking "Use"), straight from
    `_model_library_sidebar.html`'s own script, so this is the ONE place
    that does the feeding - no other template needs to change to
    participate, and a future tab reusing the same field IDs is fed for
    free. Deliberately only ever SETS a field's value - never
    auto-submits a form or navigates (Quality/Explore are GET-form pages
    that should only reflect a new table once the user actually submits,
    not silently on page load). A small "Active everywhere: &lt;label&gt;"
    banner (`#model-lib-active`, a `.badge.ok`) shows which model (if
    any) is currently being fed, without needing to inspect any one
    tab's fields to find out.
  - `Dockerfile`'s `COPY` line needs `model_library.py` - same "don't
    forget the new module" mistake this file already warns about more
    than once above.

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
  - **`roles/aiplatform.user` (for Gemini/Vertex AI - see the Custom test
    bullet above) is NOT yet one of the roles Terraform grants** - it's a
    new requirement this app didn't have before, added after Terraform's
    config was last set up for this project, so it isn't inherited for
    free the way the roles above are. Confirmed live: `eyal@moove.ai`
    (this project's day-to-day deployer) got `does not have permission to
    access projects instance [moove-platform-testing-data:setIamPolicy]`
    trying to grant it directly via `gcloud projects
    add-iam-policy-binding` - the same "can create resources, can't set
    IAM policy on the project" gap `deploy.sh`'s own comments already
    describe, not a one-off fluke. This grant needs to come from whoever
    manages this project's Terraform config (add it alongside the roles
    already listed above, for the same `speed-limit-check-runner`
    service account) or from someone who does have IAM-admin rights on
    the project - don't assume the deploying user's own account can
    self-serve this one just because it can for bucket/secret/service-
    account creation. This project's IAM policy also has *conditional*
    bindings already on it (from an unrelated Databricks integration) -
    `gcloud` refuses to add a new binding without an explicit
    `--condition=None`/condition choice once any conditional binding
    exists on the policy, which is why every project-level
    `add-iam-policy-binding` call in `deploy.sh` already carries
    `--condition=None` - don't drop that flag from a new one, `gcloud`
    will just prompt interactively instead of failing cleanly.
