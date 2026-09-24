# Archimedes

MooveAI's hub for data quality tools, currently covering speed limits (one
app, one deployment - see `app.py`):

- **`/`** - the hub itself: a catalog of MooveAI's models, linking to
  whichever ones have tools built here (today, just Speed Limits).
- **`/speed-limits`** - **Speed-Limits Quality**: nationwide BigQuery
  metrics on how often the inferred speed limit disagrees with OSM, HERE,
  observed average speed, or freeflow speed (or is itself implausibly
  fast), with optional state/functional_class filtering and breakdown.
  See "Speed-Limits Quality" below.
- **`/sign-checker`** - the original tool this repo started as: finds
  road segments in `calc_out.speed_limits_<STATE>_<YEAR>_<MONTH>_details`
  where the inferred speed limit disagrees sharply with both HERE and OSM
  (while HERE and OSM agree with each other), then tries to verify the
  real speed limit by reading a sign in Google Street View imagery at
  that location. Linked from the Speed-Limits Quality page. Most of this
  README (Query/Setup/Usage/Output/Resilience/Caching/How sign reading
  works) is about this tool specifically.
- **`/speed-limits-evaluator`** - **Speed-Limits Evaluator**: the sign
  checker's own per-segment logic, run across up to 1000 segments for a
  state concurrently instead of one at a time - durable status you can
  check on later, CSV export, and a history of past runs to compare
  against. See "Speed-Limits Evaluator" below.

## Query

The default selection criteria (all editable in the web UI - see below):

```sql
SELECT * FROM `<project>.calc_out.speed_limits_<STATE>_<YEAR>_<MONTH>_details`
WHERE
  functional_class < 6
  AND abs(speed_limit_here_mph - speed_limit_osm_mph) <= 1
  AND abs(speed_limit_infer_mph - speed_limit_here_mph) >= 10
  AND abs(speed_limit_infer_mph - speed_limit_osm_mph) >= 10
ORDER BY abs(speed_limit_infer_mph - speed_limit_here_mph) DESC
```

