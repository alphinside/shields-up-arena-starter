#!/usr/bin/env bash
# Deletes the cloud resources created by scripts/deploy.sh in this project.
# Safe to rerun: missing resources print an error and the script continues.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

ENV_PROJECT="${GOOGLE_CLOUD_PROJECT:-}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

GOOGLE_CLOUD_PROJECT="${ENV_PROJECT:-${GOOGLE_CLOUD_PROJECT:-}}"

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env or export it}"
PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="${GOOGLE_CLOUD_LOCATION:-${BIGQUERY_LOCATION:-us-central1}}"
TOOLBOX_SERVICE="retail-toolbox"
TOOLBOX_SECRET="retail-toolbox-tools"
TOOLBOX_SA="retail-toolbox-sa@${PROJECT}.iam.gserviceaccount.com"

ENGINE_RESOURCE=""
if [[ -f deployment_metadata.json ]]; then
  ENGINE_RESOURCE=$(uv run python scripts/engine.py resource 2>/dev/null || true)
fi
if [[ -z "$ENGINE_RESOURCE" ]]; then
  agents-cli deploy --status --project="$PROJECT" --no-confirm-project 2>/dev/null || true
  ENGINE_RESOURCE=$(uv run python scripts/engine.py resource 2>/dev/null || true)
fi

if [[ -n "$ENGINE_RESOURCE" ]]; then
  # Remove the Agent Identity's project IAM bindings before deleting the engine.
  # Deleting the engine leaves project bindings behind and hides the principal.
  PRINCIPAL=$(uv run python scripts/engine.py principal 2>/dev/null || true)
  if [[ "$PRINCIPAL" == principal://* ]]; then
    for ROLE in $(gcloud projects get-iam-policy "$PROJECT" --flatten='bindings[].members' \
      --filter="bindings.members:${PRINCIPAL}" --format='value(bindings.role)'); do
      gcloud projects remove-iam-policy-binding "$PROJECT" --member="$PRINCIPAL" \
        --role="$ROLE" --condition=None --quiet >/dev/null &&
        echo "Removed ${ROLE} from ${PRINCIPAL}."
    done
  fi
  echo "Deleting agent runtime instance ${ENGINE_RESOURCE}..."
  uv run python scripts/engine.py delete
  printf '{\n  "remote_agent_runtime_id": null,\n  "deployment_timestamp": null\n}\n' >deployment_metadata.json
fi
rm -f .sandbox-warm

gcloud run services delete "$TOOLBOX_SERVICE" --project="$PROJECT" --region="$REGION" --quiet
gcloud secrets delete "$TOOLBOX_SECRET" --project="$PROJECT" --quiet

# Remove project bindings before deleting the service account so they do not
# remain as deleted:serviceAccount entries.
for ROLE in roles/bigquery.jobUser roles/bigquery.dataEditor; do
  gcloud projects remove-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${TOOLBOX_SA}" --role="$ROLE" \
    --condition=None --quiet >/dev/null
done
gcloud iam service-accounts delete "$TOOLBOX_SA" --project="$PROJECT" --quiet

bq --project_id="$PROJECT" rm -r -f -d harden_retail
