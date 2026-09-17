#!/usr/bin/env python3
"""Find a road segment where inferred and vendor speed limits disagree, then try
to verify the real-world speed limit by reading a sign in Street View imagery.

Query logic (against `<project>.calc_out.speed_limits_<STATE>_<YEAR>_<MONTH>_details`):
    abs(speed_limit_infer_mph - speed_limit_here_mph) >= 10
    AND abs(speed_limit_infer_mph - speed_limit_osm_mph) >= 10
    AND functional_class < 6
    AND abs(speed_limit_here_mph - speed_limit_osm_mph) <= 1

i.e. HERE and OSM agree with each other but both disagree with the inferred
value by a lot, on a "real" road (functional_class < 6). Candidates are
ranked by the size of that disagreement; the script walks down the ranked
list until it finds a segment with Street View coverage and a sign it can
read, since not every location has Street View imagery.

Usage:
    python find_bad_speed_limit.py --state NC --year 2026 --month 08

Keys:
    GOOGLE_MAPS_API_KEY (a Street View Static API key) is read from the
    environment if set, otherwise from a `KEY=VALUE` keys file - by default
    ~/Claude/MooveAI/keys.env, overridable with --keys-file. This keeps the
    key out of shell history/env and out of the git repo (the file lives
    above the repo, not inside it).

    BigQuery and Cloud Vision use standard Application Default Credentials
    (`gcloud auth application-default login`, or GOOGLE_APPLICATION_CREDENTIALS)
    - not the keys file.

See README.md for full setup instructions.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import requests
from google.cloud import bigquery, vision
from PIL import Image, ImageDraw

DEFAULT_PROJECT = "moove-platform-testing-data"
DEFAULT_DATASET = "calc_out"
DEFAULT_KEYS_FILE = Path.home() / "Claude" / "MooveAI" / "keys.env"
STREETVIEW_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
STREETVIEW_IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"
DEFAULT_HEADINGS = (0, 90, 180, 270)

SPEED_TOKEN_RE = re.compile(r"^speed$", re.IGNORECASE)
LIMIT_TOKEN_RE = re.compile(r"^limit$", re.IGNORECASE)
NUMBER_TOKEN_RE = re.compile(r"^\d{2,3}$")
INLINE_SIGN_RE = re.compile(r"speed\s*limit\s*(\d{2,3})", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", required=True, help="Two-letter state code, e.g. NC")
    p.add_argument("--year", required=True, help="Year, e.g. 2026")
    p.add_argument("--month", required=True, help="Month, e.g. 08 or 8")
    p.add_argument("--project", default=DEFAULT_PROJECT, help=f"BigQuery project (default: {DEFAULT_PROJECT})")
    p.add_argument("--dataset", default=DEFAULT_DATASET, help=f"BigQuery dataset (default: {DEFAULT_DATASET})")
    p.add_argument("--candidates", type=int, default=10, help="How many top-mismatch rows to try before giving up (default: 10)")
    p.add_argument("--out-dir", default="output", help="Directory to save Street View images into (default: ./output)")
    p.add_argument(
        "--keys-file",
        default=DEFAULT_KEYS_FILE,
        type=Path,
        help=f"KEY=VALUE file to load API keys from if not already in the environment (default: {DEFAULT_KEYS_FILE})",
    )
    return p.parse_args()


def table_name(state: str, year: str, month: str) -> str:
    month_padded = f"{int(month):02d}" if month.isdigit() else month
    return f"speed_limits_{state.upper()}_{year}_{month_padded}_details"


def safe_segment_dirname(segment_id: str) -> str:
    """here_segment_id values look like 'here:cm:segment:412644259' - ':' is
    awkward in file paths (esp. Windows) and needs escaping in URLs, so swap
    it out for a plain filesystem/URL-safe directory name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", segment_id)


