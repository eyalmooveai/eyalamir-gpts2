#!/usr/bin/env python3
"""Local web UI for the speed limit sign checker.

Run:
    python app.py

Then open http://127.0.0.1:5050 in a browser. Runs on localhost only.
See README.md for the API keys/credentials this needs.
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
    DEFAULT_HEADINGS,
    DEFAULT_KEYS_FILE,
    DEFAULT_PROJECT,
    NoUsableApiKey,
    StreetViewAuthError,
    load_keys_file,
    parse_headings,
    run_pipeline,
)

OUT_DIR = Path("output")
MAX_JOBS = 20  # cap in-memory job history for this long-lived local process

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
    check_both_sides: bool,
    side_offset_m: float,
    headings: tuple[int, ...],
    headings_relative: bool,
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
            check_both_sides=check_both_sides,
            side_offset_m=side_offset_m,
            headings=headings,
            headings_relative=headings_relative,
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
def index():
    return render_template(
        "index.html",
        default_project=DEFAULT_PROJECT,
        default_dataset=DEFAULT_DATASET,
        criteria_defs=CRITERIA_DEFS,
        default_headings=",".join(str(h) for h in DEFAULT_HEADINGS),
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
    check_both_sides = request.form.get("check_both_sides") is not None
    try:
        side_offset_m = float(request.form.get("side_offset_m") or 20.0)
    except ValueError:
        side_offset_m = 20.0
    headings_raw = request.form.get("headings", "").strip()
    try:
        headings = parse_headings(headings_raw) if headings_raw else DEFAULT_HEADINGS
    except ValueError:
        headings = DEFAULT_HEADINGS
    headings_relative = request.form.get("headings_relative") is not None
    criteria = _read_criteria_from_form(request.form)

    if not (state and year and month):
        return render_template("error.html", message="State, year, and month are all required.", log=[])

    job_id = _new_job(state, year, month, segment_id, walk_all, walk_segment)
    thread = threading.Thread(
        target=_run_job,
        args=(
            job_id, state, year, month, project, dataset, max_candidates,
            criteria, segment_id, walk_all, walk_segment, walk_segment_spacing_m,
            check_both_sides, side_offset_m, headings, headings_relative,
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
    return send_from_directory(OUT_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
