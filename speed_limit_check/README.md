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

3. **Street View Static API key**

   Create a Google Maps Platform API key with the **Street View Static API**
   enabled. Store it in a keys file *outside* this git repo, at
   `~/Claude/MooveAI/keys.env` (i.e. one directory above the repo checkout):

   ```bash
   cp keys.env.example ~/Claude/MooveAI/keys.env
   # then edit ~/Claude/MooveAI/keys.env and fill in:
   #   GOOGLE_MAPS_API_KEY=AIza...your real key...
   ```

   The script reads this file automatically. An environment variable of the
   same name, if already set, takes precedence over the file. Use
   `--keys-file` to point at a different path.

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

Additional options on the form:

- **Selection criteria** - each of the criteria listed under "Query" above
  has its own checkbox and editable threshold; any combination can be on.
- **Check one specific segment** - paste a `here_segment_id` (e.g.
  `here:cm:segment:412644259`) to check just that segment directly,
  bypassing the criteria entirely.
- **Walk through all candidates** - by default the run stops at the first
  segment with a readable sign; check this to instead try every candidate
  up to "Candidates to try" and report every match found.

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

## Caching

Both APIs are billed per call, so a given segment's images and OCR results
are cached to disk and reused rather than re-fetched:

- **Street View images**: named deterministically by heading
  (`streetview_heading<N>.jpg`) under a segment's output directory. If the
  file already exists there, it's reused - the image at a fixed lat/lon/
  heading never changes.
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
