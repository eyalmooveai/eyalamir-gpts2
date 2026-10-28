#!/usr/bin/env bash
# Self-contained: run the parallel speed-limit variant training on a throwaway GCE VM, with YOUR gcloud
# user credentials, so the run survives closing/turning off the laptop.
#
# This is the only file you need. Fetch it, then run it from inside your clone of this repo
# (it pulls run_variants_parallel.py from the branch itself; no checkout, no branch switch):
#
#   git fetch origin claude/clever-johnson-lqw9nb
#   git show origin/claude/clever-johnson-lqw9nb:scripts/run_variants_vm.sh > run_variants_vm.sh && chmod +x run_variants_vm.sh
#
#   ./run_variants_vm.sh start [args...]   create the VM if needed, ship the latest runner, run detached (tmux).
#                                          args go to the runner, e.g. --dry-run, --workers 6, --variants avgspeed_only,
#                                          --states MT,NE,NV. The VM shuts itself down when the run ends.
#   ./run_variants_vm.sh status            VM state + last 30 log lines
#   ./run_variants_vm.sh down              delete the VM (also removes the copied credentials)
#
# Re-running `start` resumes: registered states are skipped; a stopped VM is restarted.
# Settings (env): YEAR=2026 MONTH=9 WORKERS=8 PROJECT=moove-platform-testing-data ZONE=us-central1-a
#                 VM=variants-runner BRANCH=claude/clever-johnson-lqw9nb
# Needs: gcloud (logged in), `gcloud auth application-default login` done, Compute Engine API enabled.
set -euo pipefail

YEAR="${YEAR:-2026}"
MONTH="${MONTH:-9}"
WORKERS="${WORKERS:-8}"
PROJECT="${PROJECT:-moove-platform-testing-data}"
ZONE="${ZONE:-us-central1-a}"
VM="${VM:-variants-runner}"
BRANCH="${BRANCH:-claude/clever-johnson-lqw9nb}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADC="${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}/application_default_credentials.json"

g()        { gcloud compute "$@" --project "$PROJECT" --zone "$ZONE"; }
vm_ssh()   { g ssh "$VM" --command "$1"; }
vm_state() { g instances describe "$VM" --format='value(status)' 2>/dev/null || echo MISSING; }

# latest runner from the branch, via git show (works from your clone, or from the script's own folder)
fetch_runner() {
  local out="$1" d
  for d in "$PWD" "$HERE"; do
    if git -C "$d" fetch --quiet origin "$BRANCH" 2>/dev/null \
       && git -C "$d" show "origin/$BRANCH:scripts/run_variants_parallel.py" > "$out" 2>/dev/null \
       && [[ -s "$out" ]]; then
      return 0
    fi
  done
  return 1
}

create_vm() {
  [[ -f "$ADC" ]] || { echo "no $ADC; run: gcloud auth application-default login" >&2; exit 1; }
  # --no-service-account: the VM has no identity of its own; it acts as you via the copied ADC file
  g instances create "$VM" --machine-type e2-small \
    --image-family debian-12 --image-project debian-cloud \
    --no-service-account --no-scopes
  for i in 1 2 3 4 5 6 7 8 9 10; do
    vm_ssh 'true' 2>/dev/null && break
    echo "waiting for ssh ($i)"; sleep 10
  done
  g scp "$ADC" "$VM:~/adc.json"
  vm_ssh 'mkdir -p ~/.config/gcloud && mv ~/adc.json ~/.config/gcloud/application_default_credentials.json \
    && chmod 600 ~/.config/gcloud/application_default_credentials.json \
    && sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv tmux'
}

cmd="${1:-}"; shift || true
case "$cmd" in
  start)
    tmpdir="$(mktemp -d)"; trap 'rm -rf "$tmpdir"' EXIT
    fetch_runner "$tmpdir/run_variants_parallel.py" \
      || { echo "cannot fetch scripts/run_variants_parallel.py from origin/$BRANCH; run this from inside your clone of the repo" >&2; exit 1; }
    case "$(vm_state)" in
      MISSING)    create_vm ;;
      TERMINATED) g instances start "$VM"; sleep 20 ;;
    esac
    args="$(printf '%q ' "$@")"
    cat > "$tmpdir/go.sh" <<EOF
#!/usr/bin/env bash
cd ~
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -c 'import google.cloud.bigquery' 2>/dev/null || .venv/bin/pip install -q google-cloud-bigquery
.venv/bin/python run_variants_parallel.py --year $YEAR --month $MONTH --project $PROJECT --workers $WORKERS $args 2>&1 | tee -a run.log
echo "finished (rc=\${PIPESTATUS[0]}) \$(date)" | tee -a run.log
sudo shutdown -h +2
EOF
    g scp "$tmpdir/run_variants_parallel.py" "$tmpdir/go.sh" "$VM:~"
    vm_ssh 'tmux kill-session -t variants 2>/dev/null; tmux new-session -d -s variants "bash ~/go.sh"; tmux ls'
    echo "running on the VM; you can turn the laptop off. Check with: $0 status   Delete with: $0 down"
    ;;
  status)
    st="$(vm_state)"; echo "VM: $st"
    [[ "$st" == "RUNNING" ]] && vm_ssh 'tmux ls 2>&1; echo ---; tail -n 30 ~/run.log'
    ;;
  down)
    g instances delete "$VM"
    ;;
  *)
    sed -n '2,19p' "$0"; exit 1
    ;;
esac
