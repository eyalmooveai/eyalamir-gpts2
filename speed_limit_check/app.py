#!/usr/bin/env python3
"""Web UI for Archimedes, MooveAI's platform for data science on
movement data - currently the speed limit sign checker plus the
nationwide Speed-Limits Quality dashboard, both in this one app/deployment.

Local:
    python app.py

Then open http://127.0.0.1:5050 in a browser. Runs on localhost only.

Can also run as a Cloud Run service instead (via gunicorn, see
Dockerfile) - see README.md's "Deploying to Cloud Run" section for setup,
and the API keys/credentials this needs either way.
"""
from __future__ import annotations

import dataclasses
import os
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, url_for

from batch_evaluator import (
    DAILY_COST_CAP_USD,
    MAX_SEGMENT_COUNT,
    STREETVIEW_RATE_PER_1000,
    VISION_RATE_PER_1000,
    BatchConfig,
    breakdown_by_functional_class,
    csv_path_if_exists,
    daily_spend_for_user,
    list_batches,
    read_status,
    run_batch,
    run_history_stats,
)
from find_bad_speed_limit import (
    CRITERIA_DEFS,
    DEFAULT_DATASET,
    DEFAULT_FOV,
    DEFAULT_HEADINGS,
    DEFAULT_KEYS_FILE,
    DEFAULT_PROJECT,
    GCS_CACHE_BUCKET,
    MAX_FOV,
    MIN_FOV,
    SIDE_MODES,
    STATE_RE,
    NoUsableApiKey,
    StreetViewAuthError,
    _validate_identifier,
    fetch_candidates,
    gcs_cache_pull,
    load_keys_file,
    parse_headings,
    run_pipeline,
    table_name,
)
from quality_metrics import (
    DEFAULT_INFER_FIELD,
    GROUP_BY_CHOICES,
    QUALITY_METRIC_KEYS,
    QUALITY_METRICS,
    US_STATE_CODES,
    QualityFilters,
    default_table_name,
    fetch_custom_metric,
    fetch_custom_sample,
    fetch_quality_metrics,
    fetch_sample_mismatches,
    full_table_name,
    list_evaluable_tables,
    list_infer_fields,
    list_table_columns,
    metric_labels,
    missing_columns_report,
)
from custom_metrics import ExpressionError, resolve_custom_criterion, resolve_custom_criterion_as_expression
from bq_sql_console import run_sql_console, sql_statement_kind
from model_library import (
    MAX_COMPARE_MODELS,
    bytes_to_gb_cost,
    compare_models,
    find_model,
    list_common_columns,
    list_periods,
    list_states,
    list_tags,
)

# The Archimedes hub's model catalog - only "Speed Limits" has a built tool
# today (this app); the rest are placeholders naming what MooveAI expects
# to add here, so the hub is truthful about what exists without promising
# a href that 404s.
HUB_MODELS = [
    {
        "name": "Speed Limits", "href": "/speed-limits", "icon": "speed_limits.svg",
        "description": "How the inferred speed-limit model performs against OSM/HERE/observed speeds nationwide, "
                       "a per-segment Street View sign check, and a batch evaluator across up to 1000 segments at once.",
    },
    {
        "name": "Lanes", "href": None, "icon": "lanes.svg",
        "description": "Results and performance for lane-level inference - not yet available in Archimedes.",
    },
    {
        "name": "Construction Zones", "href": None, "icon": "construction_zones.svg",
        "description": "Results and performance for construction-zone detection - not yet available in Archimedes.",
    },
    {
        "name": "Accident Prediction", "href": None, "icon": "accident_prediction.svg",
        "description": "Results and performance for predicting where accidents are likely - not yet available in Archimedes.",
    },
    {
        "name": "Accident Detection", "href": None, "icon": "accident_detection.svg",
        "description": "Results and performance for detecting accidents from movement data - not yet available in Archimedes.",
    },
]

# Preselected table on the Speed-Limits Quality page, if it's still in
# the live calc_out.speed_limits_US* list (falls back to the first table
# in that list otherwise - see speed_limits_quality()). Bump this when a
# newer month's nationwide _details table becomes the one worth defaulting
# to; the table selector itself always reflects what's actually there.
DEFAULT_QUALITY_YEAR = "2026"
DEFAULT_QUALITY_MONTH = "08"

OUT_DIR = Path("output")
MAX_JOBS = 20  # cap in-memory job history for this long-lived local process

# Web UI form defaults - deliberately different from find_bad_speed_limit's
# own general-purpose CLI defaults (a single heading, narrower zoom, and
# walking every candidate's full length at close spacing is a lot more API
# calls than a sensible CLI baseline, but is what this tool's actual usage
# here has converged on for reliably catching a sign). Only take effect on
# a browser with nothing remembered yet in localStorage - see the "remember
# my last values" JS in index.html, which overrides these once anything's
# been saved.
WEB_DEFAULT_HEADINGS = "10"
WEB_DEFAULT_HEADINGS_RELATIVE = True
WEB_DEFAULT_FOV = 50
WEB_DEFAULT_AUTO_SIDE_OFFSET = False
WEB_DEFAULT_WALK_ALL = True
WEB_DEFAULT_WALK_SEGMENT = True
WEB_DEFAULT_WALK_SEGMENT_SPACING_M = 2
WEB_DEFAULT_SIDE_MODE = "sides"
WEB_DEFAULT_CANDIDATES = 3

# Speed-Limits Evaluator (batch) defaults - segment count/concurrency per
# the user's own spec; walk/side/headings/fov reuse the same "what
# actually works" defaults as the single-segment sign checker above.
EVALUATOR_DEFAULT_SEGMENT_COUNT = 1000
EVALUATOR_DEFAULT_CONCURRENCY = 10
MAX_EVALUATOR_CONCURRENCY = 30  # a sanity ceiling on the form, not a hard API limit
# Hard ceiling on segments per run (see batch_evaluator.MAX_SEGMENT_COUNT,
# the single source of truth this just re-exports for readability here) -
# independent of, and on top of, DAILY_COST_CAP_USD below.
MAX_EVALUATOR_SEGMENT_COUNT = MAX_SEGMENT_COUNT
EVALUATOR_MAX_JOBS_IN_MEMORY = 20  # cap on live threading.Event registry - old ones are done, don't need one
# Cap on the "preview these candidates on a map before running" endpoints
# (evaluator_preview_candidates/sign_checker_preview_candidates) -
# independent of segment_count/candidates, which can ask for up to 1000/50
# respectively: a preview is for eyeballing what a run would cover, not
# for actually processing, so it stays cheap and the map stays responsive
# regardless of how large a real run is configured for.
PREVIEW_MAX_SEGMENTS = 300

# Load once at startup (not just inside run_pipeline's background thread) so
# GOOGLE_MAPS_API_KEY is available for embedding in a page - e.g. the
# Google Maps JavaScript API script tag - even before any job has run.
load_keys_file(DEFAULT_KEYS_FILE)

app = Flask(__name__)

# In-memory job store. A single local user, one run at a time in practice,
# so a plain dict + lock is enough - no need for a task queue/DB here.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# Separate in-memory job store for the Speed-Limits Quality page's own
# BigQuery queries - same pattern as JOBS/JOBS_LOCK above, but kept apart
# since the job shape is completely different (query filters/results, not
# a segment-checking pipeline) and the two are otherwise unrelated. Runs
# every quality query - a full-table aggregate scanning tens of millions
# of rows - in a background thread instead of blocking the page load, so
# the shell (filters form, table dropdown) renders immediately and the
# stats fill in once the query (or its cache hit) actually completes.
QUALITY_JOBS: dict[str, dict] = {}
QUALITY_JOBS_LOCK = threading.Lock()
MAX_QUALITY_JOBS = 20

# Batch evaluator runs are durable (status/results live in
# output/_batch_jobs/, see batch_evaluator.py) - this registry is only
# for cancelling a batch that's actually running in *this* process, via
# its threading.Event. It's fine for this to be lost on a restart (the
# batch's own background thread would already be gone too in that case);
# it's not the source of truth for status the way JOBS/QUALITY_JOBS are.
BATCH_CANCEL_EVENTS: dict[str, threading.Event] = {}
BATCH_CANCEL_EVENTS_LOCK = threading.Lock()

# Model Registry Compare's own job store, same pattern as JOBS/QUALITY_JOBS
# above - a compare can take a while (up to 3 sequential
# calc.model_registry_compare_models CALLs, each a real nationwide join),
# so /models/compare starts it in a background thread and returns a job_id
# immediately; the sidebar polls /models/compare/status/<job_id> for live
# progress (which pair, bytes processed so far) instead of the request
# blocking until every pair finishes - real feedback ("nothing changed on
# the main page... no progress meter") on the previous blocking version.
COMPARE_JOBS: dict[str, dict] = {}
COMPARE_JOBS_LOCK = threading.Lock()
MAX_COMPARE_JOBS = 20