def load_keys_file(path: Path) -> None:
    """Load KEY=VALUE lines from `path` into os.environ, without overriding
    anything already set in the environment. Blank lines and lines starting
    with '#' are ignored; surrounding quotes on the value are stripped."""
    if not path.exists():
        return
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"  Warning: ignoring malformed line {lineno} in {path} (expected KEY=VALUE)", file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def fetch_candidates(project: str, dataset: str, table: str, limit: int) -> list[bigquery.table.Row]:
    client = bigquery.Client(project=project)
    query = f"""
        SELECT
          *,
          ST_Y(ST_CENTROID(geom)) AS centroid_lat,
          ST_X(ST_CENTROID(geom)) AS centroid_lon
        FROM `{project}.{dataset}.{table}`
        WHERE
          ABS(speed_limit_infer_mph - speed_limit_here_mph) >= 10
          AND ABS(speed_limit_infer_mph - speed_limit_osm_mph) >= 10
          AND functional_class < 6
          AND ABS(speed_limit_here_mph - speed_limit_osm_mph) <= 1
        ORDER BY ABS(speed_limit_infer_mph - speed_limit_here_mph) DESC
        LIMIT {limit}
    """
    return list(client.query(query).result())


class StreetViewAuthError(RuntimeError):
    """The API key/billing/API-enablement is broken, as opposed to 'no imagery here'."""


def streetview_coverage(lat: float, lon: float, api_key: str) -> Optional[dict]:
    resp = requests.get(STREETVIEW_METADATA_URL, params={"location": f"{lat},{lon}", "key": api_key}, timeout=30)
    resp.raise_for_status()
    meta = resp.json()
    status = meta.get("status")
    if status == "OK":
        return meta
    if status == "ZERO_RESULTS":
        return None
    # REQUEST_DENIED, OVER_QUERY_LIMIT, INVALID_REQUEST, UNKNOWN_ERROR, etc.
    # are all key/billing/quota problems, not "no imagery at this location" -
    # surface them instead of silently treating every candidate as uncovered.
    raise StreetViewAuthError(f"Street View metadata request failed: status={status} error_message={meta.get('error_message')!r}")


def fetch_streetview_images(lat: float, lon: float, api_key: str, out_dir: Path, headings=DEFAULT_HEADINGS) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for heading in headings:
        params = {"size": "640x640", "location": f"{lat},{lon}", "heading": heading, "fov": 90, "pitch": 0, "key": api_key}
        resp = requests.get(STREETVIEW_IMAGE_URL, params=params, timeout=30)
        resp.raise_for_status()
        path = out_dir / f"streetview_heading{heading}.jpg"
        path.write_bytes(resp.content)
        paths.append(path)
    return paths


@dataclasses.dataclass
class SignReading:
    speed_mph: int
    image_path: Path
    box: tuple[int, int, int, int]  # left, top, right, bottom
    evidence: str


def _bbox(vertices) -> tuple[int, int, int, int]:
    xs = [v.x for v in vertices]
    ys = [v.y for v in vertices]
    return min(xs), min(ys), max(xs), max(ys)


def _center(box) -> tuple[float, float]:
    l, t, r, b = box
    return (l + r) / 2, (t + b) / 2


