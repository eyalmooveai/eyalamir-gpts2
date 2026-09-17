#!/usr/bin/env python3
"""Local web UI for the speed limit sign checker.

Run:
    python app.py

Then open http://127.0.0.1:5050 in a browser. Runs on localhost only.
See README.md for the API keys/credentials this needs.
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, render_template, request

from find_bad_speed_limit import (
    DEFAULT_DATASET,
    DEFAULT_KEYS_FILE,
    DEFAULT_PROJECT,
    NoUsableApiKey,
    StreetViewAuthError,
    run_pipeline,
    safe_segment_dirname,
)

OUT_DIR = Path("output")

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        default_project=DEFAULT_PROJECT,
        default_dataset=DEFAULT_DATASET,
    )


@app.route("/run", methods=["POST"])
def run():
    state = request.form.get("state", "").strip().upper()
    year = request.form.get("year", "").strip()
    month = request.form.get("month", "").strip()
    project = request.form.get("project", "").strip() or DEFAULT_PROJECT
    dataset = request.form.get("dataset", "").strip() or DEFAULT_DATASET
    max_candidates = int(request.form.get("candidates") or 10)

    log_lines: list[str] = []

    if not (state and year and month):
        return render_template("error.html", message="State, year, and month are all required.", log=log_lines)

    try:
        result = run_pipeline(
            state,
            year,
            month,
            project=project,
            dataset=dataset,
            max_candidates=max_candidates,
            out_dir=OUT_DIR,
            keys_file=DEFAULT_KEYS_FILE,
            log=log_lines.append,
        )
    except NoUsableApiKey as e:
        return render_template("error.html", message=str(e), log=log_lines)
    except StreetViewAuthError as e:
        return render_template(
            "error.html",
            message=(
                f"{e}\n\nThis means the API key, billing, or API enablement is broken - not that "
                "there's no imagery. Check that GOOGLE_MAPS_API_KEY is a real key with the Street "
                "View Static API enabled and billing active on its project."
            ),
            log=log_lines,
        )
    except Exception as e:  # BigQuery/Vision auth errors etc. - surface rather than 500
        return render_template("error.html", message=f"{type(e).__name__}: {e}", log=log_lines)

    image_urls = None
    annotated_url = None
    if result.match_row:
        segment_dir = f"{state}_{year}_{month}/{safe_segment_dirname(result.match_row['here_segment_id'])}"
        image_urls = [f"/images/{segment_dir}/{p.name}" for p in (result.match_all_images or [])]
        annotated_url = f"/images/{segment_dir}/{result.match_annotated_image.name}"

    return render_template(
        "result.html",
        state=state,
        year=year,
        month=month,
        result=result,
        log=log_lines,
        image_urls=image_urls,
        annotated_url=annotated_url,
    )


@app.route("/images/<path:filename>")
def images(filename):
    from flask import send_from_directory

    return send_from_directory(OUT_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=False)