def _image_url(path: Path) -> str:
    return "/images/" + str(Path(path).relative_to(OUT_DIR)).replace(os.sep, "/")


def _now_label_suffix() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _requester_email() -> str:
    """The signed-in user's email, from the header IAP sets on every
    forwarded request (see CLAUDE.md's IAP notes) - falls back to
    "unknown" for local dev, where there's no IAP in front of the app."""
    raw = request.headers.get("X-Goog-Authenticated-User-Email", "")
    return raw.split(":", 1)[-1] if raw else "unknown"


# A small fixed, deliberately-chosen palette (not raw random colors, which
# can land on something illegible against white text) - which one a given
# user gets is deterministic (hashed from their email), not random per
# page load, so the same person's avatar looks the same everywhere.
_AVATAR_COLORS = ["#2f6fed", "#00b5ad", "#b25a00", "#7c3aed", "#c62828", "#1d8a4a", "#0e7490", "#a3168a"]


def _user_avatar_color(email: str) -> str:
    import hashlib
    digest = hashlib.sha256(email.encode("utf-8")).digest()
    return _AVATAR_COLORS[digest[0] % len(_AVATAR_COLORS)]


def _user_initials(email: str) -> str:
    """"jane.doe@moove.ai" -> "JD"; a single-token local part -> its first
    two letters; the "unknown" placeholder (no IAP, local dev) -> "?"."""
    if not email or email == "unknown":
        return "?"
    local_part = email.split("@", 1)[0]
    tokens = [t for t in re.split(r"[.\-_+]", local_part) if t]
    if not tokens:
        return local_part[:2].upper() or "?"
    if len(tokens) == 1:
        return tokens[0][:2].upper()
    return (tokens[0][0] + tokens[1][0]).upper()


@app.context_processor
def _inject_nav_context():
    """Available in every template (layout.html's header/footer chrome
    is shared by every page) - who's signed in (per _requester_email(),
    IAP-backed in production) and a deterministic avatar (initials +
    color) for them. There's no real profile photo available here - IAP
    only forwards an email, never a picture - so a generated avatar is
    the honest choice, not an approximation of a photo we don't have."""
    from datetime import datetime, timezone
    email = _requester_email()
    return {
        "nav_user_email": email,
        "nav_user_signed_in": email != "unknown",
        "nav_user_initials": _user_initials(email),
        "nav_user_color": _user_avatar_color(email),
        "current_year": datetime.now(timezone.utc).year,
    }


def _google_maps_js_key() -> str:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    return "" if key == "your-key-here" else key


def _read_criteria_from_form(form) -> dict[str, tuple[bool, float]]:
    criteria = {}
    for c in CRITERIA_DEFS:
        enabled = form.get(f"crit_{c['key']}_enabled") is not None
        raw_value = form.get(f"crit_{c['key']}_value", "").strip()
        try:
            value = float(raw_value) if raw_value else c["default_value"]
        except ValueError:
            value = c["default_value"]
        criteria[c["key"]] = (enabled, value)
    return criteria


def _parse_csv_field(raw: str, *, upper: bool = False) -> tuple[str, ...]:
    """A comma-separated form field ("27601, 27603" or "Wake, Durham") into
    a tuple of trimmed, non-empty values - the same shape zip_codes/
    counties (and states/functional_classes) are threaded through as.
    Actual validity (5-digit zip, real county name) is checked downstream
    by find_bad_speed_limit.build_geo_filter_sql, not here."""
    return tuple((s.strip().upper() if upper else s.strip()) for s in raw.split(",") if s.strip())


def _quality_filters_from_args(args, *, require_infer_field: bool = False) -> tuple[QualityFilters | None, tuple | None]:
    """Common table/infer_field/param1/param2/states/fc/zip_codes/counties
    parsing shared by the Quality page's on-demand JSON endpoints
    (sample-mismatches, custom-test, custom-test/sample) - returns
    (QualityFilters, None) on success or (None, (response, status)) on a
    parse error, so callers can `filters, err = ...(); if err: return err`."""
    table_option = args.get("table", "").strip()
    dataset, _, table = table_option.partition(".")
    infer_field = args.get("infer_field", "").strip()
    if not (dataset and table) or (require_infer_field and not infer_field):
        msg = "Table and inferred field are required." if require_infer_field else "Table is required."
        return None, (jsonify({"error": msg}), 400)
    try:
        param1 = float(args.get("param1", "10") or 10)
        param2 = float(args.get("param2", "80") or 80)
    except ValueError:
        return None, (jsonify({"error": "Invalid mismatch/implausible-speed threshold."}), 400)
    states = _parse_csv_field(args.get("states", ""), upper=True)
    fcs_raw = _parse_csv_field(args.get("fc", ""))
    try:
        fcs = tuple(int(v) for v in fcs_raw)
    except ValueError:
        return None, (jsonify({"error": "Functional class(es) must be whole numbers."}), 400)
    zip_codes = _parse_csv_field(args.get("zip_codes", ""))
    counties = _parse_csv_field(args.get("counties", ""))
    filters = QualityFilters(
        project=DEFAULT_PROJECT, dataset=dataset, table=table, infer_field=infer_field or DEFAULT_INFER_FIELD,
        param1=param1, param2=param2, states=states, functional_classes=fcs,
        zip_codes=zip_codes, counties=counties,
    )
    return filters, None


def _preview_candidates_response(
    project: str, dataset: str, state: str, year: str, month: str,
    criteria: dict, zip_codes: tuple[str, ...], counties: tuple[str, ...], requested_count: int,
    custom_criterion_text: str = "",
):
    """Shared body of the "preview these candidates on a map before
    running" endpoints (sign checker + Evaluator launch pages) - the same
    fetch_candidates() call a real run would make, just capped at
    PREVIEW_MAX_SEGMENTS and returning JSON instead of kicking off a job.
    Walk/side/heading/fov settings don't affect *which* segments get
    selected (only how each one is later checked), so this only needs
    state/year/month/project/dataset/criteria/zip_codes/counties/count -
    a real subset of what evaluator_start()/run() read from their forms.
    custom_criterion_text (the Custom test box's raw text, carried over
    from the Quality page or typed directly here) is re-validated fresh
    against this specific table's live columns on every call - see
    custom_metrics.py's module docstring for why that can't be skipped."""
    if not (state and year and month):
        return jsonify({"error": "State, year, and month are all required."}), 400
    preview_limit = max(1, min(requested_count, PREVIEW_MAX_SEGMENTS))
    try:
        _validate_identifier(state, STATE_RE, "state (expected 2 letters, e.g. NC)")
        table = table_name(state, year, month)
        custom_criterion_sql = None
        if custom_criterion_text.strip():
            available_columns = list_table_columns(project, dataset, table)
            validated, _source, _explanation = resolve_custom_criterion_as_expression(
                custom_criterion_text, available_columns, DEFAULT_PROJECT,
            )
            custom_criterion_sql = validated.sql
        candidates = fetch_candidates(
            project, dataset, table, criteria, preview_limit, state=state, zip_codes=zip_codes, counties=counties,
            custom_criterion_sql=custom_criterion_sql,
        )
    except ExpressionError as e:
        return jsonify({"error": f"Custom test: {e}"}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    points = [
        {
            "segment_id": c.get("here_segment_id"), "lat": c.get("centroid_lat"), "lon": c.get("centroid_lon"),
            "street_name": c.get("street_name"), "functional_class": c.get("functional_class"),
        }
        for c in candidates
    ]
    return jsonify({
        "points": points,
        "shown": len(points),
        "requested": requested_count,
        "truncated": requested_count > preview_limit or len(points) >= preview_limit,
    })


def _new_job(state: str, year: str, month: str, segment_id: str | None, walk_all: bool, walk_segment: bool) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",  # "running" | "done" | "error"
            "progress": 0.0,
            "message": "Starting...",
            "log": [],
            "result": None,
            "error": None,
            "state": state,
            "year": year,
            "month": month,
            "segment_id": segment_id,
            "walk_all": walk_all,
            "walk_segment": walk_segment,
        }
        while len(JOBS) > MAX_JOBS:
            del JOBS[next(iter(JOBS))]
    return job_id