i.e. HERE and OSM agree with each other but both disagree with the inferred
value by a lot, on a "real" road. Two more criteria are available but
disabled by default, using `speed_limit_infer_mph_corrected` instead of the
raw inferred value: `abs(infer_corrected - HERE) >= 5` and
`abs(infer_corrected - OSM) >= 5`. Any combination of criteria can be
checked/unchecked with its own threshold in the web UI; candidates are
ranked by the first checked "≥ N mph" criterion, largest mismatch first.
Not every road segment has Street View coverage or a legible sign in frame,
so the pipeline walks down the ranked list until one works (or, with "walk
through all candidates" checked, tries every one and reports every match).

## Setup

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Google Cloud credentials** (used for both BigQuery and the Cloud Vision API)

   ```bash
   gcloud auth application-default login
   ```

   or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key with:
   - `roles/bigquery.dataViewer` (or read access to the `calc_out` dataset) and `roles/bigquery.jobUser`
   - `roles/serviceusage.serviceUsageConsumer` (Vision API calls are gated on `serviceusage.services.use` against the quota project - Vision ships no `roles/cloudvision.*` predefined role at all)

   Make sure the **Cloud Vision API** is enabled on the project you run jobs
   against (`gcloud services enable vision.googleapis.com`).

3. **Google Maps Platform API key**

   Create a key with these APIs enabled:
   - **Street View Static API** - used server-side to fetch the images that get OCR'd.
   - **Maps JavaScript API** - used client-side (web app only) for the "Explore in Google Maps" interactive map/Street View panorama.

   Store it in a keys file *outside* this git repo, at
   `~/Claude/MooveAI/keys.env` (i.e. one directory above the repo checkout):

   ```bash
   cp keys.env.example ~/Claude/MooveAI/keys.env
   # then edit ~/Claude/MooveAI/keys.env and fill in:
   #   GOOGLE_MAPS_API_KEY=AIza...your real key...
   ```

   The script reads this file automatically. An environment variable of the
   same name, if already set, takes precedence over the file. Use
   `--keys-file` to point at a different path.

   Note: the Maps JavaScript API key is necessarily visible in the web app's
   page source (that's how the JS API always works, not specific to this
   app) - fine for a tool bound to `127.0.0.1` that only you use. If you
   want to be careful anyway, add an HTTP referrer restriction on the key
   in the Cloud Console for `http://127.0.0.1:5050/*` and `http://localhost:5050/*`.

## Usage

### Web app

```bash
python app.py
```

Then open **http://127.0.0.1:5050** in a browser. Fill in state/year/month
(project/dataset default to `moove-platform-testing-data`/`calc_out`, and
can be overridden per run) and submit - the run happens in a background
thread while the page shows a live progress bar and log (polling every
~700ms), then lands on a results page with the matched segment's full row,
the sign-read speed, and a gallery of every candidate tried - including
the images captured and the OCR text found in each, for ones that didn't
match too. Runs on localhost only.

Click any thumbnail to open it full-size in a lightbox with a small live
map (OpenStreetMap via Leaflet, no extra API key needed) showing exactly
where that image was captured and which way the camera was pointed (an
arrow rotated to the image's compass heading: 0=N, 90=E, 180=S, 270=W).
Use the on-screen arrows, the ← / → keys, or Esc to navigate between a
candidate's images or close the viewer.

Each candidate also has an **"Explore in Google Maps"** button, which opens
the actual Google Maps JavaScript API - not just a static image - starting
you directly inside an interactive Street View panorama at that segment's
location, facing the direction the sign was read from. From there you can
drag to look around, click the navigation arrows or double-click the
ground to walk further down the road, click the small map thumbnail in
the corner to pop back out to the 2D map, and use the layers control
there to switch to satellite - the same pegman-drag/walk/switch experience
as maps.google.com, embedded in the page. Requires the Maps JavaScript
API key from setup step 3 above.

The form remembers every value you enter, saved to that browser's
`localStorage` as soon as you change anything - not only when you submit -
so settings survive a page reload or reopening the tab later, no server-side
account or file needed. This only affects that one browser; a different
browser or a cleared site data starts back at the built-in defaults below.
The first time you ever open the page (nothing saved yet), the form starts
with the settings this tool has actually converged on for reliably catching
a sign: `3` candidates to try, headings `10` (relative to direction of
travel), camera zoom `50`, walk through all candidates, slow-walk each
segment at `2`m spacing, and sides-only. This is a lot more API calls per
candidate than a conservative starting point would be (roughly
`segment_length / 2 × 2 sides` Street View + Vision calls each) - lower
"Candidates to try" further if a run is taking too long or costing more
than expected.

Additional options on the form:

- **Selection criteria** - each of the criteria listed under "Query" above
  has its own checkbox and editable threshold; any combination can be on.
- **Headings to capture per position** - comma-separated degrees, applied
  at every position checked (centroid, walked positions, and side offsets
  alike). More headings costs one more Street View + Vision call per
  position each, but improves the odds of catching a sign at an angle
  that falls between the ones already captured - e.g.
  `0,45,90,135,180,225,270,315` for 8 evenly-spaced directions. Capped at
  24 headings.
- **Headings are relative to direction of travel** - unchecked, headings
  are fixed compass degrees (0=N, 90=E, 180=S, 270=W). Checked, each
  heading is instead rotated by the road's own local bearing at the
  position it's captured from (0=straight ahead along the road, 90=right,
  180=behind, 270=left) - useful since a sign usually faces along the road
  it's posted on rather than a fixed compass point, and a walked segment's
  bearing can vary from one end to the other. Combined with "slow-walk"
  and "sides only"/"both sides", a side position is only a guess at where
  a separate, unmapped trunk might be, and that trunk isn't guaranteed to
  run parallel to the centerline it was guessed from (e.g. a diverging
  ramp) - so its own actual direction of travel is derived instead from
  consecutive real Street View panorama positions Google snaps to along
  that side, wherever at least two resolve to coverage; otherwise it falls
  back to the centerline's bearing (including whenever slow-walk is off,
  since there's then only one point per side to begin with).
- **Camera zoom (field of view)** - Google's normal shot is 90 degrees
  wide. A narrower value zooms in, making a distant or small
  sign occupy more of the fixed 640x640 image and so easier for OCR to
  read - try this when a sign is visibly present in a captured image at
  the right position/heading but still isn't being detected, which
  usually means it's too small/low-resolution in frame rather than
  missing entirely. Comes at the cost of a narrower cone around each
  heading, so a sign that was off-axis enough to still fit the default
  view may fall outside a much narrower one - pair a narrow zoom with
  more headings if a sign might be caught at an angle. Range 10-120.
- **Check one specific segment** - paste a `here_segment_id` (e.g.
  `here:cm:segment:412644259`) to check just that segment directly,
  bypassing the criteria entirely.
- **Walk through all candidates** - by default the run stops at the first
  segment with a readable sign; check this to instead try every candidate
  up to "Candidates to try" and report every match found.
- **Slow-walk each segment's full length** - by default only the segment's
  centroid is checked, which can miss a sign positioned elsewhere along a
  longer segment. Check this to instead sample the segment's actual line
  geometry (fetched from BigQuery) at positions roughly "Spacing between
  positions (meters)" apart, from one end to the other, and check each in
  turn, stopping at the first one with a readable sign. The number of
  positions is derived from that spacing and the segment's own length
  (clamped to 2-40 points), not a fixed count, so a short segment isn't
  over-sampled and a long one isn't under-sampled by one setting. If a
  sign keeps getting missed, try a smaller spacing - a wider one leaves
  more room to walk past a sign that's only clearly legible within a
  narrow window. This costs more API calls the smaller
  the spacing (roughly `segment_length / spacing` calls per segment), so
  it runs slower - keep "candidates to try" modest when it's on. Images/
  results are cached per position (under a `point<N>/` subdirectory),
  same as the default single-point mode.
- **Which side(s) of the road to check** - a road can have two physically
  distinct, separately-surveyed-by-Google carriageways or trunks a short
  distance apart, close enough that they're the same "here segment" in
  the source data but not close enough for Street View's nearest-panorama
  snapping to ever reach the far one from centerline sampling alone.
  Applies to every position checked (whether just the centroid or every
  walked point):
  - **Center only** - just the position(s) themselves, same as always.
  - **Left only / Right only** - skips the center and instead offsets
    each position by "Offset distance (meters)" perpendicular to the
    road on just that one side. Useful once you already know which
    carriageway/trunk has the sign, so it wastes no calls on the center
    or the other side. "Left"/"right" are relative to this
    here_segment_id's own digitized line direction (the same bearing
    "headings are relative to direction of travel" uses below), not
    necessarily the real-world right-hand side for a driver - HERE's
    line direction isn't guaranteed to match legal direction of travel,
    so it's worth checking a segment's actual result before assuming
    "right" always means the same physical side across every segment.
  - **Sides only** - skips the center and instead offsets each position
    by "Offset distance (meters)" perpendicular to the road on both
    sides. Useful once you already know the center point's own
    Street View coverage isn't the carriageway/trunk you care about, so
    it wastes no calls checking it.
  - **Center + both sides** - checks all three, for when you're not sure
    which one has the sign. Roughly triples API calls per position
    (center + left + right).

  If the other carriageway/trunk still isn't reached, try a larger
  offset. Combines with "slow-walk the full length" - each walked
  position gets the same side-mode treatment.
