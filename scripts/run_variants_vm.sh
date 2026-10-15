#!/usr/bin/env bash
# Run run_variants.sh on a throwaway GCE VM, with YOUR gcloud user credentials, so the run
# survives closing/turning off the laptop. Run this on the laptop, from the folder that holds
# run_variants.sh and run_variants_parallel.py (the wrapper creates the latter on its first run).
#
#   ./run_variants_vm.sh up                 create the VM, copy the scripts + your ADC credentials, install deps
#   ./run_variants_vm.sh start [args...]    start the run detached (tmux); args go to run_variants.sh,
#                                           e.g. --workers 6 or --variants avgspeed_only.
#                                           The VM shuts itself down when the run ends (finished or stopped).
#   ./run_variants_vm.sh status             VM state + the last lines of the log
#   ./run_variants_vm.sh down               delete the VM (this also removes the copied credentials)
#
# Re-running `start` resumes: registered states are skipped. It also restarts a stopped VM.
# Do not pass --restore-avgspeed here (bq on the VM has no gcloud login); run that CALL from your laptop.
# Settings (env): PROJECT=moove-platform-testing-data  ZONE=us-central1-a  VM=variants-runner
set -euo pipefail

PROJECT="${PROJECT:-moove-platform-testing-data}"
ZONE="${ZONE:-us-central1-a}"
VM="${VM:-variants-runner}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADC="${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}/application_default_credentials.json"

g()      { gcloud compute "$@" --project "$PROJECT" --zone "$ZONE"; }
vm_ssh() { g ssh "$VM" --command "$1"; }
vm_state() { g instances describe "$VM" --format='value(status)' 2>/dev/null || echo MISSING; }

cmd="${1:-}"; shift || true
case "$cmd" in
  up)
    for f in run_variants.sh run_variants_parallel.py; do
      [[ -f "$HERE/$f" ]] || { echo "missing $HERE/$f" >&2; exit 1; }
    done
    [[ -f "$ADC" ]] || { echo "no $ADC; run: gcloud auth application-default login" >&2; exit 1; }
    # --no-service-account: the VM has no identity of its own; it acts as you via the copied ADC file
    g instances create "$VM" --machine-type e2-small \
      --image-family debian-12 --image-project debian-cloud \
      --no-service-account --no-scopes
    for i in 1 2 3 4 5 6 7 8 9 10; do
      vm_ssh 'true' 2>/dev/null && break
      echo "waiting for ssh ($i)"; sleep 10
    done
    g scp "$HERE/run_variants.sh" "$HERE/run_variants_parallel.py" "$VM:~"
    g scp "$ADC" "$VM:~/adc.json"
    vm_ssh 'mkdir -p ~/.config/gcloud && mv ~/adc.json ~/.config/gcloud/application_default_credentials.json \
      && chmod 600 ~/.config/gcloud/application_default_credentials.json \
      && sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv tmux git \
      && chmod +x ~/run_variants.sh'
    echo "VM ready. Next: $0 start"
    ;;
  start)
    [[ "$(vm_state)" == "TERMINATED" ]] && { g instances start "$VM"; sleep 20; }
    args="$(printf '%q ' "$@")"
    tmp="$(mktemp)"
    cat > "$tmp" <<EOF
#!/usr/bin/env bash
cd ~
SKIP_AUTH_CHECK=1 ./run_variants.sh $args 2>&1 | tee -a run.log
echo "finished (rc=\${PIPESTATUS[0]}) \$(date)" | tee -a run.log
sudo shutdown -h +2
EOF
    g scp "$tmp" "$VM:~/go.sh"; rm -f "$tmp"
    vm_ssh 'tmux kill-session -t variants 2>/dev/null; tmux new-session -d -s variants "bash ~/go.sh"; tmux ls'
    echo "running on the VM; you can turn the laptop off. Check with: $0 status"
    ;;
  status)
    st="$(vm_state)"; echo "VM: $st"
    [[ "$st" == "RUNNING" ]] && vm_ssh 'tmux ls 2>&1; echo ---; tail -n 30 ~/run.log'
    ;;
  down)
    g instances delete "$VM"
    ;;
  *)
    sed -n '2,15p' "$0"; exit 1
    ;;
esac