def _update_job(job_id: str, **kwargs) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def _get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _run_job(
    job_id: str,
    state: str,
    year: str,
    month: str,
    project: str,
    dataset: str,
    max_candidates: int,
    criteria: dict[str, tuple[bool, float]],
    zip_codes: tuple[str, ...],
    counties: tuple[str, ...],
    custom_criterion_sql: str | None,
    segment_id: str | None,
    walk_all: bool,
    walk_segment: bool,
    walk_segment_spacing_m: float,
    side_mode: str,
    side_offset_m: float,
    auto_side_offset: bool,
    headings: tuple[int, ...],
    headings_relative: bool,
    fov: int,
) -> None:
    def log(msg: str) -> None:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["log"].append(msg)

    def progress(fraction: float, message: str) -> None:
        _update_job(job_id, progress=max(0.0, min(1.0, fraction)), message=message)

    try:
        result = run_pipeline(
            state,
            year,
            month,
            project=project,
            dataset=dataset,
            max_candidates=max_candidates,
            criteria=criteria,
            zip_codes=zip_codes,
            counties=counties,
            custom_criterion_sql=custom_criterion_sql,
            segment_id=segment_id,
            walk_all=walk_all,
            walk_segment=walk_segment,
            walk_segment_spacing_m=walk_segment_spacing_m,
            side_mode=side_mode,
            side_offset_m=side_offset_m,
            auto_side_offset=auto_side_offset,
            headings=headings,
            headings_relative=headings_relative,
            fov=fov,
            out_dir=OUT_DIR,
            keys_file=DEFAULT_KEYS_FILE,
            log=log,
            progress=progress,
        )
        _update_job(job_id, status="done", progress=1.0, result=result)
    except NoUsableApiKey as e:
        _update_job(job_id, status="error", error=str(e))
    except StreetViewAuthError as e:
        _update_job(
            job_id,
            status="error",
            error=(
                f"{e}\n\nThis means the API key, billing, or API enablement is broken - not that "
                "there's no imagery. Check that GOOGLE_MAPS_API_KEY is a real key with the Street "
                "View Static API enabled and billing active on its project."
            ),
        )
    except ValueError as e:  # bad state/year/month/project/dataset/segment_id
        _update_job(job_id, status="error", error=str(e))
    except Exception as e:  # BigQuery/Vision auth errors etc. - surface rather than hanging the UI
        _update_job(job_id, status="error", error=f"{type(e).__name__}: {e}")


def _new_quality_job() -> str:
    job_id = uuid.uuid4().hex
    with QUALITY_JOBS_LOCK:
        QUALITY_JOBS[job_id] = {
            "status": "running",  # "running" | "done" | "error"
            "message": "Running BigQuery query...",
            "error": None,
            "table_name_display": None,
            "filtered": False,
            "metrics": None,
            "breakdown": None,
            "metric_defs": [],
            "group_by": "none",
        }
        while len(QUALITY_JOBS) > MAX_QUALITY_JOBS:
            del QUALITY_JOBS[next(iter(QUALITY_JOBS))]
    return job_id


def _update_quality_job(job_id: str, **kwargs) -> None:
    with QUALITY_JOBS_LOCK:
        if job_id in QUALITY_JOBS:
            QUALITY_JOBS[job_id].update(kwargs)


def _get_quality_job(job_id: str) -> dict | None:
    with QUALITY_JOBS_LOCK:
        job = QUALITY_JOBS.get(job_id)
        return dict(job) if job else None


def _run_quality_job(job_id: str, base: QualityFilters, group_by: str, param1: float, param2: float) -> None:
    try:
        labels = metric_labels(param1, param2)
        metric_defs_view = [{"key": m["key"], "label": labels[m["key"]]} for m in QUALITY_METRICS]
        table_name_display = full_table_name(base)
        filtered = bool(base.states or base.functional_classes)
        _update_quality_job(
            job_id,
            table_name_display=table_name_display,
            filtered=filtered,
            metric_defs=metric_defs_view,
            group_by=group_by,
            message=f"Querying {table_name_display}...",
        )

        metrics_rows = fetch_quality_metrics(base)
        metrics_view = metrics_rows[0] if metrics_rows else None

        breakdown_view = None
        if group_by != "none":
            _update_quality_job(job_id, message="Querying the breakdown...")
            grouped = dataclasses.replace(base, group_by=group_by)
            breakdown_view = fetch_quality_metrics(grouped)

        _update_quality_job(job_id, status="done", metrics=metrics_view, breakdown=breakdown_view)
    except ValueError as e:
        _update_quality_job(job_id, status="error", error=str(e))
    except Exception as e:
        _update_quality_job(job_id, status="error", error=f"{type(e).__name__}: {e}")


def _new_compare_job() -> str:
    job_id = uuid.uuid4().hex
    with COMPARE_JOBS_LOCK:
        COMPARE_JOBS[job_id] = {
            "status": "running",  # "running" | "done" | "error"
            "error": None,
            "column": None,
            "pairs_total": 0,
            "pairs_done": 0,
            "current_pair_label": None,
            "current_pair_bytes_processed": 0,
            "bytes_so_far": 0,  # sum of COMPLETED pairs' bytes_billed only
            "pairs": [],  # completed pairs' to_json(), in order
        }
        while len(COMPARE_JOBS) > MAX_COMPARE_JOBS:
            del COMPARE_JOBS[next(iter(COMPARE_JOBS))]
    return job_id


def _update_compare_job(job_id: str, **kwargs) -> None:
    with COMPARE_JOBS_LOCK:
        if job_id in COMPARE_JOBS:
            COMPARE_JOBS[job_id].update(kwargs)


def _get_compare_job(job_id: str) -> dict | None:
    with COMPARE_JOBS_LOCK:
        job = COMPARE_JOBS.get(job_id)
        return dict(job) if job else None


def _run_compare_job(job_id: str, column: str, entries: list) -> None:
    """Runs compare_models() in a background thread, translating its
    on_event callback into updates on this job's COMPARE_JOBS record -
    see model_library.compare_models's own docstring for what each event
    carries. Never writes the final gb/cost_usd directly: the status
    route always recomputes them from bytes_so_far (+ whatever the
    in-progress pair has processed), so there's one formula, not two
    that could drift apart (see model_library.bytes_to_gb_cost)."""
    _update_compare_job(job_id, column=column)

    def on_event(event: str, **kw) -> None:
        if event == "start":
            _update_compare_job(job_id, pairs_total=kw["pairs_total"])
        elif event == "pair_started":
            _update_compare_job(
                job_id, current_pair_label=f'{kw["tag_a"]} vs {kw["tag_b"]}', current_pair_bytes_processed=0,
            )
        elif event == "bytes_update":
            _update_compare_job(job_id, current_pair_bytes_processed=kw["bytes_processed"])
        elif event == "pair_done":
            with COMPARE_JOBS_LOCK:
                job = COMPARE_JOBS.get(job_id)
                if job is not None:
                    pair = kw["pair"]
                    job["pairs"].append(pair.to_json())
                    job["bytes_so_far"] = job.get("bytes_so_far", 0) + pair.bytes_billed
                    job["pairs_done"] = kw["index"] + 1
                    job["current_pair_bytes_processed"] = 0

    try:
        result = compare_models(DEFAULT_PROJECT, column, entries, log=print, on_event=on_event)
    except Exception as e:
        _update_compare_job(job_id, status="error", error=f"{type(e).__name__}: {e}")
        return
    if result.kind == "error":
        _update_compare_job(job_id, status="error", error=result.bq_error)
        return
    # The authoritative final write, not just a status flip - "pairs" was
    # already built up incrementally via on_event's "pair_done" events
    # (what the live-polling UI sees mid-run), but this overwrites it with
    # result.pairs directly so the done state is always correct even if
    # an on_event call were ever missed, rather than depending on two
    # mechanisms agreeing.
    _update_compare_job(job_id, status="done", pairs=[p.to_json() for p in result.pairs])


@app.route("/", methods=["GET"])
def hub():
    """The hub page: MooveAI's model catalog. Kept to just that - the
    "Explore" nav item and its live sample map live on their own page
    (see explore_page() below), not here; don't re-merge them, that was
    tried and explicitly reverted ("the main page should not have
    explore, it should have the models")."""
    return render_template("hub.html", models=HUB_MODELS)