- **Auto-detect the offset instead, from the segment's own width** - off
  by default, meaning "Offset distance" above is a single fixed value
  used for every candidate regardless of the actual road. Check this to
  instead estimate it per candidate: from the segment's own middle
  position, probe increasing distances on each side and look for where a
  genuinely separate Street View panorama actually exists (i.e. its
  snapped location has moved away from the centerline with the probe,
  rather than Street View just re-snapping back to the same nearby
  coverage) - the smallest such distance found is used as that
  candidate's offset, on the side(s) it was found. Adds a handful of
  extra Street View calls per candidate for the probing itself, and falls
  back to the manual offset distance for a candidate where nothing is
  found within the probed range (out to 60m).

### CLI

```bash
python find_bad_speed_limit.py --state NC --year 2026 --month 08
```

| Flag | Default | Description |
|---|---|---|
| `--project` | `moove-platform-testing-data` | BigQuery project |
| `--dataset` | `calc_out` | BigQuery dataset |
| `--candidates` | `10` | How many top-mismatch rows to try before giving up |
| `--segment-id` | none | Check one specific `here_segment_id` instead of running the mismatch query |
| `--walk-all` | off | Don't stop at the first readable sign - try every candidate and report every match |
| `--walk-segment` | off | Sample several positions along each segment's full length instead of just its centroid |
| `--walk-segment-spacing-m` | `15` | Target distance in meters between sampled positions when `--walk-segment` is set (point count is derived from this and each segment's actual length, clamped to 2-40 points) |
| `--side-mode` | `center` | Which perpendicular-offset points to probe at each checked position: `center` (just the point itself), `left`/`right` (only that one offset point, skipping center), `sides` (both offset points, skipping center), or `both` (center plus both sides) |
| `--side-offset-m` | `20` | Perpendicular offset in meters for `--side-mode sides`/`both` - the fallback when `--auto-side-offset` is set and finds nothing |
| `--auto-side-offset` | off | Estimate `--side-offset-m` per candidate from the segment's own width instead of using a fixed value (see web UI description above) |
| `--headings` | `0,90,180,270` | Comma-separated headings (0-359) to capture per position (max 24) - compass degrees, or relative angles if `--headings-relative` is set |
| `--headings-relative` | off | Treat `--headings` as relative to each position's local road bearing (0=ahead, 90=right, 180=behind, 270=left) instead of fixed compass degrees |
| `--fov` | `90` | Street View camera field of view in degrees (10-120) - narrower zooms in, making a distant/small sign more legible to OCR at the cost of a narrower cone per heading |
| `--out-dir` | `output` | Where Street View images are saved |
| `--keys-file` | `~/Claude/MooveAI/keys.env` | KEY=VALUE file to load API keys from |

The CLI always uses the default selection criteria - per-criterion
enable/threshold customization is currently web-UI only.

## Speed-Limits Quality

`/speed-limits` is a nationwide, no-imagery, table-only counterpart to the
sign checker - instead of verifying one segment's actual sign, it reports
what fraction of *all* road segments show a large enough mismatch to be
worth caring about, computed directly by a BigQuery aggregate query
against a `calc_out` table you pick from a live-populated dropdown (see
"Choosing a table" below) - by default
`speed_limits_US_<YEAR>_<MONTH>_details`, the nationwide version of the
same table family the sign checker queries per-state. The page shows the
exact fully-qualified table it evaluated - deliberately not
`archimedes_api.speed_limits_infer_details`, which is missing
`speed_limit_here_mph`/`freeflow_mph` that two of these six metrics need.

### Choosing a table

The "Table" dropdown lists every table/view currently matching
`calc_out.speed_limits_US*` or `archimedes_api.speed_limits_infer*`,
fetched fresh on every page load (a cheap `INFORMATION_SCHEMA.TABLES`
query per dataset, not billed against the table data itself, and cached -
see "Async loading and caching" below) rather than assumed from a naming
pattern - so it always reflects what's actually published, including a
new month's table as soon as it exists, with no code change needed here.
Not every matching table/view has all the columns these six metrics
need, though: only the `calc_out` `_<YEAR>_<MONTH>_details` tables carry
`speed_limit_here_mph` and `freeflow_mph`. Picking a narrower one (e.g.
`calc_out.speed_limits_US_latest`, a bare
`calc_out.speed_limits_US_<YEAR>_<MONTH>` without `_details`, or either
`archimedes_api.speed_limits_infer` view) doesn't crash or show a raw
BigQuery error - the page checks the selected table's actual columns up
front and shows a plain-English warning naming what's missing and which
metric(s) need it, with no query even attempted. Harmless to try, just
not something these metrics can be computed from; pick a `_details` table
instead.

### Choosing the inferred field

Which column actually holds "the inferred speed limit" varies by table -
some have `speed_limit_infer_mph_corrected`, others only
`speed_limit_infer_mph` or `speed_limit_infer_mph_new2`/`_new3`. The
"Inferred field being evaluated" dropdown lists whatever columns matching
`speed_limit_infer*` the *currently selected table* actually has
(`quality_metrics.list_infer_fields`, live per table, also cached),
defaulting to `speed_limit_infer_mph_corrected` when that table has it.
Every metric that compares against "the inferred value" uses whichever
field is selected here - the page always shows which one that is, both
in this dropdown and (implicitly) in every stat tile's meaning.

Six metrics, each the percent of segments meeting a condition:

| Metric | Condition |
|---|---|
| vs. OSM | `abs(<inferred field> - speed_limit_osm_mph) >= param1` |
| vs. HERE | `abs(<inferred field> - speed_limit_here_mph) >= param1` |
| vs. observed avg speed | `abs(<inferred field> - speed_AVG_mph) >= param1` |
| vs. freeflow speed | `abs(<inferred field> - freeflow_mph) >= param1` |
| Observed avg speed implausible | `speed_AVG_mph > param2` |
| Freeflow speed implausible | `freeflow_mph > param2` |

`param1` ("Mismatch threshold", default 10 mph) and `param2`
("Implausible-speed threshold", default 80 mph) are both adjustable on
the page - the stat tiles and breakdown table headers splice in whatever
value is actually in effect (e.g. "Disagrees with OSM by 15+ mph"), not
the literal placeholder word "param1"/"param2" shown in the table above.
Optional filters narrow the same query to specific states and/or
`functional_class` values (comma-separated;
blank = everything); "Break down by" additionally groups the results by
state, functional_class, or both, showing a breakdown table under the
always-shown nationwide (or filtered-nationwide) summary tiles. Every
run is exactly one or two BigQuery queries (the top-line summary, plus
one more only when a breakdown is requested) - no per-segment iteration
or Street View/Vision calls, so it's comparatively cheap even though it
scans the full nationwide table (tens of millions of rows; under 1.5GB
processed per query in practice) - see "Async loading and caching" for
how that query itself is run.

Filters are plain GET query parameters (`/speed-limits?table=calc_out.speed_limits_US_2026_08_details&infer_field=speed_limit_infer_mph_corrected&param1=15&states=NC,SC&group_by=state`),
so a particular view is directly linkable/bookmarkable.

### Async loading and caching

The page itself (filters form, table/field dropdowns) renders
immediately on every load - it never blocks on the aggregate query. That
query (and the breakdown query, when requested) runs in a background
thread instead (`app.py`'s `QUALITY_JOBS`, the same pattern the sign
checker's own `/run` background jobs use), with a small progress
indicator shown in its place; the browser polls `/speed-limits/status/<job_id>`
and, once done, fetches the rendered result from
`/speed-limits/fragment/<job_id>` and swaps it in - no full page reload.

Every BigQuery call this page makes is also cached, so an identical
request doesn't re-run it:

- **The aggregate metrics/breakdown query** (`quality_metrics.fetch_quality_metrics`) -
  cached on the selected table's own last-modified time (`bigquery.Client.get_table(...).modified`,
  a metadata GET, not a billed query) alongside every filter - so the
  same request only re-queries BigQuery once the table it's reading has
  actually changed (e.g. `speed_limits_US_latest` gets refreshed), not on
  every page load and not stale forever either.
- **The table list and the inferred-field list** (both
  `INFORMATION_SCHEMA` lookups) - cached for 5 minutes each, since
  there's no single "last modified" signal for a listing across a whole
  dataset/table's schema the way there is for one table's data.

All three share the same disk (and GCS, when `GCS_CACHE_BUCKET` is set -
see "Deploying to Cloud Run") cache mechanism as the rest of this app -
see `bq_cache.py` and "Caching" below.

## Speed-Limits Evaluator

`/speed-limits-evaluator` runs the sign checker's own per-segment logic
(`find_bad_speed_limit._process_one_candidate` - the exact same "walk
positions/sides/headings until a sign is read" work `run_pipeline` does
one candidate at a time) across up to a configurable number of candidate
segments for one state **concurrently**, instead of checking one segment
at a time. It exists for the case the sign checker's single-run UI
doesn't cover well: "how good is this model across hundreds of streets in
a state," not "what's the sign on this one street."

### Starting a run

The launch form (state, year/month, project/dataset, segment count -
default and maximum 1000, concurrency - default 10 parallel workers)
reuses the same selection-criteria checkboxes and walk/side/heading/fov
settings as the sign checker's own form, with the same "what actually
works" defaults. An optional label identifies the run in its history;
left blank, one is generated from the state/month/timestamp.

**Concurrency** doesn't bypass Street View's rate limit - the throttle in
`find_bad_speed_limit.py` (`_streetview_throttle_lock`) is a process-wide
lock all worker threads share, so Street View calls stay correctly paced
regardless of how many workers are running. Higher concurrency mostly
buys overlap on Vision OCR and network latency between segments, which is
still a real speedup, just a safer one than true unpaced parallelism.

### Cost and segment caps

Two hard limits, both enforced server-side regardless of what's typed
into the form:

- **1000 segments per run.** `segment_count` is clamped to this even if
  a request asks for more.
- **$600/day per user.** Every batch run you start counts against your
  own running total for the current UTC day (Street View + Vision calls
  at their real per-1000-call rates) - not per run, across every run you
  start that day. If you're already at or over the cap, a new run is
  refused outright with an explanation. If a run is in progress when your
  day's total crosses the cap (from this run's own usage, or combined
  with others you started), it stops itself partway through rather than
  running to completion - its status page explains why, and the segments
  it already checked are kept (not discarded). The cap resets at
  midnight UTC. The launch page shows how much of today's $600 you've
  already used, right next to the cost/time estimate.

