# Speed limit sign checker

Finds road segments in `calc_out.speed_limits_<STATE>_<YEAR>_<MONTH>_details`
where the inferred speed limit disagrees sharply with both HERE and OSM (while
HERE and OSM agree with each other), then tries to verify the real speed
limit by reading a sign in Google Street View imagery at that location.

## Query

```sql
SELECT * FROM `<project>.calc_out.speed_limits_<STATE>_<YEAR>_<MONTH>_details`
WHERE
  abs(speed_limit_infer_mph - speed_limit_here_mph) >= 10
  AND abs(speed_limit_infer_mph - speed_limit_osm_mph) >= 10
  AND functional_class < 6
  AND abs(speed_limit_here_mph - speed_limit_osm_mph) <= 1
ORDER BY abs(speed_limit_infer_mph - speed_limit_here_mph) DESC
```

Candidates are tried in order of largest mismatch. Not every road segment has
Street View coverage or a legible sign in frame, so the script walks down the
ranked list until one works.

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

```bash
python find_bad_speed_limit.py --state NC --year 2026 --month 08
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--project` | `moove-platform-testing-data` | BigQuery project |
| `--dataset` | `calc_out` | BigQuery dataset |
| `--candidates` | `10` | How many top-mismatch rows to try before giving up |
| `--out-dir` | `output` | Where Street View images are saved |
| `--keys-file` | `~/Claude/MooveAI/keys.env` | KEY=VALUE file to load API keys from |

## Output

For the first segment where a sign can be read, the script prints:

- The segment's location and full BigQuery row.
- The speed limit read off the sign, and how it was found (OCR match).
- Paths to the raw Street View images and an annotated copy with a red box
  around the detected sign text, saved under
  `output/<STATE>_<YEAR>_<MONTH>/<here_segment_id>/`.

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