@app.route("/explore", methods=["GET"])
def explore_page():
    """The Explore page: a live sample map, not a canned screenshot - and
    the table/metric/states/threshold it's exploring are all changeable
    from the page itself (a plain GET form, same "submit -> page reloads
    with these query params baked into the map's own auto-fetch" pattern
    as the Quality page's main filters form), not fixed to "vs. OSM"
    nationwide. Uses the same default-table logic as the Quality page
    (_table_options_and_default) and the same /speed-limits/sample-mismatches
    endpoint the Quality page's own issue map calls - no separate query
    path to keep in sync."""
    table_options, selected_table_option, sample_map_error = _table_options_and_default(
        request.args.get("table", "").strip()
    )
    sample_map_infer_field = DEFAULT_INFER_FIELD
    if not sample_map_error:
        try:
            dataset, _, table = selected_table_option.partition(".")
            infer_field_options = list_infer_fields(DEFAULT_PROJECT, dataset, table)
            sample_map_infer_field = (
                DEFAULT_INFER_FIELD if DEFAULT_INFER_FIELD in infer_field_options
                else (infer_field_options[0] if infer_field_options else DEFAULT_INFER_FIELD)
            )
        except Exception as e:
            sample_map_error = f"{type(e).__name__}: {e}"

    requested_metric = request.args.get("metric", "").strip()
    selected_metric = requested_metric if requested_metric in QUALITY_METRIC_KEYS else "diff_osm"
    try:
        selected_param1 = float(request.args.get("param1", "10") or 10)
    except ValueError:
        selected_param1 = 10.0
    if selected_param1 == int(selected_param1):
        selected_param1 = int(selected_param1)  # "10" in the form, not "10.0"
    selected_states = request.args.get("states", "").strip()

    return render_template(
        "explore.html",
        table_options=table_options,
        sample_map_table=selected_table_option,
        sample_map_infer_field=sample_map_infer_field,
        sample_map_error=sample_map_error,
        quality_metric_choices=[{"key": m["key"], "name": m["name"], "description": m["description"]} for m in QUALITY_METRICS],
        selected_metric=selected_metric,
        selected_param1=selected_param1,
        selected_states=selected_states,
        us_state_codes=US_STATE_CODES,
    )


@app.route("/deploy", methods=["GET"])
def deploy_info():
    """A reference page, not a live dashboard - Archimedes has no wired-up
    Cloud Run/GCP Admin API calls to report real-time revision/traffic
    status, so this deliberately only states documented, static facts
    (see speed_limit_check/CLAUDE.md's deployment notes and README.md's
    "Deploying to Cloud Run" section, which this mirrors) rather than
    implying it's monitoring anything live."""
    return render_template(
        "deploy_info.html",
        service_name="speed-limit-check",
        project_id="moove-platform-testing-data",
        region="us-central1",
        bucket_name="archimedes-control",
        service_url="https://speed-limit-check-233134271134.us-central1.run.app",
        public_domain="https://archimedes.moove.ai",
    )


def _table_options_and_default(requested_table: str = ""):
    """Live, not hardcoded - so the selector always matches whatever tables
    (calc_out's speed_limits_US* family, and archimedes_api's
    speed_limits_infer* views) actually exist, without this app needing
    to know about a new one in advance. Each option's dropdown value is
    "dataset.table" since the two sources live in different datasets.
    This is a fast, cached metadata-only query (see quality_metrics.py) -
    only the actual aggregate metrics query is slow. Shared by every page
    that needs a table selector defaulted sensibly: the Quality page, the
    standalone Agent page, and the Explore page's sample map.
    Returns (table_options, selected_option, error)."""
    try:
        table_options = [f"{t['dataset']}.{t['table']}" for t in list_evaluable_tables(DEFAULT_PROJECT)]
        error = None
    except Exception as e:
        table_options = []
        error = f"Could not list evaluable tables: {type(e).__name__}: {e}"

    default_table = f"{DEFAULT_DATASET}.{default_table_name(DEFAULT_QUALITY_YEAR, DEFAULT_QUALITY_MONTH)}"
    selected_option = requested_table or (default_table if default_table in table_options else (table_options[0] if table_options else default_table))
    return table_options, selected_option, error


@app.route("/agent", methods=["GET"])
def agent_page():
    """A standalone home for the Custom test/"Ask Archimedes" AI agent
    capability (see custom_metrics.py) - the same box also lives on the
    Speed-Limits Quality page (see _ask_archimedes.html, included by
    both). Needs its own minimal table selector since the underlying
    /speed-limits/custom-test(/sample) endpoints require a specific
    table to validate a comparison's column names against, even for a
    pure Q&A question - there's no page-independent "default table" a
    comparison could run against otherwise."""
    table_options, selected_option, error = _table_options_and_default(request.args.get("table", "").strip())
    return render_template(
        "agent.html",
        table_options=table_options,
        selected_table=selected_option,
        error=error,
        us_state_codes=US_STATE_CODES,
    )


@app.route("/speed-limits", methods=["GET"])
def speed_limits_quality():
    table_options, selected_option, error = _table_options_and_default(request.args.get("table", "").strip())
    selected_dataset, _, selected_table = selected_option.partition(".")

    # Which inferred-speed-limit column to evaluate - discovered live per
    # table (see list_infer_fields's docstring for why: it varies table to
    # table, e.g. speed_limit_infer_mph_corrected isn't on every one).
    # Prefers DEFAULT_INFER_FIELD when the selected table actually has it,
    # same "keep the familiar default when it's valid, else fall back to
    # whatever's actually there" pattern as the table selector above.
    infer_field_options: list[str] = []
    if not error:
        try:
            infer_field_options = list_infer_fields(DEFAULT_PROJECT, selected_dataset, selected_table)
        except Exception as e:
            error = f"Could not list inferred-speed-limit fields for {selected_option}: {type(e).__name__}: {e}"

    requested_infer_field = request.args.get("infer_field", "").strip()
    selected_infer_field = requested_infer_field or (
        DEFAULT_INFER_FIELD if DEFAULT_INFER_FIELD in infer_field_options
        else (infer_field_options[0] if infer_field_options else DEFAULT_INFER_FIELD)
    )

    # Whether the selected table actually has every column the six metrics
    # below need (they're one query, so even one missing column blocks all
    # of them) - checked up front so a table like archimedes_api's views,
    # which are missing speed_limit_here_mph/freeflow_mph, shows a plain-
    # English explanation instead of a raw BigQuery "Unrecognized name"
    # error. Same cheap cached metadata lookup as the two above - known
    # synchronously, so no background job is needed when this fires.
    warning = None
    if not error:
        try:
            available_columns = list_table_columns(DEFAULT_PROJECT, selected_dataset, selected_table)
            warning = missing_columns_report(available_columns, selected_infer_field)
        except Exception as e:
            error = f"Could not check {selected_option}'s columns: {type(e).__name__}: {e}"

    filters_echo = {
        "table": selected_option,
        "infer_field": selected_infer_field,
        "param1": request.args.get("param1", "10").strip() or "10",
        "param2": request.args.get("param2", "80").strip() or "80",
        "states": request.args.get("states", "").strip(),
        "functional_classes": request.args.get("fc", "").strip(),
        "zip_codes": request.args.get("zip_codes", "").strip(),
        "counties": request.args.get("counties", "").strip(),
        "group_by": request.args.get("group_by", "none").strip() or "none",
    }

    # The nationwide (or filtered-nationwide) summary is always kicked off
    # and shown, even on a fresh page load with no query params at all -
    # it's the headline number the page exists to answer at a glance. It
    # runs in a background job (see _run_quality_job) rather than blocking
    # this response, since it's a full-table aggregate scanning tens of
    # millions of rows - the page renders immediately with a spinner in
    # its place, and JS (in quality.html) polls/fetches the result once
    # the job (or its cache hit) completes. Only the breakdown table is
    # opt-in (group_by), computed by that same job.
    job_id = None
    if not error and not warning:
        try:
            param1 = float(filters_echo["param1"])
            param2 = float(filters_echo["param2"])
            states = tuple(s.strip().upper() for s in filters_echo["states"].split(",") if s.strip())
            fcs = tuple(int(v.strip()) for v in filters_echo["functional_classes"].split(",") if v.strip())
            zip_codes = _parse_csv_field(filters_echo["zip_codes"])
            counties = _parse_csv_field(filters_echo["counties"])
            group_by = filters_echo["group_by"] if filters_echo["group_by"] in GROUP_BY_CHOICES else "none"
            filters_echo["group_by"] = group_by

            base = QualityFilters(
                project=DEFAULT_PROJECT, dataset=selected_dataset, table=selected_table,
                infer_field=selected_infer_field, param1=param1, param2=param2,
                states=states, functional_classes=fcs, zip_codes=zip_codes, counties=counties,
            )
            job_id = _new_quality_job()
            thread = threading.Thread(target=_run_quality_job, args=(job_id, base, group_by, param1, param2), daemon=True)
            thread.start()
        except ValueError as e:
            error = str(e)

    return render_template(
        "quality.html",
        filters=filters_echo,
        job_id=job_id,
        table_options=table_options,
        infer_field_options=infer_field_options,
        group_by_choices=GROUP_BY_CHOICES,
        us_state_codes=US_STATE_CODES,
        error=error,
        warning=warning,
        quality_metric_choices=[{"key": m["key"], "name": m["name"], "description": m["description"]} for m in QUALITY_METRICS],
        preview_max_segments=PREVIEW_MAX_SEGMENTS,
    )


