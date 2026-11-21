#!/usr/bin/env bash
# Grants the Shields Up Arena scorer service account read and query access to
# this agent's single agent runtime instance.
#
# Creates or updates a project custom role holding only:
#   - aiplatform.reasoningEngines.get
#   - aiplatform.reasoningEngines.query
# Then merges the binding into the engine's IAM policy using read-modify-write
# with etag so existing bindings (including roles/aiplatform.agentContextEditor)
# stay intact.
set -euo pipefail
cd "$(dirname "$0")/.."

ARG_SCORER="${1:-}"
ENV_SCORER="${SCORER_SERVICE_ACCOUNT:-}"
ENV_PROJECT="${GOOGLE_CLOUD_PROJECT:-}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

SCORER_SERVICE_ACCOUNT="${ARG_SCORER:-${ENV_SCORER:-${SCORER_SERVICE_ACCOUNT:-}}}"
GOOGLE_CLOUD_PROJECT="${ENV_PROJECT:-${GOOGLE_CLOUD_PROJECT:-}}"

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env or export it}"
: "${SCORER_SERVICE_ACCOUNT:?Set SCORER_SERVICE_ACCOUNT in .env or pass it as the first argument}"

PROJECT="$GOOGLE_CLOUD_PROJECT"
ROLE_ID="shieldsUpArenaScorer"
ROLE_NAME="projects/${PROJECT}/roles/${ROLE_ID}"
PERMS="aiplatform.reasoningEngines.get,aiplatform.reasoningEngines.query"
MEMBER="serviceAccount:${SCORER_SERVICE_ACCOUNT}"

ENGINE_RESOURCE=$(uv run python scripts/engine.py resource)
# projects/<project>/locations/<location>/reasoningEngines/<id>
LOCATION=$(printf '%s' "$ENGINE_RESOURCE" | cut -d/ -f4)
API_BASE="https://${LOCATION}-aiplatform.googleapis.com/v1/${ENGINE_RESOURCE}"

echo "Ensuring custom role ${ROLE_NAME}..."
if gcloud iam roles describe "$ROLE_ID" --project="$PROJECT" &>/dev/null; then
  DELETED=$(gcloud iam roles describe "$ROLE_ID" --project="$PROJECT" --format='value(deleted)')
  if [[ "$DELETED" == "True" ]]; then
    gcloud iam roles undelete "$ROLE_ID" --project="$PROJECT" --quiet >/dev/null
  fi
  gcloud iam roles update "$ROLE_ID" --project="$PROJECT" \
    --permissions="$PERMS" --stage=GA --quiet >/dev/null
else
  gcloud iam roles create "$ROLE_ID" --project="$PROJECT" \
    --title="Shields Up Arena Scorer" \
    --description="Allows the Shields Up Arena scorer to inspect and query one agent runtime instance." \
    --permissions="$PERMS" --stage=GA --quiet >/dev/null
fi

echo "Binding ${MEMBER} on ${ENGINE_RESOURCE}..."
# Retry briefly in case a newly created custom role is still propagating.
for attempt in 1 2 3 4; do
  ACCESS_TOKEN=$(gcloud auth print-access-token)
  CURRENT_POLICY=$(curl -fsS -X POST "${API_BASE}:getIamPolicy" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{}')

  PAYLOAD=$(python3 -c '
import json
import sys

role_name, member = sys.argv[1], sys.argv[2]
policy = json.load(sys.stdin)
if not isinstance(policy, dict) or not policy.get("etag"):
    sys.exit("Refusing to call :setIamPolicy without an etag from :getIamPolicy.")

bindings = list(policy.get("bindings") or [])
updated = False
for binding in bindings:
    if binding.get("role") == role_name and not binding.get("condition"):
        members = list(binding.get("members") or [])
        if member in members:
            print("ALREADY_BOUND")
            sys.exit(0)
        members.append(member)
        binding["members"] = members
        updated = True
        break

if not updated:
    bindings.append({"role": role_name, "members": [member]})

policy["bindings"] = bindings
print(json.dumps({"policy": policy}))
' "$ROLE_NAME" "$MEMBER" <<<"$CURRENT_POLICY")

  if [[ "$PAYLOAD" == "ALREADY_BOUND" ]]; then
    echo "Scorer binding already present on ${ENGINE_RESOURCE}."
    exit 0
  fi

  if curl -fsS -X POST "${API_BASE}:setIamPolicy" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" >/dev/null; then
    echo "Granted ${ROLE_NAME} to ${MEMBER} on ${ENGINE_RESOURCE}."
    echo "IAM changes can take 2 to 3 minutes to apply before the scorer can reach the engine."
    exit 0
  fi

  if [[ "$attempt" -lt 4 ]]; then
    echo "Retrying :setIamPolicy (attempt ${attempt}/4)..." >&2
    sleep 10
  fi
done

echo "Failed to update IAM policy on ${ENGINE_RESOURCE}." >&2
exit 1
