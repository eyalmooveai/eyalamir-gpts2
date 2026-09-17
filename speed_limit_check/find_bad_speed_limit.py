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

# Validation for anything that gets interpolated into SQL as an identifier
# rather than passed as a query parameter (BigQuery can't parameterize table/
# dataset/project names), so these need to be checked before use.
STATE_RE = re.compile(r"^[A-Za-z]{2}$")
YEAR_RE = re.compile(r"^\d{4}$")
MONTH_RE = re.compile(r"^\d{1,2}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{4,61}[A-Za-z0-9]$")
DATASET_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
SEGMENT_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,200}$")

# Selectable WHERE-clause criteria for the candidate query, each independently
# toggleable with an editable threshold in the web UI. `sql` references its
# own BigQuery query parameter by `key`; `magnitude`/`order_expr` mark the
# "how big is the mismatch" clauses usable for ORDER BY (largest first).
CRITERIA_DEFS = [
    {
        "key": "functional_class_lt",
        "label": "functional_class < N (lower = bigger/more important road)",
        "sql": "functional_class < @functional_class_lt",
        "default_enabled": True,
        "default_value": 6,
    },
    {
        "key": "here_osm_agree",
        "label": "|HERE − OSM| ≤ N mph (HERE and OSM agree with each other)",
        "sql": "ABS(speed_limit_here_mph - speed_limit_osm_mph) <= @here_osm_agree",
        "default_enabled": True,
        "default_value": 1,
    },
    {
        "key": "infer_vs_here",
        "label": "|infer − HERE| ≥ N mph",
        "sql": "ABS(speed_limit_infer_mph - speed_limit_here_mph) >= @infer_vs_here",
        "default_enabled": True,
        "default_value": 10,
        "magnitude": True,
        "order_expr": "ABS(speed_limit_infer_mph - speed_limit_here_mph)",
    },
    {
        "key": "infer_vs_osm",
        "label": "|infer − OSM| ≥ N mph",
        "sql": "ABS(speed_limit_infer_mph - speed_limit_osm_mph) >= @infer_vs_osm",
        "default_enabled": True,
        "default_value": 10,
        "magnitude": True,
        "order_expr": "ABS(speed_limit_infer_mph - speed_limit_osm_mph)",
    },
    {
        "key": "infer_corrected_vs_here",
        "label": "|infer_corrected − HERE| ≥ N mph",
        "sql": "ABS(speed_limit_infer_mph_corrected - speed_limit_here_mph) >= @infer_corrected_vs_here",
        "default_enabled": False,
        "default_value": 5,
        "magnitude": True,
        "order_expr": "ABS(speed_limit_infer_mph_corrected - speed_limit_here_mph)",
    },
    {
        "key": "infer_corrected_vs_osm",
        "label": "|infer_corrected − OSM| ≥ N mph",
        "sql": "ABS(speed_limit_infer_mph_corrected - speed_limit_osm_mph) >= @infer_corrected_vs_osm",
        "default_enabled": False,
        "default_value": 5,
        "magnitude": True,
        "order_expr": "ABS(speed_limit_infer_mph_corrected - speed_limit_osm_mph)",
    },
]


def default_criteria() -> dict[str, tuple[bool, float]]:
    return {c["key"]: (c["default_enabled"], c["default_value"]) for c in CRITERIA_DEFS}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", required=True, help="Two-letter state code, e.g. NC")
    p.add_argument("--year", required=True, help="Year, e.g. 2026")
    p.add_argument("--month", required=True, help="Month, e.g. 08 or 8")
    p.add_argument("--project", default=DEFAULT_PROJECT, help=f"BigQuery project (default: {DEFAULT_PROJECT})")
    p.add_argument("--dataset", default=DEFAULT_DATASET, help=f"BigQuery dataset (default: {DEFAULT_DATASET})")
    p.add_argument("--candidates", type=int, default=10, help="How many top-mismatch rows to try before giving up (default: 10)")
    p.add_argument("--segment-id", default=None, help="Check one specific here_segment_id instead of running the mismatch query")
    p.add_argument(
        "--walk-all",
        action="store_true",
        help="Don't stop at the first readable sign - process every candidate and report every match found",
    )
    p.add_argument("--out-dir", default="output", help="Directory to save Street View images into (default: ./output)")
    p.add_argument(
        "--keys-file",
        default=DEFAULT_KEYS_FILE,
        type=Path,
        help=f"KEY=VALUE file to load API keys from if not already in the environment (default: {DEFAULT_KEYS_FILE})",
    )
    return p.parse_args()