@app.route("/speed-limits/status/<job_id>")
def quality_status(job_id):
    job = _get_quality_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Unknown or expired job."})
    return jsonify({"status": job["status"], "message": job["message"]})


@app.route("/speed-limits/fragment/<job_id>")
def quality_fragment(job_id):
    job = _get_quality_job(job_id)
    if not job or job["status"] == "running":
        # Shouldn't normally be fetched before /status says done/error -
        # JS only calls this once polling sees a terminal state. Empty
        # body + 202 rather than an error page for the rare race.
        return "", 202
    return render_template(
        "quality_result_fragment.html",
        error=job["error"],
        table_name_display=job["table_name_display"],
        filtered=job["filtered"],
        metrics=job["metrics"],
        breakdown=job["breakdown"],
        quality_metric_defs=job["metric_defs"],
        group_by=job["group_by"],
    )


@app.route("/speed-limits/sample-mismatches")
def quality_sample_mismatches():
    """JSON for the Quality page's issue map: up to PREVIEW_MAX_SEGMENTS
    real segments failing the requested metric's own condition, worst
    offenders first (see quality_metrics.build_sample_mismatches_query).
    On-demand (a button click, not loaded with the rest of the page) -
    same filters as the main aggregate query, read straight from the
    query string rather than re-deriving them through the async-job
    machinery the main metrics use, since this is a single cheap LIMIT'd
    query, not a nationwide aggregate that needs a background job."""
    metric_key = request.args.get("metric", "").strip()
    if metric_key not in QUALITY_METRIC_KEYS:
        return jsonify({"error": f"Invalid metric {metric_key!r} - must be one of {QUALITY_METRIC_KEYS}"}), 400

    base, err = _quality_filters_from_args(request.args, require_infer_field=True)
    if err:
        return err
    try:
        rows = fetch_sample_mismatches(base, metric_key, PREVIEW_MAX_SEGMENTS)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify({
        "points": [
            {
                "segment_id": r.get("here_segment_id"), "lat": r.get("lat"), "lon": r.get("lon"),
                "street_name": r.get("street_name"), "functional_class": r.get("functional_class"),
                "state": r.get("state"), "infer_value": r.get("infer_value"), "magnitude": r.get("magnitude"),
            }
            for r in rows
        ],
        "shown": len(rows),
        "cap": PREVIEW_MAX_SEGMENTS,
        "metric_name": next(m["name"] for m in QUALITY_METRICS if m["key"] == metric_key),
    })


def _sql_console_response(text: str):
    """If `text` looks like a real SQL statement (SELECT/WITH/CALL, not
    the narrow single-comparison grammar custom_metrics.py validates -
    see bq_sql_console.sql_statement_kind()), handles it on that
    module's own track (dry-run cost estimate, $10 reconfirmation gate,
    privileged-only CALL, Gemini fix advice on a malformed query) and
    returns the jsonify()'d response. Returns None if `text` doesn't look
    like SQL at all, so the caller falls through to the normal
    comparison/answer/example path - this check has to run BEFORE that
    path, not after, since a real "SELECT ... FROM ..." would otherwise
    just fail validate_custom_expression() with a confusing "Unknown
    column 'SELECT'" instead of ever reaching here."""
    kind = sql_statement_kind(text)
    if kind is None:
        return None
    confirmed = request.form.get("confirmed", "").strip().lower() in ("1", "true", "yes")
    result = run_sql_console(text, DEFAULT_PROJECT, _requester_email(), confirmed, log=print)
    if result.kind == "error":
        return jsonify({
            "kind": "sql_error", "error": result.bq_error,
            "fix_explanation": result.fix_explanation, "fix_suggested_sql": result.fix_suggested_sql,
        }), 400
    if result.kind == "cost_estimate":
        return jsonify({
            "kind": "sql_cost_estimate", "statement_kind": result.statement_kind,
            "gb": result.gb, "cost_usd": result.cost_usd,
        })
    return jsonify({
        "kind": "sql_results", "statement_kind": result.statement_kind,
        "gb": result.gb, "cost_usd": result.cost_usd,
        "columns": result.columns, "rows": result.rows, "truncated": result.truncated,
    })


@app.route("/models/states", methods=["GET"])
def models_states():
    """Step 1 of the model-library sidebar's cascading picker
    (templates/_model_library_sidebar.html) - every state with at least
    one archived run, via calc.model_registry_list_states(). Each step
    of this picker only ever fetches what the previous step narrowed to,
    never the full registry - see model_library.py's module docstring."""
    try:
        states = list_states(DEFAULT_PROJECT)
    except Exception as e:
        return jsonify({"error": f"Could not load states: {type(e).__name__}: {e}"}), 500
    return jsonify({"states": [s.to_json() for s in states]})


@app.route("/models/periods", methods=["GET"])
def models_periods():
    """Step 2 - periods archived for one state (query param `state`)."""
    state = (request.args.get("state") or "").strip()
    if not state:
        return jsonify({"error": "state is required"}), 400
    try:
        periods = list_periods(DEFAULT_PROJECT, state)
    except Exception as e:
        return jsonify({"error": f"Could not load periods for {state}: {type(e).__name__}: {e}"}), 500
    return jsonify({"periods": [p.to_json() for p in periods]})


def _parse_year_month(args):
    """Shared by /models/tags and /models/columns - returns (year_num,
    month_num, None) on success or (None, None, (response, status)) on a
    bad/missing query param."""
    state = (args.get("state") or "").strip()
    try:
        year_num = int(args.get("year_num", ""))
        month_num = int(args.get("month_num", ""))
    except ValueError:
        return None, None, (jsonify({"error": "year_num and month_num must be integers"}), 400)
    if not state:
        return None, None, (jsonify({"error": "state is required"}), 400)
    return year_num, month_num, None


@app.route("/models/tags", methods=["GET"])
def models_tags():
    """Step 3 - archived tags for one state+period (query params
    `state`, `year_num`, `month_num`)."""
    state = (request.args.get("state") or "").strip()
    year_num, month_num, err = _parse_year_month(request.args)
    if err:
        return err
    try:
        entries = list_tags(DEFAULT_PROJECT, state, year_num, month_num)
    except Exception as e:
        return jsonify({"error": f"Could not load models for {state} {year_num}-{month_num:02d}: {type(e).__name__}: {e}"}), 500
    return jsonify({"models": [m.to_json() for m in entries]})


@app.route("/models/columns", methods=["GET"])
def models_columns():
    """Step 4 - numeric columns common to every one of 2-3 selected tags
    (query params `state`, `year_num`, `month_num`, repeated `tag`).
    Re-fetches list_tags() itself rather than trusting table names from
    the client - same "never trust a client-supplied identifier, always
    re-resolve server-side" rule /models/compare follows below."""
    state = (request.args.get("state") or "").strip()
    year_num, month_num, err = _parse_year_month(request.args)
    if err:
        return err
    tags = request.args.getlist("tag")
    if len(tags) < 2:
        return jsonify({"error": "Pick at least 2 models."}), 400
    try:
        entries = list_tags(DEFAULT_PROJECT, state, year_num, month_num)
    except Exception as e:
        return jsonify({"error": f"Could not load models: {type(e).__name__}: {e}"}), 500
    chosen = [e for e in entries if e.tag in tags]
    if len(chosen) != len(tags):
        return jsonify({"error": "One of the selected models is no longer in the archive - refresh and try again."}), 400
    try:
        columns = list_common_columns(DEFAULT_PROJECT, chosen)
    except Exception as e:
        return jsonify({"error": f"Could not load common columns: {type(e).__name__}: {e}"}), 500
    return jsonify({"columns": columns})


