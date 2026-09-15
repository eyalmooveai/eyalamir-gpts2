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

Required environment variables:
    GOOGLE_MAPS_API_KEY
        Google Maps Platform API key with the Street View Static API
        enabled.
    GOOGLE_APPLICATION_CREDENTIALS (or `gcloud auth application-default login`)
        Credentials with access to BigQuery (read) and the Cloud Vision API
        in the target project.

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
    return p.parse_args()


def table_name(state: str, year: str, month: str) -> str:
    month_padded = f"{int(month):02d}" if month.isdigit() else month
    return f"speed_limits_{state.upper()}_{year}_{month_padded}_details"


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


def streetview_coverage(lat: float, lon: float, api_key: str) -> Optional[dict]:
    resp = requests.get(STREETVIEW_METADATA_URL, params={"location": f"{lat},{lon}", "key": api_key}, timeout=30)
    resp.raise_for_status()
    meta = resp.json()
    return meta if meta.get("status") == "OK" else None


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


def find_sign_in_image(vision_client: vision.ImageAnnotatorClient, image_path: Path) -> Optional[SignReading]:
    """Look for a 'SPEED LIMIT NN' sign in the image via OCR.

    Prefers a number token that sits directly below SPEED/LIMIT word tokens
    (the standard US sign layout) over a bare regex match on the whole page,
    since street scenes are full of other numbers (addresses, other signs).
    """
    content = image_path.read_bytes()
    with Image.open(image_path) as im:
        img_h = im.height
    response = vision_client.text_detection(image=vision.Image(content=content))
    if response.error.message:
        print(f"  Vision API error on {image_path.name}: {response.error.message}", file=sys.stderr)
        return None
    annotations = response.text_annotations
    if not annotations:
        return None

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

    if best:
        return best

    full_text = annotations[0].description
    m = INLINE_SIGN_RE.search(full_text)
    if m:
        speed = int(m.group(1))
        if 15 <= speed <= 80:
            return SignReading(speed, image_path, (0, 0, 0, 0), f"OCR text contained 'SPEED LIMIT {speed}' (no bounding box available)")

    return None


def save_annotated_image(reading: SignReading, out_path: Path) -> None:
    with Image.open(reading.image_path) as im:
        im = im.convert("RGB")
        if reading.box != (0, 0, 0, 0):
            draw = ImageDraw.Draw(im)
            draw.rectangle(reading.box, outline="red", width=6)
        im.save(out_path)


def print_row(row: bigquery.table.Row) -> None:
    for key, value in row.items():
        print(f"  {key}: {value}")


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("ERROR: set GOOGLE_MAPS_API_KEY in the environment.", file=sys.stderr)
        return 1

    table = table_name(args.state, args.year, args.month)
    print(f"Querying `{args.project}.{args.dataset}.{table}` ...")
    candidates = fetch_candidates(args.project, args.dataset, table, args.candidates)
    if not candidates:
        print("No road segments matched the mismatch criteria.")
        return 0
    print(f"Found {len(candidates)} candidate segment(s); trying them in order of largest mismatch.\n")

    vision_client = vision.ImageAnnotatorClient()
    out_root = Path(args.out_dir) / f"{args.state.upper()}_{args.year}_{args.month}"

    for i, row in enumerate(candidates):
        lat, lon = row["centroid_lat"], row["centroid_lon"]
        print(f"[{i + 1}/{len(candidates)}] here_segment_id={row['here_segment_id']} at ({lat:.6f}, {lon:.6f})")

        coverage = streetview_coverage(lat, lon, api_key)
        if not coverage:
            print("  No Street View coverage here, trying next candidate.\n")
            continue

        seg_dir = out_root / str(row["here_segment_id"])
        image_paths = fetch_streetview_images(lat, lon, api_key, seg_dir)

        reading = None
        for image_path in image_paths:
            reading = find_sign_in_image(vision_client, image_path)
            if reading:
                break

        if not reading:
            print("  Street View imagery found, but no speed limit sign could be read in it. Trying next candidate.\n")
            continue

        annotated_path = seg_dir / "sign_detected.jpg"
        save_annotated_image(reading, annotated_path)

        print("\n=== Match found ===")
        print(f"Segment: here_segment_id={row['here_segment_id']}  street_name={row['street_name']}")
        print(f"Location: {lat:.6f}, {lon:.6f}")
        print(f"Sign reading: {reading.speed_mph} mph  ({reading.evidence})")
        print(f"Annotated image: {annotated_path}")
        print(f"Raw Street View image: {reading.image_path}")
        print("\nFull row data:")
        print_row(row)
        print(f"\nRow as JSON: {json.dumps({k: (v if not hasattr(v, 'isoformat') else v.isoformat()) for k, v in row.items() if k != 'geom'}, default=str)}")
        return 0

    print("Exhausted all candidates without finding a readable speed limit sign.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