### The cost/time estimate, and why it gets more accurate over time

The launch form shows an estimated cost, time to completion, and total
Street View+Vision call count, updating live as you change the form.
The first time you ever use this page, there's no run history yet, so it
falls back to a rough formula-based guess (labeled as such) - which can
be quite wrong, since it assumes a fixed "typical" number of walked
positions per segment that real segments don't actually match.

Every completed (or cancelled) run records its **real measured usage** -
actual elapsed wall-clock time and actual Street View/Vision call counts
(`find_bad_speed_limit.get_api_call_counts()`, incremented at the exact
call sites that make a real billed request - a cache hit never reaches
them, so this only counts calls that actually happened, not attempts).
`batch_evaluator.run_history_stats()` aggregates every completed run's
real numbers into empirical per-segment call rates and per-call timing,
bucketed by (walk_segment, side_mode, headings count) - and the launch
form uses whichever bucket matches its current settings once at least
one such run exists, falling back to the pooled average across all past
runs for a config with no exact match yet, and only reaching for the
rough formula guess when there's no history at all. The estimator box
always says which of these it used ("based on N past run(s)... with this
same config" vs. "...using the overall average" vs. "no run history yet
- rough guess"), so it's never a mystery how much to trust a given
number. In short: **the more this page gets used, the better its own
estimates get** - there's nothing to configure, it just learns.

### Checking on a run

Every run gets a stable URL (`/speed-limits-evaluator/<batch_id>`)
showing live progress (segments checked, signs found, errors, and - live
- the real elapsed time and real Street View+Vision call count so far,
the same numbers that feed the estimator above), a breakdown table
grouped by `functional_class` (segments checked/matched/no-sign/
no-coverage/errors/match rate per class), a per-segment results table
(functional_class plus the segment's OSM/HERE/inferred/observed-average/
freeflow speed values alongside its match status and when Google captured
the Street View image the result is based on - the same fields the CSV
export carries), and a Cancel button while it's running. This page -
and the run's entry in `/speed-limits-evaluator`'s history table - work
from **durably persisted status**, not just an in-memory job you have to
keep a browser tab open for: status is written to
`output/_batch_jobs/<batch_id>/status.json` (and mirrored to GCS, same as
everywhere else in this app) as the run progresses, so navigating back
later - even in a different browser, even after some time - shows real
state, not a stale in-memory snapshot.

This does **not** make a run resumable across a Cloud Run instance
restart, though - if the one instance running it dies mid-batch, that
batch's background thread dies too, leaving status.json at its last
written snapshot. See "Deploying to Cloud Run" for the `--min-instances=1`
follow-up that would prevent Cloud Run from idling the instance to zero
mid-run in the first place (not yet applied - a standing-cost decision
left to whoever owns that call).

### Results: CSV export and comparing runs over time

Every completed (or cancelled) run's per-segment results are written to
a CSV (`output/_batch_jobs/<batch_id>/results.csv`, downloadable from its
status page) - segment id, match status, matched speed, the segment's
own OSM/HERE/inferred/observed/freeflow values, and a full JSON dump of
its source row for anything not broken out into its own column. The
run's full configuration (every criteria/walk/side/heading/fov setting,
not just the headline label) is recorded alongside it too, specifically
so a later run over the *same* streets - after a model change - can be
compared on equal footing. `/speed-limits-evaluator`'s history table
lists every run (label, state, who ran it - from the `X-Goog-Authenticated-User-Email`
header IAP sets, or "unknown" without IAP in front - started/finished
time, status, counts) so a past run is easy to find again without having
bookmarked its exact URL.

