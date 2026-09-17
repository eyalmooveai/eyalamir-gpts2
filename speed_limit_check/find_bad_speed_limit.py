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
import math
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
SIDE_MODES = ("center", "sides", "both")

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
    p.add_argument(
        "--walk-segment",
        action="store_true",
        help="Instead of checking only the segment's centroid, sample several positions along its full length "
        "(a sign may be located away from the centroid)",
    )
    p.add_argument(
        "--walk-segment-spacing-m",
        type=float,
        default=15.0,
        help="Target distance in meters between sampled positions when --walk-segment is set (default: 15) - "
        "the point count is derived from this and the segment's actual length, not a fixed count, so a short "
        "segment isn't under-sampled and a long one isn't wastefully over-sampled",
    )
    p.add_argument(
        "--side-mode",
        choices=SIDE_MODES,
        default="center",
        help="Which perpendicular-offset points to probe at each checked position: 'center' (default, just the "
        "point itself), 'sides' (only the two points offset --side-offset-m to either side, skipping the center "
        "- e.g. for a divided road where each here_segment_id spans two separate trunks/carriageways and you "
        "only want the side ones), or 'both' (center plus both sides)",
    )
    p.add_argument(
        "--side-offset-m",
        type=float,
        default=20.0,
        help="Perpendicular offset in meters for --side-mode sides/both (default: 20)",
    )
    p.add_argument("--out-dir", default="output", help="Directory to save Street View images into (default: ./output)")
    p.add_argument(
        "--headings",
        type=parse_headings,
        default=DEFAULT_HEADINGS,
        help=f"Comma-separated headings (0-359) to capture per position (default: "
        f"{','.join(str(h) for h in DEFAULT_HEADINGS)}). More headings costs more Street View + Vision "
        f"calls per position but improves the odds of catching a sign at an odd angle. Compass degrees "
        f"unless --headings-relative is set.",
    )
    p.add_argument(
        "--headings-relative",
        action="store_true",
        help="Interpret --headings relative to the road's local direction of travel (0=ahead, 90=right, "
        "180=behind, 270=left) instead of as fixed compass degrees",
    )
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


MAX_HEADINGS = 24