def find_sign_in_image(vision_client: vision.ImageAnnotatorClient, image_path: Path) -> tuple[Optional[SignReading], str]:
    """Look for a 'SPEED LIMIT NN' sign in the image via OCR.

    Prefers a number token that sits directly below SPEED/LIMIT word tokens
    (the standard US sign layout) over a bare regex match on the whole page,
    since street scenes are full of other numbers (addresses, other signs).

    Returns (reading_or_None, raw_ocr_text) - the raw text is returned even
    on a miss, so callers can log what Vision actually saw for debugging
    (e.g. no sign in frame at all, vs. a sign OCR'd in an unexpected layout).
    """
    content = image_path.read_bytes()
    with Image.open(image_path) as im:
        img_h = im.height
    response = vision_client.text_detection(image=vision.Image(content=content))
    if response.error.message:
        print(f"  Vision API error on {image_path.name}: {response.error.message}", file=sys.stderr)
        return None, ""
    annotations = response.text_annotations
    if not annotations:
        return None, ""

    words = annotations[1:]  # [0] is the full-text block
    speed_boxes = [_bbox(w.bounding_poly.vertices) for w in words if SPEED_TOKEN_RE.match(w.description)]
    limit_boxes = [_bbox(w.bounding_poly.vertices) for w in words if LIMIT_TOKEN_RE.match(w.description)]
    number_words = [(w.description, _bbox(w.bounding_poly.vertices)) for w in words if NUMBER_TOKEN_RE.match(w.description)]

    anchor_boxes = limit_boxes or speed_boxes
    best = None
    best_dist = None
    for anchor_box in anchor_boxes:
        ax, ay = _center(anchor_box)
        for text, num_box in number_words:
            nx, ny = _center(num_box)
            # number must be roughly below and horizontally aligned with SPEED/LIMIT
            if ny <= ay or ny - ay > 0.4 * img_h or abs(nx - ax) > 0.5 * (anchor_box[2] - anchor_box[0] + num_box[2] - num_box[0] + 1) * 2:
                continue
            dist = (ny - ay) + abs(nx - ax)
            if best_dist is None or dist < best_dist:
                speed = int(text)
                if 15 <= speed <= 80:
                    l = min(anchor_box[0], num_box[0])
                    t = min(anchor_box[1], num_box[1])
                    r = max(anchor_box[2], num_box[2])
                    b = max(anchor_box[3], num_box[3])
                    best = SignReading(speed, image_path, (l, t, r, b), f"OCR found '{text}' below a SPEED/LIMIT word on the sign")
                    best_dist = dist

    full_text = annotations[0].description

    if best:
        return best, full_text

    m = INLINE_SIGN_RE.search(full_text)
    if m:
        speed = int(m.group(1))
        if 15 <= speed <= 80:
            return SignReading(speed, image_path, (0, 0, 0, 0), f"OCR text contained 'SPEED LIMIT {speed}' (no bounding box available)"), full_text

    return None, full_text


def save_annotated_image(reading: SignReading, out_path: Path) -> None:
    with Image.open(reading.image_path) as im:
        im = im.convert("RGB")
        if reading.box != (0, 0, 0, 0):
            draw = ImageDraw.Draw(im)
            draw.rectangle(reading.box, outline="red", width=6)
        im.save(out_path)


def print_row(row: dict) -> None:
    for key, value in row.items():
        print(f"  {key}: {value}")


def _jsonable_row(row: bigquery.table.Row) -> dict:
    return {k: v for k, v in row.items() if k != "geom"}


class NoUsableApiKey(RuntimeError):
    pass


@dataclasses.dataclass
class CandidateAttempt:
    index: int
    segment_id: str
    lat: float
    lon: float
    status: str  # "no_coverage" | "no_sign_read" | "match"
    note: str = ""
    images: list[Path] = dataclasses.field(default_factory=list)
    ocr_snippets: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class PipelineResult:
    table: str
    project: str
    dataset: str
    attempts: list[CandidateAttempt]
    match_row: Optional[dict] = None
    match_reading: Optional[SignReading] = None
    match_annotated_image: Optional[Path] = None
    match_all_images: Optional[list[Path]] = None