@app.route("/models/compare", methods=["POST"])
def models_compare():
    """Starts comparing 2-3 models the user picked from the sidebar on
    one shared column, identified by their stable ModelEntry.key (never
    a raw table name from the client - see ModelEntry.key's docstring) -
    re-resolved against a fresh list_tags() call here before building
    any SQL, so a stale/forged key just fails the lookup rather than
    reaching BigQuery. No pre-flight cost estimate/confirmation here -
    calc.model_registry_compare_models can't be cost-estimated before
    running (see model_library.py's module docstring).

    Runs in a background thread (see _run_compare_job) rather than
    blocking this request for however long up to 3 sequential CALLs
    take - returns a job_id immediately; the sidebar polls
    /models/compare/status/<job_id> for live progress and the final
    result, same pattern this app already uses for the sign checker
    (JOBS) and the Quality page's own aggregate query (QUALITY_JOBS)."""
    data = request.get_json(silent=True) or {}
    state = (data.get("state") or "").strip()
    year_num, month_num = data.get("year_num"), data.get("month_num")
    keys = data.get("keys") or []
    column = (data.get("column") or "").strip()

    if not state or not isinstance(year_num, int) or not isinstance(month_num, int):
        return jsonify({"error": "state, year_num, and month_num are required."}), 400
    if not (2 <= len(keys) <= MAX_COMPARE_MODELS):
        return jsonify({"error": f"Pick 2-{MAX_COMPARE_MODELS} models to compare."}), 400
    if len(set(keys)) != len(keys):
        return jsonify({"error": "Pick distinct models to compare."}), 400
    if not column:
        return jsonify({"error": "Pick a column to compare."}), 400

    try:
        entries = list_tags(DEFAULT_PROJECT, state, year_num, month_num)
    except Exception as e:
        return jsonify({"error": f"Could not load models: {type(e).__name__}: {e}"}), 500
    chosen = [find_model(entries, key) for key in keys]
    if any(e is None for e in chosen):
        return jsonify({"error": "One of the selected models is no longer in the archive - refresh the list and try again."}), 400

    try:
        common_columns = list_common_columns(DEFAULT_PROJECT, chosen)
    except Exception as e:
        return jsonify({"error": f"Could not verify {column}: {type(e).__name__}: {e}"}), 500
    if column not in common_columns:
        return jsonify({"error": f"{column} isn't a common numeric column across the selected models - refresh and pick again."}), 400

    job_id = _new_compare_job()
    thread = threading.Thread(target=_run_compare_job, args=(job_id, column, chosen), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id}), 202


@app.route("/models/compare/status/<job_id>")
def models_compare_status(job_id):
    """Polled by the sidebar every ~1s while a comparison runs - live
    pairs_done/pairs_total + current_pair_label, a running (gb, cost_usd)
    estimate (completed pairs' real billed bytes plus the in-progress
    pair's own polled total_bytes_processed - see
    model_library.compare_models's docstring for why that in-progress
    number is available even though a pre-flight dry-run estimate isn't),
    and every completed pair's full result (summary + histogram + capped
    disagreement sample) as soon as it finishes, not just at the end."""
    job = _get_compare_job(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Unknown or expired comparison job - try again."}), 404
    bytes_so_far = job.get("bytes_so_far", 0) + job.get("current_pair_bytes_processed", 0)
    gb, cost_usd = bytes_to_gb_cost(bytes_so_far)
    return jsonify({
        "status": job["status"], "error": job.get("error"),
        "column": job.get("column"),
        "pairs_total": job.get("pairs_total", 0), "pairs_done": job.get("pairs_done", 0),
        "current_pair_label": job.get("current_pair_label"),
        "gb": gb, "cost_usd": cost_usd,
        "pairs": job.get("pairs", []),
    })


def _validate_quality_custom_text(text: str, base: QualityFilters):
    """Returns (CustomTestResult, None) on success or (None, (response,
    status)) on failure - shared by both "Custom test" endpoints below,
    which must each independently re-validate the raw text against the
    live current table's columns. Never accept an already-validated SQL
    string (or a cached answer) from the client for reuse here - see
    custom_metrics.py's module docstring for why that would defeat the
    whole point of validating in the first place. The result's `kind` is
    "expression" (a validated comparison, safe to run against BigQuery),
    "answer" (a plain-text response to a general question about
    Moove/Archimedes - never touches SQL; the caller should just display
    it), or "example" (a sample comparison for the caller to show as an
    editable starting point - also never touches SQL/BigQuery; it's shown,
    not run)."""
    if not text:
        return None, (jsonify({"error": "Enter a comparison, describe one in plain English, or ask a question about Moove or Archimedes."}), 400)
    try:
        available_columns = list_table_columns(DEFAULT_PROJECT, base.dataset, base.table)
    except Exception as e:
        return None, (
            jsonify({"error": f"Could not check {base.dataset}.{base.table}'s columns: {type(e).__name__}: {e}"}), 500,
        )
    try:
        result = resolve_custom_criterion(text, available_columns, DEFAULT_PROJECT)
    except ExpressionError as e:
        return None, (jsonify({"error": str(e)}), 400)
    return result, None


@app.route("/speed-limits/custom-test", methods=["POST"])
def quality_custom_test():
    """The "Custom test" box's result: either how many segments (of the
    current state/functional_class/zip/county filters) match a
    user-defined comparison - typed directly, or translated from plain
    English by Gemini - or, if the text was a general question about
    Moove/Archimedes instead, Gemini's plain-text answer to it (no
    BigQuery query is run in that case). See custom_metrics.py. The
    comparison path is synchronous, not a background job - it's one cheap
    COUNT/SUM aggregate, not the nationwide six-metric query the rest of
    this page runs."""
    text = request.form.get("text", "").strip()
    sql_response = _sql_console_response(text)
    if sql_response is not None:
        return sql_response
    base, err = _quality_filters_from_args(request.form, require_infer_field=False)
    if err:
        return err
    result, err = _validate_quality_custom_text(text, base)
    if err:
        return err
    if result.kind == "answer":
        return jsonify({"kind": "answer", "answer": result.answer, "source": result.source, "sources": result.sources})
    if result.kind == "example":
        return jsonify({"kind": "example", "example": result.example, "explanation": result.explanation, "source": result.source})
    validated = result.expression
    try:
        metric = fetch_custom_metric(base, validated.sql)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    total = metric.get("total_segments") or 0
    matching = metric.get("matching_count") or 0
    return jsonify({
        "kind": "expression",
        "sql": validated.sql,
        "has_magnitude": validated.magnitude_sql is not None,
        "source": result.source,
        "explanation": result.explanation,
        "total_segments": total,
        "matching_count": matching,
        "pct_matching": (100.0 * matching / total) if total else 0.0,
    })


@app.route("/speed-limits/custom-test/sample", methods=["POST"])
def quality_custom_test_sample():
    """The Custom test box's "show these on a map" - up to
    PREVIEW_MAX_SEGMENTS real matching segments, worst-first when the
    expression has a natural magnitude (see
    custom_metrics.ValidatedExpression.magnitude_sql). If the text turned
    out to be a general question (kind == "answer"), there's no map to
    show - just returns the same answer as the aggregate endpoint."""
    text = request.form.get("text", "").strip()
    base, err = _quality_filters_from_args(request.form, require_infer_field=False)
    if err:
        return err
    result, err = _validate_quality_custom_text(text, base)
    if err:
        return err
    if result.kind == "answer":
        return jsonify({"kind": "answer", "answer": result.answer, "source": result.source, "sources": result.sources})
    if result.kind == "example":
        return jsonify({"kind": "example", "example": result.example, "explanation": result.explanation, "source": result.source})
    validated = result.expression
    try:
        rows = fetch_custom_sample(base, validated.sql, validated.magnitude_sql, PREVIEW_MAX_SEGMENTS)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({
        "kind": "expression",
        "points": [
            {
                "segment_id": r.get("here_segment_id"), "lat": r.get("lat"), "lon": r.get("lon"),
                "street_name": r.get("street_name"), "functional_class": r.get("functional_class"),
                "state": r.get("state"), "magnitude": r.get("magnitude"),
            }
            for r in rows
        ],
        "shown": len(rows),
        "cap": PREVIEW_MAX_SEGMENTS,
        "sql": validated.sql,
        "source": result.source,
    })


@app.route("/sign-checker", methods=["GET"])
def sign_checker():
    return render_template(
        "index.html",
        default_project=DEFAULT_PROJECT,
        default_dataset=DEFAULT_DATASET,
        default_state=request.args.get("state", "").strip().upper(),
        default_custom_test=request.args.get("custom_test", ""),
        criteria_defs=CRITERIA_DEFS,
        default_headings=WEB_DEFAULT_HEADINGS,
        default_headings_relative=WEB_DEFAULT_HEADINGS_RELATIVE,
        default_fov=WEB_DEFAULT_FOV,
        min_fov=MIN_FOV,
        max_fov=MAX_FOV,
        default_walk_all=WEB_DEFAULT_WALK_ALL,
        default_walk_segment=WEB_DEFAULT_WALK_SEGMENT,
        default_walk_segment_spacing_m=WEB_DEFAULT_WALK_SEGMENT_SPACING_M,
        default_side_mode=WEB_DEFAULT_SIDE_MODE,
        default_auto_side_offset=WEB_DEFAULT_AUTO_SIDE_OFFSET,
        default_candidates=WEB_DEFAULT_CANDIDATES,
    )


@app.route("/sign-checker/preview-candidates", methods=["POST"])
def sign_checker_preview_candidates():
    project = request.form.get("project", "").strip() or DEFAULT_PROJECT
    dataset = request.form.get("dataset", "").strip() or DEFAULT_DATASET
    try:
        requested_count = max(1, int(request.form.get("candidates") or WEB_DEFAULT_CANDIDATES))
    except ValueError:
        requested_count = WEB_DEFAULT_CANDIDATES
    return _preview_candidates_response(
        project, dataset,
        request.form.get("state", "").strip().upper(), request.form.get("year", "").strip(), request.form.get("month", "").strip(),
        _read_criteria_from_form(request.form),
        _parse_csv_field(request.form.get("zip_codes", "")), _parse_csv_field(request.form.get("counties", "")),
        requested_count,
        custom_criterion_text=request.form.get("custom_test", ""),
    )


@app.route("/run", methods=["POST"])
def run():
    state = request.form.get("state", "").strip().upper()
    year = request.form.get("year", "").strip()
    month = request.form.get("month", "").strip()
    project = request.form.get("project", "").strip() or DEFAULT_PROJECT
    dataset = request.form.get("dataset", "").strip() or DEFAULT_DATASET
    max_candidates = int(request.form.get("candidates") or WEB_DEFAULT_CANDIDATES)
    segment_id = request.form.get("segment_id", "").strip() or None
    walk_all = request.form.get("walk_all") is not None
    walk_segment = request.form.get("walk_segment") is not None
    try:
        walk_segment_spacing_m = float(request.form.get("walk_segment_spacing_m") or 15.0)
    except ValueError:
        walk_segment_spacing_m = 15.0
    side_mode = request.form.get("side_mode", "").strip()
    if side_mode not in SIDE_MODES:
        side_mode = "center"
    try:
        side_offset_m = float(request.form.get("side_offset_m") or 20.0)
    except ValueError:
        side_offset_m = 20.0
    auto_side_offset = request.form.get("auto_side_offset") is not None
    headings_raw = request.form.get("headings", "").strip()
    try:
        headings = parse_headings(headings_raw) if headings_raw else DEFAULT_HEADINGS
    except ValueError:
        headings = DEFAULT_HEADINGS
    headings_relative = request.form.get("headings_relative") is not None
    try:
        fov = int(float(request.form.get("fov") or DEFAULT_FOV))
    except ValueError:
        fov = DEFAULT_FOV
    if not (MIN_FOV <= fov <= MAX_FOV):
        fov = DEFAULT_FOV
    criteria = _read_criteria_from_form(request.form)
    zip_codes = _parse_csv_field(request.form.get("zip_codes", ""))
    counties = _parse_csv_field(request.form.get("counties", ""))
    custom_criterion_text = request.form.get("custom_test", "").strip()

    if not (state and year and month):
        return render_template("error.html", message="State, year, and month are all required.", log=[])

    # Same re-validation as evaluator_start() - ignored (like zip/county
    # and the checkbox criteria already are) when checking one specific
    # segment_id, since that path bypasses candidate selection entirely.
    custom_criterion_sql = None
    if custom_criterion_text and not segment_id:
        try:
            _validate_identifier(state, STATE_RE, "state (expected 2 letters, e.g. NC)")
            table = table_name(state, year, month)
            available_columns = list_table_columns(project, dataset, table)
            validated, _source, _explanation = resolve_custom_criterion_as_expression(
                custom_criterion_text, available_columns, DEFAULT_PROJECT,
            )
            custom_criterion_sql = validated.sql
        except (ValueError, ExpressionError) as e:
            return render_template("error.html", message=f"Custom test: {e}", log=[])

    job_id = _new_job(state, year, month, segment_id, walk_all, walk_segment)
    thread = threading.Thread(
        target=_run_job,
        args=(
            job_id, state, year, month, project, dataset, max_candidates,
            criteria, zip_codes, counties, custom_criterion_sql, segment_id, walk_all, walk_segment, walk_segment_spacing_m,
            side_mode, side_offset_m, auto_side_offset, headings, headings_relative, fov,
        ),
        daemon=True,
    )
    thread.start()
    return redirect(url_for("progress_page", job_id=job_id))


@app.route("/progress/<job_id>")
def progress_page(job_id):
    job = _get_job(job_id)
    if not job:
        return render_template("error.html", message="Unknown or expired job.", log=[])
    if job["status"] == "done":
        return redirect(url_for("result_page", job_id=job_id))
    if job["status"] == "error":
        return redirect(url_for("error_page", job_id=job_id))
    return render_template(
        "progress.html",
        job_id=job_id,
        state=job["state"],
        year=job["year"],
        month=job["month"],
        segment_id=job["segment_id"],
    )


@app.route("/status/<job_id>")
def status(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"status": "error", "progress": 0, "message": "Unknown or expired job.", "log": []})
    return jsonify(
        {
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "log": job["log"][-100:],
        }
    )


