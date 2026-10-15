#!/usr/bin/env bash
# Run the parallel speed-limit variant training (avgspeed_only, freeflow_only) for a month.
#
#   ./run_variants.sh                       full run for the default month (resumes; registered states are skipped)
#   ./run_variants.sh --dry-run             print the plan, touch nothing
#   ./run_variants.sh --states MT,NE,NV --workers 3 --variants avgspeed_only     pilot
#   ./run_variants.sh --restore-avgspeed    after the run, re-save avgspeed_only as the latest toggle row
#
# Settings (environment variables, all optional):
#   YEAR=2026 MONTH=9 WORKERS=8 PROJECT=moove-platform-testing-data
#   BRANCH=claude/clever-johnson-lqw9nb   (branch the runner is fetched from; never checked out)
#   SKIP_AUTH_CHECK=1                     (on a GCE VM: credentials come from the VM's service account)
# Any other arguments are passed straight to run_variants_parallel.py (they win over the settings above).
set -euo pipefail

YEAR="${YEAR:-2026}"
MONTH="${MONTH:-9}"
WORKERS="${WORKERS:-8}"
PROJECT="${PROJECT:-moove-platform-testing-data}"
BRANCH="${BRANCH:-claude/clever-johnson-lqw9nb}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$HERE/run_variants_parallel.py"
VENV="$HERE/.venv-variants"

restore=0
args=()
for a in "$@"; do
  if [[ "$a" == "--restore-avgspeed" ]]; then restore=1; else args+=("$a"); fi
done

# 1. latest runner from the branch (git show: no checkout, no branch switch, working tree untouched)
if git -C "$HERE" fetch --quiet origin "$BRANCH" 2>/dev/null &&
   git -C "$HERE" show "origin/$BRANCH:scripts/run_variants_parallel.py" > "$RUNNER.tmp" 2>/dev/null; then
  mv "$RUNNER.tmp" "$RUNNER"
  echo "runner: fetched from origin/$BRANCH"
else
  rm -f "$RUNNER.tmp"
  [[ -f "$RUNNER" ]] || { echo "cannot fetch the runner from origin/$BRANCH and no local copy exists" >&2; exit 1; }
  echo "runner: fetch failed, using the existing local copy"
fi

# 2. python environment with the BigQuery client
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c "import google.cloud.bigquery" 2>/dev/null; then
  "$VENV/bin/pip" install --quiet google-cloud-bigquery
fi

# 3. credentials
if [[ "${SKIP_AUTH_CHECK:-0}" != "1" ]] && ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "no application-default credentials; logging in"
  gcloud auth application-default login
fi

# 4. run (re-run the same command to resume after any stop)
rc=0
"$VENV/bin/python" "$RUNNER" --year "$YEAR" --month "$MONTH" --project "$PROJECT" --workers "$WORKERS" ${args[@]+"${args[@]}"} || rc=$?

# 5. optional: leave avgspeed_only as the latest toggle row for this period
if [[ $restore -eq 1 ]]; then
  echo "re-saving avgspeed_only as the latest feature_toggle row for $YEAR-$MONTH"
  bq --project_id="$PROJECT" query --use_legacy_sql=false \
    "CALL calc.model_registry_prepare_speed_limit_model_variants(CAST([] AS ARRAY<STRING>), $YEAR, $MONTH, FALSE, ['avgspeed_only'], TRUE);"
fi

exit $rc