### Images

Each segment's Street View/annotated images are cached exactly the way
the sign checker's own single runs already are (see "Caching" below) -
this doesn't add a second caching mechanism, it's the same
`gcs_cache_push` calls inside the same per-segment code, just invoked
many times concurrently instead of once. After a run finishes, a best-effort
sweep re-pushes every image under that run's output directory to GCS as
a safety net, catching anything an individual push failed on transiently
mid-run.

## Output

For every matched segment (the first one found, or every one if walking
through all candidates), both the CLI and the web app surface:

- The segment's location and full BigQuery row (every column selected
  from `speed_limits_<STATE>_<YEAR>_<MONTH>_details`, not just the speed
  fields used for matching).
- The speed limit read off the sign, and how it was found (OCR match).
- The raw Street View images and an annotated copy with a red box around
  the detected sign text, saved under
  `output/<STATE>_<YEAR>_<MONTH>/<here_segment_id>/`.

Every candidate tried - not just a match - carries this same full row of
source-table data too, since it's useful context even when no sign was
found: the run log prints every field as soon as a candidate is selected
(before Street View/OCR even starts), and the web app's "Candidates
tried" list has a collapsible "Full segment data" section per candidate
with the same fields in a table, same as a match's.

## Resilience

Consecutive requests to either Street View endpoint (metadata and image)
are throttled to at least 100ms apart, so a walk-segment/side-mode run
sampling many positions doesn't fire off dozens of requests back to back
with no pacing at all - a plausible contributor to hitting transient
errors in the first place, on top of retrying them once they happen.
Cached images/coverage don't touch the network at all, so this never
slows down a re-run that's already fetched everything.

