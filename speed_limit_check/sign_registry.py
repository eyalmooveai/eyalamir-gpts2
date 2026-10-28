"""Records every detected speed-limit sign into `calc_out.signs` - the one
place all three sign-checking surfaces (Sign Checker, Evaluator,
precision_sample.py) end up going through, since all three already share
find_bad_speed_limit._process_one_candidate()'s own match-found code path;
that function calls record_sign_detection() once, right there, so none of
the three callers need their own copy of this wiring.

Column-by-column, what's real vs. best-effort vs. a known limitation:
  - sign_id: a fresh uuid4 per detection - there's no stable identity for
    "the same physical sign seen again" anywhere in this codebase (confirmed
    absent), so re-detecting the same real-world sign on a later run gets a
    new row, not an update to an old one. A future dedup pass would need to
    match on (here_segment_id, direction) or a lat/lon radius, not sign_id.
  - here_segment_id, image_gcs_path, lat/lon, detected_at, image_date,
    sign_reading, sign_units, sign_text: directly available from the
    existing Match/ImageDetail/SignReading objects - no new inference.
  - **lat/lon is the Street View CAMERA's position** (the walked/sampled
    point the image was taken from), not a triangulated position of the
    sign itself - there is no code anywhere in this app that estimates a
    sign's true offset from the road it's mounted beside. Typically off by
    several meters from the sign's real position. Storing it anyway since
    it's the only position data that exists and is usually close enough to
    locate the sign by eye on a map - just don't treat it as surveyed.
  - sign_type: hardcoded "speed_limit" - confirmed this pipeline's OCR/regex
    (find_bad_speed_limit.INLINE_SIGN_RE et al) only ever recognizes a plain
    "SPEED LIMIT NN" board; there is no other sign type it could possibly
    have matched to produce a Match at all. If a second detector (advisory
    speed, school zone, ...) is ever added, that's also where a real
    sign_type value would come from - not a guess made here after the fact.
  - sign_units: hardcoded "MPH" - this app's whole speed-limit pipeline is
    US-state-DOT data; nothing here ever reads a km/h board.
  - direction_of_travel_deg: the REAL compass bearing (0-359) the Street
    View camera was pointed at when it captured the matched image
    (ImageDetail.heading - already a fully-resolved absolute bearing, not a
    relative placeholder). Not a guess: since the camera could only read the
    sign face-on, the camera was looking the same way a driver regulated by
    that sign would be facing - a legitimate geometric derivation from data
    this app already computes, not a Gemini visual guess (a vision model
    can't reliably infer compass heading from a single photo with no
    compass/shadow/map context, so this deliberately does NOT ask Gemini
    for it).
  - color, shape, dimensions_m, height_above_ground_m: NONE of these are
    computed anywhere else in this codebase - built here via one extra
    Gemini Vision call (see classify_sign_visual_attributes()) over the
    annotated/cropped image of the sign that was ALREADY found, per
    explicit instruction: this never runs over every candidate image, only
    the one image behind a confirmed match, so it's one extra call per
    DETECTION, not per image fetched. dimensions_m and height_above_ground_m
    are honest best-effort visual estimates (typical sign-size priors +
    perspective in a single 2D photo, no known camera-to-sign distance or
    lens calibration) - NOT a measurement. Any of the four can come back
    None if Gemini can't tell (occluded, too small, ambiguous) or the call
    fails outright (no credentials, rate limit) - record_sign_detection()
    never lets a classification failure block recording the sign itself.

BigQuery write follows precision_sample.py's own established precedent for
a brand-new output table in this app (schema-autodetect-on-first-load, not
a hand-written DDL - see save_results_to_bigquery/_append_rows_to_bigquery
there): `client.load_table_from_json(rows, table_ref, job_config=
bigquery.LoadJobConfig(autodetect=True, write_disposition=WRITE_APPEND,
create_disposition=CREATE_IF_NEEDED, schema_update_options=
[ALLOW_FIELD_ADDITION]))`. No DDL is written or run anywhere in this
module - per this repo's standing rule, a table is never created by
running code against BigQuery; CREATE_IF_NEEDED here only ever fires the
first time this code actually runs (which the user runs themselves), not
something this session executes.

Takes an already-constructed `client` rather than building its own - same
reason as precision_sample.py's own bigquery.Client sharing (see that
module's docstring): _process_one_candidate() runs concurrently across
many worker threads in both batch_evaluator.py and precision_sample.py,
and a fresh bigquery.Client() per call was confirmed, this session, to
multiply a real macOS fork-safety crash risk.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from google.cloud import bigquery
from pydantic import BaseModel, Field

import bq_writer
import gemini_client

if TYPE_CHECKING:
    from find_bad_speed_limit import Match

SIGNS_DATASET = "calc_out"
SIGNS_TABLE = "signs"

_SIGN_VISUAL_SYSTEM_PROMPT = """You are looking at a single cropped photo of a US speed-limit road sign that has already been confirmed to read a specific speed limit. Describe ONLY what you can actually see in THIS photo - never guess or default to a "typical" value you haven't actually observed.