def _validate_identifier(value: str, pattern: re.Pattern, field_name: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return value


def table_name(state: str, year: str, month: str) -> str:
    """Builds the BigQuery table name from state/year/month. These can't be
    passed as query parameters (BigQuery doesn't parameterize identifiers),
    so they're validated here before being interpolated into SQL."""
    _validate_identifier(state, STATE_RE, "state (expected 2 letters, e.g. NC)")
    _validate_identifier(year, YEAR_RE, "year (expected 4 digits, e.g. 2026)")
    _validate_identifier(month, MONTH_RE, "month (expected 1-2 digits, e.g. 08)")
    month_num = int(month)
    if not 1 <= month_num <= 12:
        raise ValueError(f"Invalid month: {month!r} (must be 01-12)")
    return f"speed_limits_{state.upper()}_{year}_{month_num:02d}_details"


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


def build_candidates_query(
    project: str, dataset: str, table: str, criteria: dict[str, tuple[bool, float]], limit: int
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    where_parts = []
    params = []
    order_expr = None
    for c in CRITERIA_DEFS:
        enabled, value = criteria.get(c["key"], (c["default_enabled"], c["default_value"]))
        if not enabled:
            continue
        where_parts.append(c["sql"])
        params.append(bigquery.ScalarQueryParameter(c["key"], "FLOAT64", float(value)))
        if order_expr is None and c.get("magnitude"):
            order_expr = c["order_expr"]
    where_sql = " AND ".join(where_parts) if where_parts else "TRUE"
    order_sql = f"ORDER BY {order_expr} DESC" if order_expr else "ORDER BY here_segment_id"
    query = f"""
        SELECT
          *,
          ST_Y(ST_CENTROID(geom)) AS centroid_lat,
          ST_X(ST_CENTROID(geom)) AS centroid_lon
        FROM `{project}.{dataset}.{table}`
        WHERE {where_sql}
        {order_sql}
        LIMIT {int(limit)}
    """
    return query, params


def fetch_candidates(
    project: str, dataset: str, table: str, criteria: dict[str, tuple[bool, float]], limit: int
) -> list[bigquery.table.Row]:
    client = bigquery.Client(project=project)
    query, params = build_candidates_query(project, dataset, table, criteria, limit)
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return list(client.query(query, job_config=job_config).result())


def fetch_candidate_by_id(project: str, dataset: str, table: str, segment_id: str) -> list[bigquery.table.Row]:
    _validate_identifier(segment_id, SEGMENT_ID_RE, "here_segment_id")
    client = bigquery.Client(project=project)
    query = f"""
        SELECT
          *,
          ST_Y(ST_CENTROID(geom)) AS centroid_lat,
          ST_X(ST_CENTROID(geom)) AS centroid_lon
        FROM `{project}.{dataset}.{table}`
        WHERE here_segment_id = @segment_id
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("segment_id", "STRING", segment_id)])
    return list(client.query(query, job_config=job_config).result())


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
    annotated_image: Optional[Path] = None


@dataclasses.dataclass
class Match:
    row: dict
    reading: SignReading
    annotated_image: Path
    all_images: list[Path]


@dataclasses.dataclass
class PipelineResult:
    table: str
    project: str
    dataset: str
    attempts: list[CandidateAttempt]
    matches: list[Match] = dataclasses.field(default_factory=list)


def run_pipeline(
    state: str,
    year: str,
    month: str,
    project: str = DEFAULT_PROJECT,
    dataset: str = DEFAULT_DATASET,
    max_candidates: int = 10,
    criteria: Optional[dict[str, tuple[bool, float]]] = None,
    segment_id: Optional[str] = None,
    walk_all: bool = False,
    out_dir: Path = Path("output"),
    keys_file: Path = DEFAULT_KEYS_FILE,
    log=lambda msg: None,
    progress=lambda fraction, message: None,
) -> PipelineResult:
    """Core pipeline, shared by the CLI and the web app: query BigQuery for
    segments matching `criteria` (or look up one specific `segment_id`
    instead), then walk candidates trying to read a sign at each via Street
    View + Vision OCR - stopping at the first match unless `walk_all` is set,
    in which case every candidate is tried and every match kept. Raises
    NoUsableApiKey / StreetViewAuthError on setup or auth problems, or
    ValueError on invalid state/year/month/project/dataset/segment_id;
    returns normally (with matches=[]) if no candidate yields a readable
    sign.

    `progress(fraction, message)` is called throughout with fraction in
    [0, 1] and a human-readable status - e.g. for a web UI progress bar.
    It's a coarse estimate (evenly dividing 1.0 across candidates, and each
    candidate's slice across its coverage-check/download/OCR steps), not a
    measurement of actual API latency.
    """
    progress(0.0, "Starting...")
    load_keys_file(keys_file)
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key == "your-key-here":
        raise NoUsableApiKey(
            f"No usable GOOGLE_MAPS_API_KEY. Set it in the environment, or add a line "
            f"GOOGLE_MAPS_API_KEY=... to {keys_file}."
        )

    _validate_identifier(project, PROJECT_RE, "project")
    _validate_identifier(dataset, DATASET_RE, "dataset")
    table = table_name(state, year, month)
    criteria = criteria if criteria is not None else default_criteria()

    if segment_id:
        segment_id = segment_id.strip()
        log(f"Looking up segment `{segment_id}` in `{project}.{dataset}.{table}` ...")
        progress(0.0, f"Looking up segment {segment_id}...")
        candidates = fetch_candidate_by_id(project, dataset, table, segment_id)
    else:
        log(f"Querying `{project}.{dataset}.{table}` ...")
        progress(0.0, f"Querying `{project}.{dataset}.{table}` ...")
        candidates = fetch_candidates(project, dataset, table, criteria, max_candidates)

    result = PipelineResult(table=table, project=project, dataset=dataset, attempts=[])
    if not candidates:
        message = "Segment not found in this table." if segment_id else "No road segments matched the selection criteria."
        log(message)
        progress(1.0, message)
        return result
    log(f"Found {len(candidates)} candidate segment(s).")
    progress(0.02, f"Found {len(candidates)} candidate segment(s).")

    vision_client = vision.ImageAnnotatorClient()
    out_root = Path(out_dir) / f"{state.upper()}_{year}_{month}"
    n = len(candidates)
    span = 1.0 / n

    for i, row in enumerate(candidates):
        lat, lon = row["centroid_lat"], row["centroid_lon"]
        seg_id = row["here_segment_id"]
        base = i * span
        tag = f"[{i + 1}/{n}]"
        log(f"{tag} here_segment_id={seg_id} at ({lat:.6f}, {lon:.6f})")
        progress(base, f"{tag} Checking Street View coverage for {seg_id}...")

        coverage = streetview_coverage(lat, lon, api_key)  # StreetViewAuthError propagates to caller
        if not coverage:
            result.attempts.append(CandidateAttempt(i + 1, seg_id, lat, lon, "no_coverage"))
            log("  No Street View coverage here, trying next candidate.")
            progress(base + span, f"{tag} No Street View coverage, trying next candidate...")
            continue

        seg_dir = out_root / safe_segment_dirname(seg_id)
        progress(base + 0.3 * span, f"{tag} Downloading Street View imagery...")
        image_paths = fetch_streetview_images(lat, lon, api_key, seg_dir)

        reading = None
        ocr_snippets: list[str] = []
        for k, image_path in enumerate(image_paths):
            progress(base + (0.4 + 0.5 * (k + 1) / len(image_paths)) * span, f"{tag} Running OCR on image {k + 1}/{len(image_paths)}...")
            reading, ocr_text = find_sign_in_image(vision_client, image_path)
            snippet = " / ".join(ocr_text.split("\n")[:6])[:200] or "(no text detected)"
            ocr_snippets.append(snippet)
            log(f"    {image_path.name}: OCR saw: {snippet}")
            if reading:
                break

        if not reading:
            result.attempts.append(
                CandidateAttempt(i + 1, seg_id, lat, lon, "no_sign_read", images=image_paths, ocr_snippets=ocr_snippets)
            )
            log("  Street View imagery found, but no speed limit sign could be read in it. Trying next candidate.")
            progress(base + span, f"{tag} No readable sign, trying next candidate...")
            continue

        annotated_path = seg_dir / "sign_detected.jpg"
        save_annotated_image(reading, annotated_path)

        result.attempts.append(
            CandidateAttempt(
                i + 1, seg_id, lat, lon, "match", reading.evidence,
                images=image_paths, ocr_snippets=ocr_snippets, annotated_image=annotated_path,
            )
        )
        match_row = _jsonable_row(row)
        match_row["centroid_lat"] = lat
        match_row["centroid_lon"] = lon
        result.matches.append(Match(row=match_row, reading=reading, annotated_image=annotated_path, all_images=image_paths))
        log(f"Match found: {reading.speed_mph} mph on segment {seg_id}.")

        if not walk_all:
            progress(1.0, f"Match found: {reading.speed_mph} mph.")
            return result
        progress(base + span, f"{tag} Match found: {reading.speed_mph} mph. Continuing...")

    if result.matches:
        message = f"Done. {len(result.matches)} match(es) found out of {n} candidate(s)."
    else:
        message = "Exhausted all candidates without finding a readable speed limit sign."
    log(message)
    progress(1.0, message)
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
            segment_id=args.segment_id,
            walk_all=args.walk_all,
            out_dir=Path(args.out_dir),
            keys_file=args.keys_file,
            log=print,
        )
    except (NoUsableApiKey, ValueError) as e:
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

    if not result.matches:
        return 1

    for m in result.matches:
        print("\n=== Match found ===")
        print(f"Segment: here_segment_id={m.row['here_segment_id']}  street_name={m.row['street_name']}")
        print(f"Location: {m.row['centroid_lat']:.6f}, {m.row['centroid_lon']:.6f}")
        print(f"Sign reading: {m.reading.speed_mph} mph  ({m.reading.evidence})")
        print(f"Annotated image: {m.annotated_image}")
        print(f"Raw Street View image: {m.reading.image_path}")
        print("\nFull row data:")
        print_row(m.row)
        print(f"\nRow as JSON: {json.dumps(m.row, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