A transient 5xx from the Street View Static (image) API - Google's own
server hiccupping, not a bad key or request - is retried a few times and
then that one heading is skipped (logged, not fatal) rather than aborting
the whole run. Likewise, the Street View metadata (coverage-check) API's
own `UNKNOWN_ERROR` status - Google's documented status for "a server
error, the request may succeed if you try again" - is retried and then
treated as no coverage at that position rather than aborting. This
matters most on a long walk-segment/side-mode run with many positions:
one flaky response no longer throws away everything already fetched for
the other dozens of positions, or the progress already made across
earlier candidates in the same run. A real key/billing/quota problem
(REQUEST_DENIED, OVER_QUERY_LIMIT, INVALID_REQUEST, or a 4xx from the
image API) still fails immediately without retrying, since that will
keep failing identically on every position.

## Caching

Every billed API this tool calls - BigQuery, Street View, and Vision -
is cached to disk (and, if `GCS_CACHE_BUCKET` is set, to GCS too - see
"Deploying to Cloud Run") and reused rather than re-fetched:

- **Speed-Limits Quality's BigQuery queries** (`bq_cache.py`, used by
  `quality_metrics.py`) - see "Async loading and caching" above for
  specifics; the general mechanism is the same disk+GCS JSON cache as
  everywhere else, just keyed generically (a table's own last-modified
  time for the aggregate query, a short TTL for schema-shaped listings)
  rather than the sign checker's own year/month-snapshot assumption
  below.
