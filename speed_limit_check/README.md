# Speed limit sign checker

Finds road segments in `calc_out.speed_limits_<STATE>_<YEAR>_<MONTH>_details`
where the inferred speed limit disagrees sharply with both HERE and OSM (while
HERE and OSM agree with each other), then tries to verify the real speed
limit by reading a sign in Google Street View imagery at that location.

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
   - `roles/cloudvision.user` (Vision API)

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
a sign: headings `10` (relative to direction of travel), camera zoom `50`,
walk through all candidates, slow-walk each segment at `2`m spacing, and
sides-only. This is a lot more API calls per candidate than a conservative
starting point would be (roughly `segment_length / 2 × 2 sides` Street View
+ Vision calls each) - lower "Candidates to try" if a run is taking too
long or costing more than expected.

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
| `--side-mode` | `center` | Which perpendicular-offset points to probe at each checked position: `center` (just the point itself), `sides` (only the two offset points, skipping center), or `both` (center plus both sides) |
| `--side-offset-m` | `20` | Perpendicular offset in meters for `--side-mode sides`/`both` |
| `--headings` | `0,90,180,270` | Comma-separated headings (0-359) to capture per position (max 24) - compass degrees, or relative angles if `--headings-relative` is set |
| `--headings-relative` | off | Treat `--headings` as relative to each position's local road bearing (0=ahead, 90=right, 180=behind, 270=left) instead of fixed compass degrees |
| `--fov` | `90` | Street View camera field of view in degrees (10-120) - narrower zooms in, making a distant/small sign more legible to OCR at the cost of a narrower cone per heading |
| `--out-dir` | `output` | Where Street View images are saved |
| `--keys-file` | `~/Claude/MooveAI/keys.env` | KEY=VALUE file to load API keys from |

The CLI always uses the default selection criteria - per-criterion
enable/threshold customization is currently web-UI only.

## Output

For every matched segment (the first one found, or every one if walking
through all candidates), both the CLI and the web app surface:

- The segment's location and full BigQuery row.
- The speed limit read off the sign, and how it was found (OCR match).
- The raw Street View images and an annotated copy with a red box around
  the detected sign text, saved under
  `output/<STATE>_<YEAR>_<MONTH>/<here_segment_id>/`.

## Resilience

A transient 5xx from the Street View Static (image) API - Google's own
server hiccupping, not a bad key or request - is retried a few times and
then that one heading is skipped (logged, not fatal) rather than aborting
the whole run. This matters most on a long walk-segment/side-mode run
with many positions: one flaky image no longer throws away everything
already fetched for the other dozens of positions. A 4xx (bad key,
billing, or malformed request) still fails immediately without retrying,
since that will keep failing identically on every position.

## Caching

Both APIs are billed per call, so a given segment's images and OCR results
are cached to disk and reused rather than re-fetched:

- **Street View images**: named deterministically by heading
  (`streetview_heading<N>.jpg`, or `streetview_heading<N>_fov<F>.jpg` when
  "Camera zoom"/`--fov` isn't the default 90) under a segment's output
  directory. If the file already exists there, it's reused - the image at
  a fixed lat/lon/heading/fov never changes. Changing the fov gets its own
  filename rather than overwriting the default-fov one, so re-running with
  a narrower zoom to chase a hard-to-read sign doesn't throw away the
  wider shots already fetched, and a plain re-run still reuses them too.
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
