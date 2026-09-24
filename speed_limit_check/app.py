#!/usr/bin/env python3
"""Web UI for Archimedes, MooveAI's data quality hub - currently the
speed limit sign checker plus the nationwide Speed-Limits Quality
dashboard, both in this one app/deployment.

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
    NoUsableApiKey,
    StreetViewAuthError,
    gcs_cache_pull,
    load_keys_file,
    parse_headings,
    run_pipeline,
)
from quality_metrics import (
    DEFAULT_INFER_FIELD,
    GROUP_BY_CHOICES,
    QUALITY_METRICS,
    US_STATE_CODES,
    QualityFilters,
    default_table_name,
    fetch_quality_metrics,
    full_table_name,
    list_evaluable_tables,
    list_infer_fields,
    list_table_columns,
    metric_labels,
    missing_columns_report,
)

# The Archimedes hub's model catalog - only "Speed Limits" has a built tool
# today (this app); the rest are placeholders naming what MooveAI expects
# to add here, so the hub is truthful about what exists without promising
# a href that 404s.
HUB_MODELS = [
    {"name": "Speed Limits", "description": "Inferred speed limit quality vs. OSM/HERE/observed speeds, a per-segment Street View sign checker, and a batch evaluator across up to 1000 segments at once.", "href": "/speed-limits"},
    {"name": "Lanes", "description": "Not yet available in Archimedes.", "href": None},
    {"name": "Construction Zones", "description": "Not yet available in Archimedes.", "href": None},
    {"name": "Accident Prediction", "description": "Not yet available in Archimedes.", "href": None},
    {"name": "Accident Detection", "description": "Not yet available in Archimedes.", "href": None},
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


@app.route("/", methods=["GET"])
def hub():
    return render_template("hub.html", models=HUB_MODELS)


@app.route("/speed-limits", methods=["GET"])
def speed_limits_quality():
    error = None

    # Live, not hardcoded - so the selector always matches whatever tables
    # (calc_out's speed_limits_US* family, and archimedes_api's
    # speed_limits_infer* views) actually exist, without this app needing
    # to know about a new one in advance. Each option's dropdown value is
    # "dataset.table" since the two sources live in different datasets.
    # Both this and the infer-field lookup below are fast, cached
    # metadata-only queries (see quality_metrics.py) - only the actual
    # aggregate metrics query is slow enough to need the async job below.
    try:
        table_options = [f"{t['dataset']}.{t['table']}" for t in list_evaluable_tables(DEFAULT_PROJECT)]
    except Exception as e:
        table_options = []
        error = f"Could not list evaluable tables: {type(e).__name__}: {e}"

    default_table = f"{DEFAULT_DATASET}.{default_table_name(DEFAULT_QUALITY_YEAR, DEFAULT_QUALITY_MONTH)}"
    requested_table = request.args.get("table", "").strip()
    selected_option = requested_table or (default_table if default_table in table_options else (table_options[0] if table_options else default_table))
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
            group_by = filters_echo["group_by"] if filters_echo["group_by"] in GROUP_BY_CHOICES else "none"
            filters_echo["group_by"] = group_by

            base = QualityFilters(
                project=DEFAULT_PROJECT, dataset=selected_dataset, table=selected_table,
                infer_field=selected_infer_field, param1=param1, param2=param2,
                states=states, functional_classes=fcs,
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


@app.route("/sign-checker", methods=["GET"])
def sign_checker():
    return render_template(
        "index.html",
        default_project=DEFAULT_PROJECT,
        default_dataset=DEFAULT_DATASET,
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

    if not (state and year and month):
        return render_template("error.html", message="State, year, and month are all required.", log=[])

    job_id = _new_job(state, year, month, segment_id, walk_all, walk_segment)
    thread = threading.Thread(
        target=_run_job,
        args=(
            job_id, state, year, month, project, dataset, max_candidates,
            criteria, segment_id, walk_all, walk_segment, walk_segment_spacing_m,
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
            {"url": _image_url(d.path), "heading": d.heading, "snippet": d.ocr_snippet, "lat": d.lat, "lon": d.lon}
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

    return render_template(
        "result.html",
        state=job["state"],
        year=job["year"],
        month=job["month"],
        result=result,
        log=job["log"],
        attempts_view=attempts_view,
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
        default_dataset=DEFAULT_DATASET,
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

    label = label or f"{state}_{year}_{month}_{_now_label_suffix()}"

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