def parse_headings(raw: str) -> tuple[int, ...]:
    """Parses a comma-separated list of compass headings (e.g. "0,90,180,270")
    into a deduped, order-preserving tuple of ints normalized to [0, 359).
    Raises ValueError on anything that doesn't parse, is empty, or exceeds
    MAX_HEADINGS (a runaway list would multiply Street View + Vision calls
    per position by that many)."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("No headings given (expected e.g. '0,90,180,270')")
    seen: list[int] = []
    for p in parts:
        try:
            v = int(round(float(p))) % 360
        except ValueError:
            raise ValueError(f"Invalid heading {p!r} (expected a number 0-359)")
        if v not in seen:
            seen.append(v)
    if len(seen) > MAX_HEADINGS:
        raise ValueError(f"Too many headings ({len(seen)}) - {MAX_HEADINGS} max")
    return tuple(seen)


HEADING_FROM_FILENAME_RE = re.compile(r"heading(\d+)")


def heading_from_filename(path: Path) -> Optional[int]:
    """Street View images are saved as streetview_heading<N>.jpg - recovers
    the compass heading (0=N, 90=E, 180=S, 270=W) from that filename, e.g.
    for pairing each image with a direction arrow on a map."""
    m = HEADING_FROM_FILENAME_RE.search(path.stem)
    return int(m.group(1)) if m else None


def _haversine_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Distance in meters between two (lon, lat) points."""
    lon1, lat1 = p1
    lon2, lat2 = p2
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_deg(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Compass bearing (0=N, 90=E, ...) from p1 to p2, both (lon, lat)."""
    lon1, lat1 = p1
    lon2, lat2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Point `distance_m` meters from (lat, lon) along `bearing_deg` (forward geodesic, spherical)."""
    r = 6371000.0
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d_r = distance_m / r
    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(d_r) * math.cos(lat1), math.cos(d_r) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def line_coords_from_geojson(geojson_str: Optional[str]) -> list[tuple[float, float]]:
    """Extracts [(lon, lat), ...] vertices, in order, from a GeoJSON string
    (as produced by BigQuery's ST_ASGEOJSON). Handles LineString and
    MultiLineString (HERE/OSM segments are sometimes split into multiple
    parts - concatenated here into one path); anything else (e.g. a bare
    Point) yields an empty list, meaning "no line to walk"."""
    if not geojson_str:
        return []
    try:
        geom = json.loads(geojson_str)
    except (TypeError, ValueError):
        return []
    gtype = geom.get("type")
    if gtype == "LineString":
        return [tuple(c[:2]) for c in geom.get("coordinates", [])]
    if gtype == "MultiLineString":
        coords: list[tuple[float, float]] = []
        for part in geom.get("coordinates", []):
            coords.extend(tuple(c[:2]) for c in part)
        return coords
    return []


def overall_bearing(coords: list[tuple[float, float]]) -> float:
    """Bearing from the first to last vertex of a line - a reasonable
    approximation of "which way the road runs" for a short segment when a
    per-point bearing isn't available (e.g. the single-centroid case)."""
    if len(coords) < 2:
        return 0.0
    return _bearing_deg(coords[0], coords[-1])


def line_length_m(coords: list[tuple[float, float]]) -> float:
    return sum(_haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def points_for_spacing(coords: list[tuple[float, float]], spacing_m: float, min_points: int = 2, max_points: int = 40) -> int:
    """How many points `sample_points_along_line` needs to keep consecutive
    points roughly `spacing_m` apart over this line's actual length, e.g.
    so a short segment isn't under-sampled and a long one isn't wastefully
    over-sampled by a single fixed count regardless of length."""
    length = line_length_m(coords)
    if length <= 0 or spacing_m <= 0:
        return min_points
    return max(min_points, min(max_points, round(length / spacing_m) + 1))


def sample_points_along_line(coords: list[tuple[float, float]], num_points: int) -> list[tuple[float, float, float]]:
    """Given [(lon, lat), ...] line vertices, returns `num_points`
    (lat, lon, bearing_deg) points evenly spaced BY DISTANCE along the line
    (not by vertex index, since a line's vertices are rarely evenly
    spaced), always including the first and last vertex. `bearing_deg` is
    the direction of travel at that point (the line segment it falls on),
    e.g. for offsetting perpendicular to the road."""
    num_points = max(2, num_points)
    if len(coords) < 2:
        if not coords:
            return []
        lon, lat = coords[0]
        return [(lat, lon, 0.0)] * num_points

    seg_lengths = [_haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    total = sum(seg_lengths)
    if total == 0:
        lon, lat = coords[0]
        return [(lat, lon, 0.0)] * num_points

    points = []
    seg_idx = 0
    cum_before_seg = 0.0
    for i in range(num_points):
        target = total * i / (num_points - 1)
        while seg_idx < len(seg_lengths) - 1 and cum_before_seg + seg_lengths[seg_idx] < target:
            cum_before_seg += seg_lengths[seg_idx]
            seg_idx += 1
        seg_len = seg_lengths[seg_idx] or 1e-9
        t = min(max((target - cum_before_seg) / seg_len, 0.0), 1.0)
        lon1, lat1 = coords[seg_idx]
        lon2, lat2 = coords[seg_idx + 1]
        bearing = _bearing_deg(coords[seg_idx], coords[seg_idx + 1])
        points.append((lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t, bearing))
    return points


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
          ST_X(ST_CENTROID(geom)) AS centroid_lon,
          ST_ASGEOJSON(geom) AS geom_geojson
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
          ST_X(ST_CENTROID(geom)) AS centroid_lon,
          ST_ASGEOJSON(geom) AS geom_geojson
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


def fetch_streetview_images(
    lat: float, lon: float, api_key: str, out_dir: Path, headings=DEFAULT_HEADINGS, log=lambda msg: None
) -> list[Path]:
    """Downloads one Street View Static image per heading into `out_dir`,
    named deterministically by heading (streetview_heading<N>.jpg). If a
    file already exists there (e.g. from a prior run over the same segment),
    it's reused instead of re-fetching - the API is billed per call, and the
    image at a given lat/lon/heading never changes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for heading in headings:
        path = out_dir / f"streetview_heading{heading}.jpg"
        if path.exists() and path.stat().st_size > 0:
            log(f"    {path.name}: using cached image")
            paths.append(path)
            continue
        params = {"size": "640x640", "location": f"{lat},{lon}", "heading": heading, "fov": 90, "pitch": 0, "key": api_key}
        resp = requests.get(STREETVIEW_IMAGE_URL, params=params, timeout=30)
        resp.raise_for_status()
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


def _ocr_cache_path(image_path: Path) -> Path:
    return image_path.with_name(image_path.name + ".ocr.json")


def _get_ocr_data(vision_client: vision.ImageAnnotatorClient, image_path: Path, log=lambda msg: None) -> dict:
    """Runs (or reuses a cached) Vision text_detection on `image_path`,
    returning {"img_h": int, "full_text": str, "words": [[text, [l,t,r,b]], ...]}.

    Cached to a `<image>.ocr.json` sidecar file next to the image, since
    Vision is billed per call and the OCR result for a given image never
    changes - a re-run (or --walk-all revisiting a prior run's images)
    should never re-call it for the same file.
    """
    cache_path = _ocr_cache_path(image_path)
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            log(f"    {image_path.name}: using cached OCR result")
            return data
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass  # corrupt cache file - fall through and re-fetch

    content = image_path.read_bytes()
    with Image.open(image_path) as im:
        img_h = im.height
    response = vision_client.text_detection(image=vision.Image(content=content))
    if response.error.message:
        print(f"  Vision API error on {image_path.name}: {response.error.message}", file=sys.stderr)
        data = {"img_h": img_h, "full_text": "", "words": []}
    else:
        annotations = response.text_annotations
        full_text = annotations[0].description if annotations else ""
        words = [[w.description, list(_bbox(w.bounding_poly.vertices))] for w in annotations[1:]]
        data = {"img_h": img_h, "full_text": full_text, "words": words}

    try:
        cache_path.write_text(json.dumps(data))
    except OSError:
        pass  # caching is an optimization, not a requirement - don't fail the run over it
    return data


def find_sign_in_image(
    vision_client: vision.ImageAnnotatorClient, image_path: Path, log=lambda msg: None
) -> tuple[Optional[SignReading], str]:
    """Look for a 'SPEED LIMIT NN' sign in the image via OCR.

    Prefers a number token that sits directly below SPEED/LIMIT word tokens
    (the standard US sign layout) over a bare regex match on the whole page,
    since street scenes are full of other numbers (addresses, other signs).

    Returns (reading_or_None, raw_ocr_text) - the raw text is returned even
    on a miss, so callers can log what Vision actually saw for debugging
    (e.g. no sign in frame at all, vs. a sign OCR'd in an unexpected layout).
    """
    data = _get_ocr_data(vision_client, image_path, log=log)
    img_h = data["img_h"]
    full_text = data["full_text"]
    words = [(text, tuple(box)) for text, box in data["words"]]
    if not full_text and not words:
        return None, ""

    speed_boxes = [box for text, box in words if SPEED_TOKEN_RE.match(text)]
    limit_boxes = [box for text, box in words if LIMIT_TOKEN_RE.match(text)]
    number_words = [(text, box) for text, box in words if NUMBER_TOKEN_RE.match(text)]

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
    return {k: v for k, v in row.items() if k not in ("geom", "geom_geojson")}


class NoUsableApiKey(RuntimeError):
    pass


@dataclasses.dataclass
class ImageDetail:
    """One fetched Street View image and what OCR found in it, including
    where it was actually taken - which, with --walk-segment, can differ
    per image within the same candidate segment."""

    path: Path
    lat: float
    lon: float
    heading: Optional[int]
    ocr_snippet: str = ""


@dataclasses.dataclass
class CandidateAttempt:
    index: int
    segment_id: str
    lat: float  # the segment's centroid - used for the summary line/ordering
    lon: float
    status: str  # "no_coverage" | "no_sign_read" | "match"
    note: str = ""
    image_details: list[ImageDetail] = dataclasses.field(default_factory=list)
    annotated_image: Optional[Path] = None
    matched_image_index: Optional[int] = None  # index into image_details


@dataclasses.dataclass
class Match:
    row: dict
    reading: SignReading
    annotated_image: Path
    image_details: list[ImageDetail]
    matched_image_index: int


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
    walk_segment: bool = False,
    walk_segment_spacing_m: float = 15.0,
    side_mode: str = "center",
    side_offset_m: float = 20.0,
    headings: tuple[int, ...] = DEFAULT_HEADINGS,
    headings_relative: bool = False,
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

    By default each candidate is checked only at its centroid. With
    `walk_segment=True`, its full line geometry is instead sampled at
    positions spaced roughly `walk_segment_spacing_m` meters apart (the
    count is derived from that and the segment's own length, clamped to
    [2, 40] points, rather than a fixed count regardless of length), and
    each is checked in turn - a sign relevant to the segment may sit well
    away from its centroid. This multiplies API
    calls by roughly that many points, so it costs more and takes longer.

    `side_mode` controls which perpendicular-offset points are probed at
    each checked position (whether just the centroid or every walked
    point): `"center"` (default) checks only the position itself;
    `"both"` additionally probes two points offset `side_offset_m` meters
    perpendicular to the road on either side; `"sides"` probes only
    those two offset points and skips the center. Street View's
    nearest-panorama snapping means sampling only the centerline can keep
    returning the same one carriageway of a divided road even as you walk
    its length - a sign on the other carriageway, a short perpendicular
    distance away, is otherwise never reached. Some here_segment_ids cover
    two genuinely separate trunks/carriageways where the center point's
    own Street View coverage is on neither one of interest, in which case
    `"sides"` avoids wasting calls on it.

    `headings` (default 0/90/180/270, i.e. N/E/S/W) sets which directions
    get captured at every position checked. More headings costs more
    Street View + Vision calls per position but improves the odds of
    catching a sign at an angle that falls between the default four. With
    `headings_relative=True`, each value is instead interpreted relative
    to the road's own local direction of travel at that position (0=ahead,
    90=right, 180=behind, 270=left) rather than as a fixed compass degree
    - useful since "which compass direction the sign faces" varies with
    how the road happens to run, but "the sign is off to the right ahead"
    doesn't. The local bearing comes from the segment's line geometry
    (computed per point when walking; the segment's overall start-to-end
    bearing otherwise).

    `progress(fraction, message)` is called throughout with fraction in
    [0, 1] and a human-readable status - e.g. for a web UI progress bar.
    It's a coarse estimate (evenly dividing 1.0 across candidates, and each
    candidate's slice across its sample points and their coverage-check/
    download/OCR steps), not a measurement of actual API latency.
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
    if side_mode not in SIDE_MODES:
        raise ValueError(f"Invalid side_mode {side_mode!r} - must be one of {SIDE_MODES}")
    table = table_name(state, year, month)
    criteria = criteria if criteria is not None else default_criteria()
    walk_segment_spacing_m = max(1.0, float(walk_segment_spacing_m))

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
        seg_dir_root = out_root / safe_segment_dirname(seg_id)

        coords = line_coords_from_geojson(row.get("geom_geojson")) if (walk_segment or side_mode != "center" or headings_relative) else []
        if walk_segment and coords:
            point_count = points_for_spacing(coords, walk_segment_spacing_m)
            base_points = sample_points_along_line(coords, point_count)  # [(lat, lon, bearing), ...]
            walked = True
        else:
            base_points = [(lat, lon, overall_bearing(coords) if coords else 0.0)]
            walked = False

        # Flatten each base position into its side_mode query points -
        # "center" is just the point itself, "sides" is the two
        # perpendicular offset points only (skipping the center - useful
        # when a here_segment_id spans two genuinely separate trunks and
        # only the offset ones land on the one(s) of interest), "both" is
        # center plus both sides. Directory naming preserves the exact
        # prior layout when a mode is off, so existing caches on disk are
        # still reused: flat seg_dir_root with side_mode="center" and no
        # walk_segment, point<N> with only walk_segment, so only
        # combinations actually using a new mode get new subdirectories.
        # Each point carries the road's local bearing there, so
        # headings_relative can rotate the requested headings to face
        # "forward along this point's direction of travel" rather than a
        # fixed compass direction.
        include_center = side_mode in ("center", "both")
        include_sides = side_mode in ("sides", "both")
        sample_points = []  # (lat, lon, point_prefix_or_None, side_label_or_None, base_idx, bearing)
        for p_idx, (p_lat, p_lon, p_bearing) in enumerate(base_points):
            point_prefix = f"point{p_idx}" if walked else None
            if include_center:
                sample_points.append((p_lat, p_lon, point_prefix, "center" if include_sides else None, p_idx, p_bearing))
            if include_sides:
                l_lat, l_lon = _destination_point(p_lat, p_lon, p_bearing - 90, side_offset_m)
                r_lat, r_lon = _destination_point(p_lat, p_lon, p_bearing + 90, side_offset_m)
                sample_points.append((l_lat, l_lon, point_prefix, "left", p_idx, p_bearing))
                sample_points.append((r_lat, r_lon, point_prefix, "right", p_idx, p_bearing))

        mode_desc = []
        if walked:
            mode_desc.append(f"walking {len(base_points)} position(s)")
        if side_mode == "both":
            mode_desc.append(f"±{side_offset_m:.0f}m both sides")
        elif side_mode == "sides":
            mode_desc.append(f"±{side_offset_m:.0f}m sides only (no center)")
        where = f"({lat:.6f}, {lon:.6f})" if not mode_desc else ", ".join(mode_desc)
        log(f"{tag} here_segment_id={seg_id} - {where}")

        image_details: list[ImageDetail] = []
        reading = None
        num_pts = len(sample_points)
        point_span = span / num_pts

        for pt_i, (p_lat, p_lon, point_prefix, side_label, base_idx, p_bearing) in enumerate(sample_points):
            point_base = base + pt_i * point_span
            label_bits = [b for b in (f"position {base_idx + 1}/{len(base_points)}" if walked else None, side_label) if b]
            ptag = f"{tag} {' '.join(label_bits)}" if label_bits else tag
            progress(point_base, f"{ptag} Checking Street View coverage...")

            coverage = streetview_coverage(p_lat, p_lon, api_key)  # StreetViewAuthError propagates to caller
            if not coverage:
                log(f"  {ptag}: no Street View coverage here.")
                progress(point_base + point_span, f"{ptag} No Street View coverage, trying next position...")
                continue

            point_dir = seg_dir_root
            if point_prefix:
                point_dir = point_dir / point_prefix
            if side_label:
                point_dir = point_dir / side_label
            actual_headings = tuple(int(round(h + p_bearing)) % 360 for h in headings) if headings_relative else headings
            progress(point_base + 0.3 * point_span, f"{ptag} Downloading Street View imagery...")
            image_paths = fetch_streetview_images(p_lat, p_lon, api_key, point_dir, headings=actual_headings, log=log)

            for k, image_path in enumerate(image_paths):
                progress(
                    point_base + (0.4 + 0.5 * (k + 1) / len(image_paths)) * point_span,
                    f"{ptag} Running OCR on image {k + 1}/{len(image_paths)}...",
                )
                reading, ocr_text = find_sign_in_image(vision_client, image_path, log=log)
                snippet = " / ".join(ocr_text.split("\n")[:6])[:200] or "(no text detected)"
                log(f"    {image_path.name}: OCR saw: {snippet}")
                image_details.append(
                    ImageDetail(path=image_path, lat=p_lat, lon=p_lon, heading=heading_from_filename(image_path), ocr_snippet=snippet)
                )
                if reading:
                    break

            if reading:
                break
            progress(point_base + point_span, f"{ptag} No readable sign, trying next position...")

        if not image_details:
            result.attempts.append(CandidateAttempt(i + 1, seg_id, lat, lon, "no_coverage"))
            log("  No Street View coverage at any checked position, trying next candidate.")
            progress(base + span, f"{tag} No Street View coverage, trying next candidate...")
            continue

        if not reading:
            result.attempts.append(CandidateAttempt(i + 1, seg_id, lat, lon, "no_sign_read", image_details=image_details))
            log("  Street View imagery found, but no speed limit sign could be read in it. Trying next candidate.")
            progress(base + span, f"{tag} No readable sign, trying next candidate...")
            continue

        matched_index = len(image_details) - 1
        annotated_path = reading.image_path.parent / "sign_detected.jpg"
        save_annotated_image(reading, annotated_path)

        result.attempts.append(
            CandidateAttempt(
                i + 1, seg_id, lat, lon, "match", reading.evidence,
                image_details=image_details, annotated_image=annotated_path, matched_image_index=matched_index,
            )
        )
        match_row = _jsonable_row(row)
        match_row["centroid_lat"] = lat
        match_row["centroid_lon"] = lon
        result.matches.append(
            Match(row=match_row, reading=reading, annotated_image=annotated_path, image_details=image_details, matched_image_index=matched_index)
        )
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
            walk_segment=args.walk_segment,
            walk_segment_spacing_m=args.walk_segment_spacing_m,
            side_mode=args.side_mode,
            side_offset_m=args.side_offset_m,
            headings=args.headings,
            headings_relative=args.headings_relative,
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
        matched_detail = m.image_details[m.matched_image_index]
        print("\n=== Match found ===")
        print(f"Segment: here_segment_id={m.row['here_segment_id']}  street_name={m.row['street_name']}")
        print(f"Segment centroid: {m.row['centroid_lat']:.6f}, {m.row['centroid_lon']:.6f}")
        print(f"Sign location: {matched_detail.lat:.6f}, {matched_detail.lon:.6f}  (heading {matched_detail.heading}°)")
        print(f"Sign reading: {m.reading.speed_mph} mph  ({m.reading.evidence})")
        print(f"Annotated image: {m.annotated_image}")
        print(f"Raw Street View image: {m.reading.image_path}")
        print("\nFull row data:")
        print_row(m.row)
        print(f"\nRow as JSON: {json.dumps(m.row, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