def run_pipeline(
    state: str,
    year: str,
    month: str,
    project: str = DEFAULT_PROJECT,
    dataset: str = DEFAULT_DATASET,
    max_candidates: int = 10,
    out_dir: Path = Path("output"),
    keys_file: Path = DEFAULT_KEYS_FILE,
    log=lambda msg: None,
) -> PipelineResult:
    """Core pipeline, shared by the CLI and the web app: query BigQuery for
    mismatched segments, then walk candidates trying to read a sign at each
    via Street View + Vision OCR. Raises NoUsableApiKey / StreetViewAuthError
    on setup or auth problems; returns normally (with match_row=None) if no
    candidate yields a readable sign."""
    load_keys_file(keys_file)
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key == "your-key-here":
        raise NoUsableApiKey(
            f"No usable GOOGLE_MAPS_API_KEY. Set it in the environment, or add a line "
            f"GOOGLE_MAPS_API_KEY=... to {keys_file}."
        )

    table = table_name(state, year, month)
    log(f"Querying `{project}.{dataset}.{table}` ...")
    candidates = fetch_candidates(project, dataset, table, max_candidates)
    result = PipelineResult(table=table, project=project, dataset=dataset, attempts=[])
    if not candidates:
        log("No road segments matched the mismatch criteria.")
        return result
    log(f"Found {len(candidates)} candidate segment(s); trying them in order of largest mismatch.")

    vision_client = vision.ImageAnnotatorClient()
    out_root = Path(out_dir) / f"{state.upper()}_{year}_{month}"

    for i, row in enumerate(candidates):
        lat, lon = row["centroid_lat"], row["centroid_lon"]
        segment_id = row["here_segment_id"]
        log(f"[{i + 1}/{len(candidates)}] here_segment_id={segment_id} at ({lat:.6f}, {lon:.6f})")

        coverage = streetview_coverage(lat, lon, api_key)  # StreetViewAuthError propagates to caller
        if not coverage:
            result.attempts.append(CandidateAttempt(i + 1, segment_id, lat, lon, "no_coverage"))
            log("  No Street View coverage here, trying next candidate.")
            continue

        seg_dir = out_root / safe_segment_dirname(segment_id)
        image_paths = fetch_streetview_images(lat, lon, api_key, seg_dir)

        reading = None
        ocr_snippets: list[str] = []
        for image_path in image_paths:
            reading, ocr_text = find_sign_in_image(vision_client, image_path)
            snippet = " / ".join(ocr_text.split("\n")[:6])[:200] or "(no text detected)"
            ocr_snippets.append(snippet)
            log(f"    {image_path.name}: OCR saw: {snippet}")
            if reading:
                break

        if not reading:
            result.attempts.append(
                CandidateAttempt(i + 1, segment_id, lat, lon, "no_sign_read", images=image_paths, ocr_snippets=ocr_snippets)
            )
            log("  Street View imagery found, but no speed limit sign could be read in it. Trying next candidate.")
            continue

        annotated_path = seg_dir / "sign_detected.jpg"
        save_annotated_image(reading, annotated_path)

        result.attempts.append(
            CandidateAttempt(i + 1, segment_id, lat, lon, "match", reading.evidence, images=image_paths, ocr_snippets=ocr_snippets)
        )
        result.match_row = _jsonable_row(row)
        result.match_row["centroid_lat"] = lat
        result.match_row["centroid_lon"] = lon
        result.match_reading = reading
        result.match_annotated_image = annotated_path
        result.match_all_images = image_paths
        log(f"Match found: {reading.speed_mph} mph on segment {segment_id}.")
        return result

    log("Exhausted all candidates without finding a readable speed limit sign.")
    return result


def main() -> int:
    args = parse_args()

    try:
        result = run_pipeline(
            args.state,
            args.year,
            args.month,
            project=args.project,
            dataset=args.dataset,
            max_candidates=args.candidates,
            out_dir=Path(args.out_dir),
            keys_file=args.keys_file,
            log=print,
        )
    except NoUsableApiKey as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except StreetViewAuthError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        print(
            "This means the API key, billing, or API enablement is broken - not that "
            "there's no imagery. Check that GOOGLE_MAPS_API_KEY is a real key with the "
            "Street View Static API enabled and billing active on its project.",
            file=sys.stderr,
        )
        return 1

    if not result.match_row:
        return 1

    reading = result.match_reading
    print("\n=== Match found ===")
    print(f"Segment: here_segment_id={result.match_row['here_segment_id']}  street_name={result.match_row['street_name']}")
    print(f"Location: {result.match_row['centroid_lat']:.6f}, {result.match_row['centroid_lon']:.6f}")
    print(f"Sign reading: {reading.speed_mph} mph  ({reading.evidence})")
    print(f"Annotated image: {result.match_annotated_image}")
    print(f"Raw Street View image: {reading.image_path}")
    print("\nFull row data:")
    print_row(result.match_row)
    print(f"\nRow as JSON: {json.dumps(result.match_row, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
