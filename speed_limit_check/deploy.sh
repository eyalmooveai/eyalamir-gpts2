#!/usr/bin/env bash
# Deploys the speed limit sign checker web app to Cloud Run.
#
# Required environment variables (set these before running):
#   PROJECT_ID     GCP project to deploy the Cloud Run service into
#   REGION         e.g. us-central1
#   BUCKET_NAME    GCS bucket name for the persistent Street View/OCR cache
#   MAPS_API_KEY   Google Maps Platform API key (Street View Static API enabled)
#   INVOKER_EMAIL  Google account granted roles/run.invoker directly (only
#                  takes effect when SKIP_IAM_GRANTS is unset - see below;
#                  with IAP fronting the service, who can actually reach it
#                  is governed by roles/iap.httpsResourceAccessor instead,
#                  granted outside this script)
#
# Optional:
#   BQ_PROJECT_ID    GCP project that owns the speed_limits_..._details
#                    BigQuery table, if different from PROJECT_ID
#                    (default: moove-platform-testing-data)
#   BUCKET_LOCATION  GCS bucket location, e.g. "US" (a multi-region) or a
#                    single region - separate from REGION because Cloud
#                    Run needs a specific region (no "US") while a GCS
#                    bucket can use a broader multi-region (default: REGION)
#   SKIP_IAM_GRANTS  Set to any non-empty value to skip every IAM grant
#                    this script would otherwise make - both the runtime
#                    service account's roles and the INVOKER_EMAIL grant -
#                    e.g. when an admin/devops (or Terraform) already
#                    grants all of it directly. Granting IAM policy is a
#                    separate permission from creating the underlying
#                    resources above, so your own account can lack it even
#                    after those succeed - if unset and that's the case,
#                    each grant fails individually but doesn't abort the
#                    rest of the deploy either way.
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

# Each grant below is a DIFFERENT permission on a DIFFERENT resource
# (project vs. bucket vs. secret vs., later, the Cloud Run service
# itself) - having rights to create the bucket/secret/service account
# above doesn't imply rights to set IAM policy on the project, and vice
# versa. A missing grant is tracked and reported at the end rather than
# aborting the whole deploy - what CAN be granted still gets granted, and
# the service still gets deployed (it just won't work correctly until
# whatever gap is reported gets closed by someone who can).
IAM_GRANT_FAILURES=()
grant_iam() {
  local description="$1"
  shift
  if ! "$@"; then
    IAM_GRANT_FAILURES+=("$description")
    echo "   (continuing - this one needs an admin, see the summary at the end)"
  fi
}

if [ -n "${SKIP_IAM_GRANTS:-}" ]; then
  echo "-- Granting IAM roles -- skipped (SKIP_IAM_GRANTS set - assuming $SA and $INVOKER_EMAIL already have what they need)"
else
  echo "-- Granting IAM roles --"
  grant_iam "roles/bigquery.dataViewer on project $BQ_PROJECT_ID for $SA" \
    gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member="serviceAccount:$SA" --role="roles/bigquery.dataViewer" --condition=None
  grant_iam "roles/bigquery.jobUser on project $BQ_PROJECT_ID for $SA" \
    gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member="serviceAccount:$SA" --role="roles/bigquery.jobUser" --condition=None
  # Vision ships no roles/cloudvision.* predefined role at all - what it
  # actually gates calls on is serviceusage.services.use against the
  # quota project, i.e. this role.
  grant_iam "roles/serviceusage.serviceUsageConsumer on project $PROJECT_ID for $SA" \
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="roles/serviceusage.serviceUsageConsumer" --condition=None
  grant_iam "roles/storage.objectAdmin on gs://$BUCKET_NAME for $SA" \
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
  grant_iam "roles/secretmanager.secretAccessor on secret $SECRET_NAME for $SA" \
    gcloud secrets add-iam-policy-binding "$SECRET_NAME" --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
fi

echo "-- Deploying to Cloud Run --"
gcloud run deploy "$SERVICE_NAME" \
  --source "$SCRIPT_DIR" \
  --region "$REGION" \
  --service-account "$SA" \
  --iap \
  --no-cpu-throttling \
  --max-instances=1 \
  --memory=1Gi \
  --set-env-vars="GCS_CACHE_BUCKET=$BUCKET_NAME" \
  --set-secrets="GOOGLE_MAPS_API_KEY=$SECRET_NAME:latest"

if [ -n "${SKIP_IAM_GRANTS:-}" ]; then
  echo "-- Granting invoker access to $INVOKER_EMAIL -- skipped (SKIP_IAM_GRANTS set)"
else
  echo "-- Granting invoker access to $INVOKER_EMAIL --"
  grant_iam "roles/run.invoker on service $SERVICE_NAME for user:$INVOKER_EMAIL" \
    gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
      --region "$REGION" \
      --member="user:$INVOKER_EMAIL" \
      --role="roles/run.invoker"
fi

cat <<EOF

Deployed. This service is fronted by Identity-Aware Proxy - reach it
through the domain IAP is configured for (e.g. https://archimedes.moove.ai)
and sign in with an authorized Google account, not by visiting the
service's own Cloud Run URL directly.

Logs:

  gcloud run services logs read $SERVICE_NAME --region $REGION
EOF

if [ "${#IAM_GRANT_FAILURES[@]}" -gt 0 ]; then
  echo ""
  echo "NOTE: the account running this script couldn't grant everything below -"
  echo "someone with IAM admin rights on the relevant project/resource needs to:"
  for f in "${IAM_GRANT_FAILURES[@]}"; do
    echo "  - $f"
  done
  echo "Until then, the deployed app may fail on whichever of BigQuery/Vision/the"
  echo "cache bucket/the Maps key secret/invoking the service that grant covers."
fi
