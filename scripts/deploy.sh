#!/usr/bin/env bash
# Deploys the starter agent to agent runtime with Agent Identity and a private
# Cloud Run MCP Toolbox service.
#
# Safe to rerun: updates the Toolbox service and agent runtime instance in place
# and keeps the same Agent Identity principal.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
ENV_SCORER_SET="${SCORER_SERVICE_ACCOUNT+1}"
ENV_SCORER="${SCORER_SERVICE_ACCOUNT-}"
DEFAULT_SCORER_SA="red-teamer-sa@alvin-exploratory-2.iam.gserviceaccount.com"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

GOOGLE_CLOUD_PROJECT="${ENV_PROJECT:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -n "$ENV_SCORER_SET" ]]; then
  SCORER_SERVICE_ACCOUNT="$ENV_SCORER"
else
  SCORER_SERVICE_ACCOUNT="${SCORER_SERVICE_ACCOUNT-$DEFAULT_SCORER_SA}"
fi
export GOOGLE_CLOUD_PROJECT SCORER_SERVICE_ACCOUNT

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env or export it}"
if [[ "$GOOGLE_CLOUD_PROJECT" == "your-gcp-project-id" || "$GOOGLE_CLOUD_PROJECT" == "YOUR_GCP_PROJECT_ID" ]]; then
  echo "Replace GOOGLE_CLOUD_PROJECT in .env with your real Google Cloud project ID." >&2
  exit 1
fi
PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="${GOOGLE_CLOUD_LOCATION:-${BIGQUERY_LOCATION:-us-central1}}"

TOOLBOX_SERVICE="retail-toolbox"
TOOLBOX_SECRET="retail-toolbox-tools"
TOOLBOX_SA="retail-toolbox-sa@${PROJECT}.iam.gserviceaccount.com"
TOOLBOX_IMAGE="us-central1-docker.pkg.dev/database-toolbox/toolbox/toolbox:1.12.0"

# A new service account can take a moment before IAM accepts it in a binding.
retry() { for _ in 1 2 3 4 5 6; do "$@" && return; sleep 10; done; return 1; }

echo "[1/6] Checking Agents CLI version..."
version=$(agents-cli --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)
IFS=. read -r major minor _ <<< "${version:-0.0.0}"
if [[ "$((10#${major:-0}))" -ne 1 || "$((10#${minor:-0}))" -lt 12 ]]; then
  echo "This repo needs Agents CLI >= 1.12.0 (< 2.0.0); this shell runs '${version:-none}'." >&2
  echo "Install or upgrade it with:" >&2
  echo '  uv tool install --upgrade google-agents-cli && uv tool update-shell && export PATH="$HOME/.local/bin:$PATH"' >&2
  exit 1
fi
echo "  agents-cli ${version} (ignore any 'scaffold upgrade' notice during deploy)"
uv sync --quiet
uv run python scripts/check_contract.py

if [[ -z "${SKIP_SETUP:-}" ]]; then
  echo "[2/6] Enabling required APIs..."
  gcloud services enable \
    aiplatform.googleapis.com \
    bigquery.googleapis.com \
    cloudresourcemanager.googleapis.com \
    cloudtrace.googleapis.com \
    iam.googleapis.com \
    logging.googleapis.com \
    run.googleapis.com \
    secretmanager.googleapis.com \
    telemetry.googleapis.com \
    --project="$PROJECT"
else
  echo "[2/6] Skipping API enablement (SKIP_SETUP is set)."
fi

if [[ -z "${SKIP_SEED:-}" ]]; then
  echo "[3/6] Loading synthetic retail dataset into BigQuery..."
  uv run python scripts/load_data.py --project="$PROJECT" --location="$REGION"
else
  echo "[3/6] Skipping BigQuery seed (SKIP_SEED is set)."
fi

echo "[4/6] Deploying private MCP Toolbox on Cloud Run (${TOOLBOX_SERVICE})..."
gcloud iam service-accounts describe "$TOOLBOX_SA" --project="$PROJECT" &>/dev/null ||
  gcloud iam service-accounts create retail-toolbox-sa --project="$PROJECT" \
    --display-name="MCP Toolbox for retail analytics agent" >/dev/null