@app.route("/result/<job_id>")
def result_page(job_id):
    job = _get_job(job_id)
    if not job:
        return render_template("error.html", message="Unknown or expired job.", log=[])
    if job["status"] == "error":
        return redirect(url_for("error_page", job_id=job_id))
    if job["status"] != "done":
        return redirect(url_for("progress_page", job_id=job_id))

    result = job["result"]

    # Each candidate's images now carry their own lat/lon (not just the
    # segment's shared centroid), since --walk-segment can fetch them from
    # different positions along the segment - so the page can plot each
    # image's actual location/direction, not an approximation.
    attempts_view = []
    for a in result.attempts:
        images_js = [
            {"url": _image_url(d.path), "heading": d.heading, "snippet": d.ocr_snippet, "lat": d.lat, "lon": d.lon, "date": d.capture_date}
            for d in a.image_details
        ]
        matched_index = a.matched_image_index if a.matched_image_index is not None else 0
        # Where/which way to start the interactive Street View panorama:
        # the matched sign's position if there is one, else the first
        # position checked.
        explore_detail = a.image_details[matched_index] if images_js else None
        attempts_view.append(
            {
                "attempt": a,
                "images_js": images_js,
                "annotated_url": _image_url(a.annotated_image) if a.annotated_image else None,
                "matched_index": matched_index,
                "explore_lat": explore_detail.lat if explore_detail else a.lat,
                "explore_lon": explore_detail.lon if explore_detail else a.lon,
                "explore_heading": (explore_detail.heading or 0) if explore_detail else 0,
            }
        )

    # A separate, plain-dict list for the overview map's markers (not
    # attempts_view itself - CandidateAttempt is a dataclass, not
    # JSON-serializable via Jinja's |tojson without this).
    map_points_js = [
        {
            "segment_id": a.segment_id, "status": a.status,
            "lat": entry["explore_lat"], "lon": entry["explore_lon"],
            "street_name": a.row.get("street_name") if a.row else None,
        }
        for entry, a in zip(attempts_view, result.attempts)
    ]

    return render_template(
        "result.html",
        state=job["state"],
        year=job["year"],
        month=job["month"],
        result=result,
        log=job["log"],
        attempts_view=attempts_view,
        map_points_js=map_points_js,
        google_maps_js_key=_google_maps_js_key(),
    )


@app.route("/error/<job_id>")
def error_page(job_id):
    job = _get_job(job_id)
    if not job:
        return render_template("error.html", message="Unknown or expired job.", log=[])
    return render_template("error.html", message=job["error"] or "Unknown error.", log=job["log"])


@app.route("/speed-limits-evaluator", methods=["GET"])
def evaluator_index():
    return render_template(
        "evaluator.html",
        batches=list_batches(),
        criteria_defs=CRITERIA_DEFS,
        us_state_codes=US_STATE_CODES,
        default_year=DEFAULT_QUALITY_YEAR,
        default_month=DEFAULT_QUALITY_MONTH,
        default_project=DEFAULT_PROJECT,
        default_dataset=request.args.get("dataset", "").strip() or DEFAULT_DATASET,
        default_state=request.args.get("state", "").strip().upper(),
        default_custom_test=request.args.get("custom_test", ""),
        default_segment_count=EVALUATOR_DEFAULT_SEGMENT_COUNT,
        max_segment_count=MAX_EVALUATOR_SEGMENT_COUNT,
        default_concurrency=EVALUATOR_DEFAULT_CONCURRENCY,
        max_concurrency=MAX_EVALUATOR_CONCURRENCY,
        default_headings=WEB_DEFAULT_HEADINGS,
        default_headings_relative=WEB_DEFAULT_HEADINGS_RELATIVE,
        default_fov=WEB_DEFAULT_FOV,
        min_fov=MIN_FOV,
        max_fov=MAX_FOV,
        default_walk_segment=WEB_DEFAULT_WALK_SEGMENT,
        default_walk_segment_spacing_m=WEB_DEFAULT_WALK_SEGMENT_SPACING_M,
        default_side_mode=WEB_DEFAULT_SIDE_MODE,
        default_auto_side_offset=WEB_DEFAULT_AUTO_SIDE_OFFSET,
        history_stats=run_history_stats(),
        streetview_rate_per_1000=STREETVIEW_RATE_PER_1000,
        vision_rate_per_1000=VISION_RATE_PER_1000,
        daily_cost_cap=DAILY_COST_CAP_USD,
        today_spent=daily_spend_for_user(_requester_email()),
    )