Report:
- color: the sign's background color as a plain word (e.g. "white", "yellow", "black"). None if you can't tell.
- shape: the sign's outline shape as a plain word (e.g. "rectangle", "square", "diamond", "round"). None if you can't tell.
- dimensions_m: your best rough estimate of the sign's real-world width x height in meters (e.g. "0.75 x 0.9"), based on typical US road-sign sizing and the sign's apparent proportions/perspective in this photo. This is necessarily a rough visual estimate, not a measurement - say so isn't needed, just give your best estimate or None if the photo gives you nothing to go on (e.g. too distant, too cropped).
- height_above_ground_m: your best rough estimate, in meters, of how high the BOTTOM of the sign sits above the ground/road surface, from visual context (pole height, relative to a passing vehicle, etc). None if you can't tell.

Never fabricate precision you don't have - None is a correct, expected answer for a field you genuinely can't estimate from this photo."""


class _SignVisualAttributes(BaseModel):
    color: Optional[str] = Field(None, description="Background color of the sign, e.g. 'white', 'yellow'.")
    shape: Optional[str] = Field(None, description="Outline shape of the sign, e.g. 'rectangle', 'diamond', 'round'.")
    dimensions_m: Optional[str] = Field(None, description="Rough estimated width x height in meters, e.g. '0.75 x 0.9'.")
    height_above_ground_m: Optional[float] = Field(None, description="Rough estimated height of the sign's bottom edge above ground, in meters.")


def classify_sign_visual_attributes(image_path: Path, project: str) -> dict:
    """Best-effort Gemini Vision classification of a single already-detected
    sign's color/shape/dimensions/height from its cropped/annotated image -
    see module docstring for why these four (and only these four) go
    through an LLM rather than being computed directly, and why
    direction_of_travel deliberately does NOT. Never raises - any failure
    (no Vertex AI credentials, rate limit, bad output) just means these
    four columns stay None for this detection; a classification problem
    must never block recording the sign itself."""
    try:
        parsed = gemini_client.call_structured_with_image(
            system_instruction=_SIGN_VISUAL_SYSTEM_PROMPT,
            user_text="Classify this speed-limit sign's visual attributes from the photo.",
            image_path=image_path,
            response_schema=_SignVisualAttributes,
            project=project,
        )
    except Exception:
        return {"color": None, "shape": None, "dimensions_m": None, "height_above_ground_m": None}
    return {
        "color": parsed.color, "shape": parsed.shape,
        "dimensions_m": parsed.dimensions_m, "height_above_ground_m": parsed.height_above_ground_m,
    }


def build_sign_row(match: "Match", *, image_gcs_path: str, project: str, classify_visual: bool = True) -> dict:
    """Assembles one calc_out.signs row for a confirmed Match - see module
    docstring for the provenance/caveats of every field. `image_gcs_path`
    is passed in rather than computed here, since the caller already knows
    the deterministic GCS key the matching annotated image was (or is
    being, asynchronously) pushed to - see find_bad_speed_limit._gcs_object_key."""
    sign = match.image_details[match.matched_image_index]
    visual = (
        classify_sign_visual_attributes(match.annotated_image, project) if classify_visual
        else {"color": None, "shape": None, "dimensions_m": None, "height_above_ground_m": None}
    )
    return {
        "sign_id": str(uuid.uuid4()),
        "here_segment_id": match.row.get("here_segment_id"),
        "state": match.row.get("state"),
        "street_name": match.row.get("street_name"),
        "functional_class": match.row.get("functional_class"),
        "image_gcs_path": image_gcs_path,
        "lat": sign.lat,
        "lon": sign.lon,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "image_date": sign.capture_date,
        "sign_type": "speed_limit",
        "direction_of_travel_deg": sign.heading,
        "dimensions_m": visual["dimensions_m"],
        "height_above_ground_m": visual["height_above_ground_m"],
        "color": visual["color"],
        "shape": visual["shape"],
        "sign_reading": match.reading.speed_mph,
        "sign_units": "MPH",
        "sign_text": match.reading.evidence,
    }


def record_sign_detection(match: "Match", *, image_gcs_path: str, project: str, client: "bigquery.Client" = None, log=lambda msg: None) -> None:
    """Queues one row for `{project}.calc_out.signs` for a confirmed Match
    and returns immediately - never waits for Gemini or BigQuery. The row's
    facts are spooled to disk right away (see bq_writer.py); the slow parts
    (the Gemini visual classification, then the BigQuery load) happen on a
    background thread, and anything that doesn't finish - a failed load, a
    crash, an instance restart - is retried by bq_writer.replay_pending()
    on the next start. `client` is accepted for call-site compatibility
    only; bq_writer keeps its own shared per-project client. Never raises."""
    try:
        row = build_sign_row(match, image_gcs_path=image_gcs_path, project=project, classify_visual=False)
        bq_writer.submit(
            project, f"{project}.{SIGNS_DATASET}.{SIGNS_TABLE}", [row],
            classify={"image": str(match.annotated_image), "project": project}, log=log,
        )
        log(f"    Queued sign {row['sign_id']} ({row['sign_reading']} mph) for {SIGNS_DATASET}.{SIGNS_TABLE} (background)")
    except Exception as e:
        log(f"    WARNING: failed to queue sign detection for calc_out.signs ({type(e).__name__}: {e}) - continuing without it")
