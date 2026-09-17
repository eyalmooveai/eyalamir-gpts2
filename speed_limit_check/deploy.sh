#!/usr/bin/env bash
# Deploys the speed limit sign checker web app to Cloud Run.
#
# Required environment variables (set these before running):
#   PROJECT_ID     GCP project to deploy the Cloud Run service into
#   REGION         e.g. us-central1
#   BUCKET_NAME    GCS bucket name for the persistent Street View/OCR cache
#   MAPS_API_KEY   Google Maps Platform API key (Street View Static API enabled)
#   INVOKER_EMAIL  Google account allowed to use the deployed app (roles/run.invoker)
#
# Optional:
#   BQ_PROJECT_ID    GCP project that owns the speed_limits_..._details
#                    BigQuery table, if different from PROJECT_ID
#                    (default: moove-platform-testing-data)
#   BUCKET_LOCATION  GCS bucket location, e.g. "US" (a multi-region) or a
#                    single region - separate from REGION because Cloud
#                    Run needs a specific region (no "US") while a GCS
#                    bucket can use a broader multi-region (default: REGION)
#
# Usage:
#   PROJECT_ID=my-project REGION=us-central1 BUCKET_NAME=my-bucket \
#   MAPS_API_KEY=AIza... INVOKER_EMAIL=me@example.com ./deploy.sh
#
# Safe to re-run - existing resources (bucket, secret, service account) are
# detected and left alone rather than recreated.

set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID to your GCP project ID for deploying this service}"
: "${REGION:?Set REGION, e.g. us-central1}"
: "${BUCKET_NAME:?Set BUCKET_NAME for the persistent Street View/OCR cache}"
: "${MAPS_API_KEY:?Set MAPS_API_KEY to your Google Maps Platform API key}"
: "${INVOKER_EMAIL:?Set INVOKER_EMAIL to the Google account that should be allowed to use the deployed app}"
BQ_PROJECT_ID="${BQ_PROJECT_ID:-moove-platform-testing-data}"
BUCKET_LOCATION="${BUCKET_LOCATION:-$REGION}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI not found - install the Google Cloud SDK first: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi

export CLOUDSDK_CORE_DISABLE_PROMPTS=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="speed-limit-check"
SA_NAME="speed-limit-check-runner"
SECRET_NAME="speed-limit-check-maps-key"
SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

echo "== Deploying $SERVICE_NAME to project $PROJECT_ID ($REGION) =="

gcloud config set project "$PROJECT_ID"

echo "-- Enabling required APIs --"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  bigquery.googleapis.com \
  vision.googleapis.com

echo "-- GCS cache bucket --"
if gcloud storage buckets describe "gs://$BUCKET_NAME" >/dev/null 2>&1; then
  echo "gs://$BUCKET_NAME already exists, skipping creation."
else
  gcloud storage buckets create "gs://$BUCKET_NAME" --location="$BUCKET_LOCATION"
fi

echo "-- Maps API key secret --"
if gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
  printf '%s' "$MAPS_API_KEY" | gcloud secrets versions add "$SECRET_NAME" --data-file=-
else
  printf '%s' "$MAPS_API_KEY" | gcloud secrets create "$SECRET_NAME" --data-file=-
fi

echo "-- Runtime service account --"
if gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
  echo "$SA already exists, skipping creation."
else
  gcloud iam service-accounts create "$SA_NAME" --display-name="Speed limit sign checker (Cloud Run)"
fi

echo "-- Granting IAM roles --"
gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member="serviceAccount:$SA" --role="roles/bigquery.dataViewer" --condition=None
gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member="serviceAccount:$SA" --role="roles/bigquery.jobUser" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="roles/cloudvision.user" --condition=None
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"

echo "-- Deploying to Cloud Run --"
gcloud run deploy "$SERVICE_NAME" \
  --source "$SCRIPT_DIR" \
  --region "$REGION" \
  --service-account "$SA" \
  --no-allow-unauthenticated \
  --no-cpu-throttling \
  --max-instances=1 \
  --memory=1Gi \
  --set-env-vars="GCS_CACHE_BUCKET=$BUCKET_NAME" \
  --set-secrets="GOOGLE_MAPS_API_KEY=$SECRET_NAME:latest"

echo "-- Granting invoker access to $INVOKER_EMAIL --"
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region "$REGION" \
  --member="user:$INVOKER_EMAIL" \
  --role="roles/run.invoker"

cat <<EOF

Deployed.

A plain browser visit to the service URL will 403 even for a granted
user - there's no token attached to a normal page load. To open it:

  gcloud run services proxy $SERVICE_NAME --region $REGION

then visit http://127.0.0.1:8080

Logs:

  gcloud run services logs read $SERVICE_NAME --region $REGION
EOF