@app.route("/speed-limits-evaluator/preview-candidates", methods=["POST"])
def evaluator_preview_candidates():
    project = request.form.get("project", "").strip() or DEFAULT_PROJECT
    dataset = request.form.get("dataset", "").strip() or DEFAULT_DATASET
    try:
        requested_count = max(1, min(MAX_EVALUATOR_SEGMENT_COUNT, int(request.form.get("segment_count") or EVALUATOR_DEFAULT_SEGMENT_COUNT)))
    except ValueError:
        requested_count = EVALUATOR_DEFAULT_SEGMENT_COUNT
    return _preview_candidates_response(
        project, dataset,
        request.form.get("state", "").strip().upper(), request.form.get("year", "").strip(), request.form.get("month", "").strip(),
        _read_criteria_from_form(request.form),
        _parse_csv_field(request.form.get("zip_codes", "")), _parse_csv_field(request.form.get("counties", "")),
        requested_count,
        custom_criterion_text=request.form.get("custom_test", ""),
    )


@app.route("/speed-limits-evaluator/start", methods=["POST"])
def evaluator_start():
    state = request.form.get("state", "").strip().upper()
    year = request.form.get("year", "").strip()
    month = request.form.get("month", "").strip()
    project = request.form.get("project", "").strip() or DEFAULT_PROJECT
    dataset = request.form.get("dataset", "").strip() or DEFAULT_DATASET
    label = request.form.get("label", "").strip()

    if not (state and year and month):
        return render_template("error.html", message="State, year, and month are all required.", log=[])

    try:
        segment_count = max(1, min(MAX_EVALUATOR_SEGMENT_COUNT, int(request.form.get("segment_count") or EVALUATOR_DEFAULT_SEGMENT_COUNT)))
    except ValueError:
        segment_count = EVALUATOR_DEFAULT_SEGMENT_COUNT
    try:
        concurrency = max(1, min(MAX_EVALUATOR_CONCURRENCY, int(request.form.get("concurrency") or EVALUATOR_DEFAULT_CONCURRENCY)))
    except ValueError:
        concurrency = EVALUATOR_DEFAULT_CONCURRENCY

    walk_segment = request.form.get("walk_segment") is not None
    try:
        walk_segment_spacing_m = float(request.form.get("walk_segment_spacing_m") or WEB_DEFAULT_WALK_SEGMENT_SPACING_M)
    except ValueError:
        walk_segment_spacing_m = WEB_DEFAULT_WALK_SEGMENT_SPACING_M
    side_mode = request.form.get("side_mode", "").strip()
    if side_mode not in SIDE_MODES:
        side_mode = WEB_DEFAULT_SIDE_MODE
    try:
        side_offset_m = float(request.form.get("side_offset_m") or 20.0)
    except ValueError:
        side_offset_m = 20.0
    auto_side_offset = request.form.get("auto_side_offset") is not None
    headings_raw = request.form.get("headings", "").strip()
    try:
        headings = parse_headings(headings_raw) if headings_raw else DEFAULT_HEADINGS
    except ValueError:
        headings = DEFAULT_HEADINGS
    headings_relative = request.form.get("headings_relative") is not None
    try:
        fov = int(float(request.form.get("fov") or WEB_DEFAULT_FOV))
    except ValueError:
        fov = WEB_DEFAULT_FOV
    if not (MIN_FOV <= fov <= MAX_FOV):
        fov = WEB_DEFAULT_FOV
    criteria = _read_criteria_from_form(request.form)
    zip_codes = _parse_csv_field(request.form.get("zip_codes", ""))
    counties = _parse_csv_field(request.form.get("counties", ""))
    custom_criterion_text = request.form.get("custom_test", "").strip()

    label = label or f"{state}_{year}_{month}_{_now_label_suffix()}"

    # Re-validated here (never trust a client-supplied "already validated"
    # SQL string) against this specific state/year/month table's live
    # columns - see custom_metrics.py's module docstring. Done
    # synchronously so a bad custom test is rejected immediately, same as
    # every other validation error in this route, rather than starting a
    # batch that only fails once run_batch gets to it.
    custom_criterion_sql = None
    if custom_criterion_text:
        try:
            _validate_identifier(state, STATE_RE, "state (expected 2 letters, e.g. NC)")
            table = table_name(state, year, month)
            available_columns = list_table_columns(project, dataset, table)
            validated, _source, _explanation = resolve_custom_criterion_as_expression(
                custom_criterion_text, available_columns, DEFAULT_PROJECT,
            )
            custom_criterion_sql = validated.sql
        except (ValueError, ExpressionError) as e:
            return render_template("error.html", message=f"Custom test: {e}", log=[])

    # Same $600/day/user cap run_batch itself re-checks live as the run
    # progresses (see batch_evaluator.DAILY_COST_CAP_USD) - checked here
    # too so a user who's already over today's cap gets a clear rejection
    # immediately, rather than a batch that's created only to be stopped
    # at its very first status write.
    requester = _requester_email()
    spent_today = daily_spend_for_user(requester)
    if spent_today >= DAILY_COST_CAP_USD:
        return render_template(
            "error.html",
            message=(
                f"Daily cost cap reached: {requester} has already spent ${spent_today:,.2f} today, "
                f"at or over the ${DAILY_COST_CAP_USD:,.0f}/day/user cap. Try again after midnight UTC."
            ),
            log=[],
        )

    config = BatchConfig(
        label=label, state=state, year=year, month=month, project=project, dataset=dataset,
        segment_count=segment_count, concurrency=concurrency, criteria=criteria,
        zip_codes=zip_codes, counties=counties,
        custom_criterion_text=custom_criterion_text, custom_criterion_sql=custom_criterion_sql,
        walk_segment=walk_segment, walk_segment_spacing_m=walk_segment_spacing_m,
        side_mode=side_mode, side_offset_m=side_offset_m, auto_side_offset=auto_side_offset,
        headings=headings, headings_relative=headings_relative, fov=fov,
        started_by=requester,
    )

    batch_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    with BATCH_CANCEL_EVENTS_LOCK:
        BATCH_CANCEL_EVENTS[batch_id] = cancel_event
        while len(BATCH_CANCEL_EVENTS) > EVALUATOR_MAX_JOBS_IN_MEMORY:
            del BATCH_CANCEL_EVENTS[next(iter(BATCH_CANCEL_EVENTS))]

    thread = threading.Thread(target=run_batch, args=(batch_id, config, cancel_event), daemon=True)
    thread.start()
    return redirect(url_for("evaluator_status_page", batch_id=batch_id))


@app.route("/speed-limits-evaluator/<batch_id>")
def evaluator_status_page(batch_id):
    status = read_status(batch_id)
    if not status:
        return render_template("error.html", message="Unknown or expired batch run.", log=[])
    return render_template(
        "evaluator_status.html",
        batch_id=batch_id,
        status=status,
        fc_breakdown=breakdown_by_functional_class(status.get("results") or []),
    )


@app.route("/speed-limits-evaluator/<batch_id>/status")
def evaluator_status_json(batch_id):
    status = read_status(batch_id)
    if not status:
        return jsonify({"run_status": "error", "error": "Unknown or expired batch run."})
    return jsonify(status)


@app.route("/speed-limits-evaluator/<batch_id>/cancel", methods=["POST"])
def evaluator_cancel(batch_id):
    with BATCH_CANCEL_EVENTS_LOCK:
        event = BATCH_CANCEL_EVENTS.get(batch_id)
    if event:
        event.set()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "Not running in this process (already finished, or a prior instance)."})


@app.route("/speed-limits-evaluator/<batch_id>/csv")
def evaluator_csv(batch_id):
    path = csv_path_if_exists(batch_id)
    if not path:
        return render_template("error.html", message="No CSV available for this batch run yet.", log=[])
    return Response(
        path.read_text(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{batch_id}.csv"'},
    )


@app.route("/images/<path:filename>")
def images(filename):
    # When a GCS-backed cache is configured (e.g. Cloud Run, whose local
    # disk doesn't survive restarts or is per-instance), a file missing
    # locally may still exist there from a prior run/instance - pull it
    # down before serving. Contained to OUT_DIR (resolved/checked before
    # any filesystem operation) so a crafted filename can't be used to
    # probe or write outside it; send_from_directory below independently
    # guards the actual response the same way regardless.
    if GCS_CACHE_BUCKET:
        local_path = (OUT_DIR / filename).resolve()
        out_root = OUT_DIR.resolve()
        if local_path == out_root or out_root in local_path.parents:
            gcs_cache_pull(local_path)
    return send_from_directory(OUT_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
