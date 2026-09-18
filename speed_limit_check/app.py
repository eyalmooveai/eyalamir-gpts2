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

import os
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

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
from quality_metrics import GROUP_BY_CHOICES, QUALITY_METRICS, US_STATE_CODES, QualityFilters, fetch_quality_metrics

# The Archimedes hub's model catalog - only "Speed Limits" has a built tool
# today (this app); the rest are placeholders naming what MooveAI expects
# to add here, so the hub is truthful about what exists without promising
# a href that 404s.
HUB_MODELS = [
    {"name": "Speed Limits", "description": "Inferred speed limit quality vs. OSM/HERE/observed speeds, and a per-segment Street View sign checker.", "href": "/speed-limits"},
    {"name": "Lanes", "description": "Not yet available in Archimedes.", "href": None},
    {"name": "Construction Zones", "description": "Not yet available in Archimedes.", "href": None},
    {"name": "Accident Prediction", "description": "Not yet available in Archimedes.", "href": None},
    {"name": "Accident Detection", "description": "Not yet available in Archimedes.", "href": None},
]

# speed_limits_<STATE>_<YEAR>_<MONTH>_details isn't published as a
# "latest" table with all the columns the Quality metrics need (see
# quality_metrics.py's module docstring) - nothing elsewhere in this app
# auto-detects the newest available month either, so this is just the
# current one, same as speed_limits_quality()'s own default below. Update
# both together when a newer month's nationwide _details table exists.
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

# Load once at startup (not just inside run_pipeline's background thread) so
# GOOGLE_MAPS_API_KEY is available for embedding in a page - e.g. the
# Google Maps JavaScript API script tag - even before any job has run.
load_keys_file(DEFAULT_KEYS_FILE)

app = Flask(__name__)

# In-memory job store. A single local user, one run at a time in practice,
# so a plain dict + lock is enough - no need for a task queue/DB here.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _image_url(path: Path) -> str:
    return "/images/" + str(Path(path).relative_to(OUT_DIR)).replace(os.sep, "/")


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


@app.route("/", methods=["GET"])
def hub():
    return render_template("hub.html", models=HUB_MODELS)


@app.route("/speed-limits", methods=["GET"])
def speed_limits_quality():
    year = request.args.get("year", "").strip()
    month = request.args.get("month", "").strip()

    metrics_view = None
    breakdown_view = None
    error = None
    filters_echo = {
        "year": year or DEFAULT_QUALITY_YEAR,
        "month": month or DEFAULT_QUALITY_MONTH,
        "param1": request.args.get("param1", "10").strip() or "10",
        "param2": request.args.get("param2", "80").strip() or "80",
        "states": request.args.get("states", "").strip(),
        "functional_classes": request.args.get("fc", "").strip(),
        "group_by": request.args.get("group_by", "none").strip() or "none",
    }

    # The nationwide (or filtered-nationwide) summary is always computed
    # and shown, even on a fresh page load with no query params at all -
    # it's the headline number the page exists to answer at a glance.
    # Only the breakdown table is opt-in (group_by).
    try:
        param1 = float(filters_echo["param1"])
        param2 = float(filters_echo["param2"])
        states = tuple(s.strip().upper() for s in filters_echo["states"].split(",") if s.strip())
        fcs = tuple(int(v.strip()) for v in filters_echo["functional_classes"].split(",") if v.strip())
        group_by = filters_echo["group_by"] if filters_echo["group_by"] in GROUP_BY_CHOICES else "none"
        filters_echo["group_by"] = group_by

        base = QualityFilters(
            project=DEFAULT_PROJECT, dataset=DEFAULT_DATASET, year=filters_echo["year"], month=filters_echo["month"],
            param1=param1, param2=param2, states=states, functional_classes=fcs,
        )
        metrics_rows = fetch_quality_metrics(base)
        metrics_view = metrics_rows[0] if metrics_rows else None

        if group_by != "none":
            grouped = QualityFilters(
                project=DEFAULT_PROJECT, dataset=DEFAULT_DATASET, year=filters_echo["year"], month=filters_echo["month"],
                param1=param1, param2=param2, states=states, functional_classes=fcs, group_by=group_by,
            )
            breakdown_view = fetch_quality_metrics(grouped)
    except ValueError as e:
        error = str(e)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    return render_template(
        "quality.html",
        filters=filters_echo,
        metrics=metrics_view,
        breakdown=breakdown_view,
        quality_metric_defs=QUALITY_METRICS,
        group_by_choices=GROUP_BY_CHOICES,
        us_state_codes=US_STATE_CODES,
        error=error,
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
    )


@app.route("/run", methods=["POST"])
def run():
    state = request.form.get("state", "").strip().upper()
    year = request.form.get("year", "").strip()
    month = request.form.get("month", "").strip()
    project = request.form.get("project", "").strip() or DEFAULT_PROJECT
    dataset = request.form.get("dataset", "").strip() or DEFAULT_DATASET
    max_candidates = int(request.form.get("candidates") or 10)
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
