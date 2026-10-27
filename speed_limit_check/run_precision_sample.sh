#!/usr/bin/env bash
# Convenience wrapper for the precision_sample.py pilot: pulls the latest
# branch, then runs the script with the parameter set this was tuned
# against in testing (single forward-right-tilted heading, 10m stepping).
#
# Usage:
#   ./run_precision_sample.sh                  # use the defaults below
#   ./run_precision_sample.sh --sample-size 50 # override/add any flag -
#                                               # extra args are appended
#                                               # after the defaults, and
#                                               # argparse takes the LAST
#                                               # value for a repeated flag,
#                                               # so this overrides cleanly
#
# Assumes the repo is already checked out at REPO_DIR (see CLAUDE.md) and
# that you've already activated whichever Python venv has this app's
# requirements.txt installed (same one you've been running
# precision_sample.py from directly) - this script doesn't manage that for
# you, just like running it by hand doesn't.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/Claude/MooveAI/eyalamir-gpts2}"
BRANCH="claude/bigquery-connection-u39o4p"

cd "$REPO_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull origin "$BRANCH"

cd "$REPO_DIR/speed_limit_check"
python3 precision_sample.py \
  --table speed_limits_CO_2026_08_details \
  --state CO \
  --functional-class-lt 3 \
  --sample-size 20 \
  --headings 15 --headings-relative \
  --walk-segment --walk-segment-spacing-m 10 \
  "$@"
