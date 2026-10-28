#!/usr/bin/env bash
# Pulls the latest Gemini/Vertex AI changes and redeploys Archimedes to
# Cloud Run. Run from anywhere - it cd's to the repo itself.
set -euo pipefail

gcloud config set account eyal@moove.ai
gh auth switch -h github.com -u eyalmooveai
# Always switch back at the end, even if the deploy below fails - a plain
# trailing command would otherwise be skipped by `set -e` on any error in
# between, leaving the shell/gh/gcloud default identity pointed at moove.ai.
trap 'gcloud config set account eyal@parknav.com; gh auth switch -h github.com -u eyalparknav' EXIT

cd ~/Claude/MooveAI/eyalamir-gpts2
git pull origin main

cd speed_limit_check
source ~/Claude/MooveAI/keys.env

PROJECT_ID=moove-platform-testing-data \
REGION=us-central1 \
BUCKET_NAME=archimedes-control \
INVOKER_EMAIL=eyal@moove.ai \
MAPS_API_KEY="$GOOGLE_MAPS_API_KEY" \
SKIP_IAM_GRANTS=1 \
./deploy.sh