- **The candidates query itself**: a `speed_limits_<STATE>_<YEAR>_<MONTH>_details`
  table is a dated, published monthly snapshot, so the same query against
  it (same state/year/month, same selection criteria, same "candidates to
  try") always returns the same rows - and re-running with the same
  inputs while only tuning something downstream (walk spacing, headings,
  fov, side mode, ...) is by far the most common way this pipeline
  actually gets iterated on. Cached under `_candidates_cache/` in a
  table's output directory, keyed by a hash of the criteria/candidate
  count (or the `here_segment_id` for a direct lookup). Delete that
  subdirectory to force a fresh query. The query itself also only
  requests the columns actually used (`SELECT * EXCEPT (geom)` - the raw
  geometry is never used once its centroid/GeoJSON are computed from it,
  only those derived values are) rather than everything including it, to
  keep each query and its cached result smaller.
- **Street View images**: named deterministically by heading
  (`streetview_heading<N>.jpg`, or `streetview_heading<N>_fov<F>.jpg` when
  "Camera zoom"/`--fov` isn't the default 90) under a segment's output
  directory. If the file already exists there, it's reused - the image at
  a fixed lat/lon/heading/fov never changes. Changing the fov gets its own
  filename rather than overwriting the default-fov one, so re-running with
  a narrower zoom to chase a hard-to-read sign doesn't throw away the
  wider shots already fetched, and a plain re-run still reuses them too.
  Side positions are saved under a `left_<N>m`/`right_<N>m` subdirectory
  naming the actual offset used, not just `left`/`right` - so a different
  offset (whether from changing "Offset distance" manually or from
  auto-detection picking a different width on a re-run) always fetches
  fresh images at the new positions instead of silently reusing images
  from a different offset under the same directory name.
- **Vision OCR results**: cached to a `<image>.ocr.json` sidecar file next
  to each image. If present, it's loaded instead of calling Vision again.

This means re-running the same state/year/month, revisiting a segment with
`--segment-id`, or `--walk-all` scanning candidates that share images with
a prior run all skip the API calls for anything already on disk. Delete a
segment's directory under `output/` (or the whole `output/` tree) to force
a fresh fetch.

## How sign reading works

Google Cloud Vision's `TEXT_DETECTION` is run against Street View images
captured at 4 headings (0/90/180/270°) from the segment's centroid. The
script looks for a numeric token positioned directly below "SPEED"/"LIMIT"
word tokens (the standard US sign layout) rather than just regexing any
2-3 digit number out of the image, since street scenes contain lots of
unrelated numbers (addresses, other signage). This is a heuristic, not a
dedicated sign detector — it works well on clear, unobstructed shots of
standard signs but can miss non-standard signage or signs outside the
captured field of view.

## Deploying to Cloud Run

The web app (not the CLI) can run as a Cloud Run service instead of on
your own machine. This changes a few things from local use, covered
below: where the API key comes from, whether the image/OCR cache
survives a restart, and who can reach the page at all - the app itself
has no login of its own, so that last one matters.

**Two architectural facts that shape the setup**, both because a run
happens in a background thread that outlives the request that started
it (`POST /run` returns immediately; the actual work continues while the
browser polls `/status/<job_id>`):

- **`--no-cpu-throttling` is required, not optional.** Cloud Run's
  default billing model only allocates CPU to an instance while it's
  actively handling a request; a background thread with no request in
  flight would get starved of CPU between polls instead of actually
  making progress. Without this flag, runs will stall or crawl.
- **`--max-instances=1`.** Job/progress state lives in an in-memory dict
  in the one process that started the job (see `app.py`'s `JOBS`) - it
  isn't shared across instances. A second instance handling a `/status`
  poll for a job it never started would report "unknown job". This
  caps the service at one run at a time, which matches how it's meant to
  be used anyway.
- **`--min-instances=1` is not currently set, and probably should be for
  the Speed-Limits Evaluator specifically.** Cloud Run can otherwise idle
  the one instance down to zero when there's no traffic - fine for a
  sign-checker run that finishes in a couple minutes with someone
  watching, much less fine for an Evaluator batch that might run for a
  long while unattended: if the instance gets recycled mid-batch, that
  batch's background thread dies with it. This is a real standing-cost
  tradeoff (an always-on instance vs. scale-to-zero savings), not applied
  here automatically - decide deliberately before adding it.

### Scripted (recommended)

`deploy.sh` runs every step below (APIs, bucket, secret, service account,
IAM roles, deploy, invoker grant) in one shot, and is safe to re-run -
existing resources are detected and left alone rather than recreated.
Set the required environment variables and run it from the
`speed_limit_check/` directory:

```bash
PROJECT_ID=your-deploy-project-id REGION=us-central1 BUCKET_NAME=your-bucket-name MAPS_API_KEY=AIza...your-real-key... INVOKER_EMAIL=you@example.com ./deploy.sh
```

`BQ_PROJECT_ID` is also settable if the `speed_limits_..._details` table
lives in a different project than `PROJECT_ID` (defaults to
`moove-platform-testing-data`). See the top of `deploy.sh` for the full
list of variables. The manual step-by-step version below is exactly what
it runs, useful if you want to understand or customize any individual
piece.

Granting IAM policy on the project is a different permission from
creating the bucket/secret/service account - your own account can lack
it even when those succeed. If so, the script doesn't abort; it grants
what it can, still deploys, and prints exactly which grants need someone
with IAM admin rights on the relevant project/resource. If an admin
grants those directly (to `speed-limit-check-runner@<PROJECT_ID>.iam.gserviceaccount.com`)
instead, set `SKIP_IAM_GRANTS=1` to skip the step cleanly rather than
re-attempt (and fail) grants that are already in place.

### 1. Build and enable APIs

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com storage.googleapis.com secretmanager.googleapis.com \
  bigquery.googleapis.com vision.googleapis.com
```

### 2. Create a GCS bucket for the persistent cache

Cloud Run's local disk doesn't survive a restart or a new revision, which
would otherwise throw away the whole point of the image/OCR cache - not
re-billing Street View/Vision for the same image. Setting `GCS_CACHE_BUCKET`
makes the app write every fetched image and OCR result there too (best
effort - a GCS error is logged and treated as a cache miss, never fails
the run), and check there first when the local copy is missing.

```bash
gcloud storage buckets create gs://YOUR_BUCKET_NAME --location=YOUR_REGION
```

### 3. Put the Maps API key in Secret Manager

```bash
printf '%s' 'AIza...your real key...' | gcloud secrets create speed-limit-check-maps-key --data-file=-
```

### 4. Create a runtime service account and grant it access

```bash
gcloud iam service-accounts create speed-limit-check-runner \
  --display-name="Speed limit sign checker (Cloud Run)"

SA=speed-limit-check-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com

# BigQuery - read the table and run queries (same roles as local ADC setup, step 2 above)
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:$SA" --role="roles/bigquery.dataViewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
# Vision API - gated on serviceusage.services.use against the quota project;
# there's no roles/cloudvision.* predefined role to grant instead
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:$SA" --role="roles/serviceusage.serviceUsageConsumer"
# The cache bucket only, not project-wide storage access
gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET_NAME --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
# Read the Maps API key secret
gcloud secrets add-iam-policy-binding speed-limit-check-maps-key --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

### 5. Deploy

Run this from the `speed_limit_check/` directory (it builds the
`Dockerfile` there via Cloud Build, no local Docker needed):

```bash
gcloud run deploy speed-limit-check \
  --source . \
  --region YOUR_REGION \
  --service-account "$SA" \
  --no-cpu-throttling \
  --max-instances=1 \
  --memory=1Gi \
  --set-env-vars="GCS_CACHE_BUCKET=YOUR_BUCKET_NAME" \
  --set-secrets="GOOGLE_MAPS_API_KEY=speed-limit-check-maps-key:latest"
```

Deliberately no `--iap` here: Cloud Run's native IAP integration is
alpha-track only as of this writing (`gcloud run deploy --iap` errors
with "unrecognized arguments" on stable, and beta doesn't have it
either), and alpha/beta commands are a deliberate choice to avoid for
this deploy - not vetted the way stable is. Setting up IAP itself (a
one-time thing, not part of every deploy) is covered in "Grant access"
below.

### 6. Grant access

IAP fronts the service directly (however it gets enabled - see below) -
no separate load balancer/domain/managed cert needed, unlike IAP in
front of a plain Cloud Run service. Anyone who reaches the service URL
is redirected through a normal Google sign-in first; who's actually let
in past that is controlled by `roles/iap.httpsResourceAccessor` on the
service, not `roles/run.invoker` (`gcloud run services proxy` and
manually granting `run.invoker` are the *pre-IAP* access story -
`deploy.sh` still offers an optional `INVOKER_EMAIL` grant for that
fallback path, but it's not what actually gates access once IAP is on).

**Enabling IAP itself** needs either the alpha-track `--iap` flag on
`gcloud run deploy`/`gcloud run services update`, or the Cloud Console's
"Security" tab for the service - check there rather than reaching for
alpha if you want to avoid it. This deploy has stayed off alpha/beta
deliberately, so if that matters to you too, use the Console. Once
enabled, it appears to be a **persistent, service-level setting** - a
plain stable-track `gcloud run deploy` (the command above) does not
re-specify or reset it, the same way it doesn't reset `--ingress` or
other settings it isn't explicitly passed. Verify this after any deploy
rather than assuming it, though - visit the service's domain and confirm
it still prompts a Google sign-in.

Grant the people (or a whole Workspace domain, via
`--member="domain:yourcompany.com"`) who should be able to use it with
`gcloud iap web add-iam-policy-binding` - the exact invocation for a
Cloud-Run-native IAP-fronted service (as opposed to IAP in front of a
load balancer) is new enough that it's worth confirming against `gcloud
iap web add-iam-policy-binding --help` or the Cloud Console's "Security"
tab for the service rather than trusting a specific flag written down
here going stale.

### After deploying

Every run since has assumed `python app.py` locally; on Cloud Run, use
the service URL instead of `http://127.0.0.1:5050` - visiting it in a
browser now prompts a normal Google sign-in (IAP), no proxy/tunnel
needed. Check logs with:

```bash
gcloud run services logs read speed-limit-check --region YOUR_REGION
```

The CLI (`find_bad_speed_limit.py` run directly) is unaffected by any of
this and keeps working exactly as documented above - it isn't part of
what gets deployed.