for ROLE in roles/bigquery.jobUser roles/bigquery.dataEditor; do
  retry gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${TOOLBOX_SA}" --role="$ROLE" \
    --condition=None --quiet >/dev/null
done

created_secret=0
if gcloud secrets describe "$TOOLBOX_SECRET" --project="$PROJECT" &>/dev/null; then
  gcloud secrets versions add "$TOOLBOX_SECRET" --project="$PROJECT" \
    --data-file=mcp-toolbox/tools.yaml >/dev/null
else
  gcloud secrets create "$TOOLBOX_SECRET" --project="$PROJECT" \
    --replication-policy=automatic --data-file=mcp-toolbox/tools.yaml >/dev/null
  created_secret=1
fi

retry gcloud secrets add-iam-policy-binding "$TOOLBOX_SECRET" --project="$PROJECT" \
  --member="serviceAccount:${TOOLBOX_SA}" --role=roles/secretmanager.secretAccessor \
  --condition=None --quiet >/dev/null

# Newly created secrets, service accounts, and Cloud Run service agents can
# take a few seconds before Cloud Run's secret preflight sees IAM propagation.
if [[ "$created_secret" -eq 1 ]]; then
  sleep 5
fi

retry gcloud run deploy "$TOOLBOX_SERVICE" --project="$PROJECT" --region="$REGION" \
  --image="$TOOLBOX_IMAGE" \
  --service-account="$TOOLBOX_SA" \
  --no-allow-unauthenticated \
  --set-secrets="/app/tools.yaml=${TOOLBOX_SECRET}:latest" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},BIGQUERY_LOCATION=${REGION}" \
  --args="--config=/app/tools.yaml,--address=0.0.0.0,--port=8080" \
  --quiet

TOOLBOX_CLOUD_URL=$(gcloud run services describe "$TOOLBOX_SERVICE" \
  --project="$PROJECT" --region="$REGION" --format='value(status.url)')
echo "  Toolbox URL: ${TOOLBOX_CLOUD_URL}"

echo "[5/6] Deploying agent to agent runtime with Agent Identity..."
# agents-cli resets the two OTel content capture variables on every deploy, so
# pass all three trace variables and TOOLBOX_URL each time.
agents-cli deploy --agent-identity --project="$PROJECT" --no-confirm-project \
  --update-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},TOOLBOX_URL=${TOOLBOX_CLOUD_URL},GOOGLE_CLOUD_LOCATION=${REGION},BIGQUERY_LOCATION=${REGION},GOOGLE_GENAI_USE_ENTERPRISE=true,GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true,OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true,OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT"

PRINCIPAL=$(uv run python scripts/engine.py principal)
echo "  Agent Identity: ${PRINCIPAL}"
retry gcloud run services add-iam-policy-binding "$TOOLBOX_SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --member="$PRINCIPAL" --role=roles/run.invoker --quiet >/dev/null

echo "[6/6] Granting scorer access and warming code sandbox..."
if [[ -n "${SCORER_SERVICE_ACCOUNT:-}" ]]; then
  scripts/grant-scorer.sh
else
  echo "WARNING: SCORER_SERVICE_ACCOUNT is not set. Skipped scorer IAM binding." >&2
  echo "Set SCORER_SERVICE_ACCOUNT in .env and run scripts/grant-scorer.sh before submitting." >&2
fi

if [[ ! -f .sandbox-warm ]]; then
  echo "  Warming one code sandbox (about 2 minutes the first time in a project)..."
  uv run python scripts/engine.py warm
  touch .sandbox-warm
fi

ENGINE_RESOURCE=$(uv run python scripts/engine.py resource)
echo
echo "Deploy complete. Paste this resource name into https://g.dev/cloud/kdd-q4-26-hack :"
echo "  ${ENGINE_RESOURCE}"
echo
echo "If this is the first deploy, wait 2 to 3 minutes for IAM bindings to propagate."
