#!/bin/bash
# Deliberately no `set -e`: this script's steps (data uploads, search
# ingestion, AI Foundry/Fabric info, bot identity check, .env write) are
# independent of each other and of the deploy phase that follows. A single
# flaky step (e.g. a storage propagation delay) must not skip the rest —
# in particular the bot identity check (step 6) and .env write (step 9)
# are cheap and important even if ingestion upstream failed. Each risky
# step below tracks its own failure in $FAILED; the script exits non-zero
# at the end if anything failed, but always runs every step first.
FAILED=0

echo "=== Post-provision: Public Health RFP POC ==="

source "$(dirname "${BASH_SOURCE[0]}")/lib-bot-identity.sh"

# Bicep reports the storage account (and its containers) as "Succeeded" before
# they're reliably queryable on the data plane — a brand-new storage account's
# containers can 404 on `upload-batch` for several minutes after ARM says
# they exist. Poll until the container is actually visible instead of
# guessing a fixed sleep.
wait_for_container() {
  local account="$1" container="$2" attempt
  for attempt in $(seq 1 20); do
    if az storage container show --account-name "$account" --name "$container" \
      --auth-mode key --output none 2>/dev/null; then
      return 0
    fi
    echo "      · Container '$container' not yet visible on $account (attempt $attempt/20) — waiting 15s..."
    sleep 15
  done
  echo "      ✗ Container '$container' never became visible after 5 minutes."
  return 1
}

# ── Resolve AZD environment variables ────────────────────────────────────────
ACCOUNT=$(azd env get-value AZURE_STORAGE_ACCOUNT)
SEARCH_ENDPOINT=$(azd env get-value AZURE_SEARCH_ENDPOINT)
OPENAI_ENDPOINT=$(azd env get-value AZURE_OPENAI_ENDPOINT)
APPINSIGHTS_CONN=$(azd env get-value APPLICATIONINSIGHTS_CONNECTION_STRING)
CONTAINER="rfp-corpus"

echo ""
echo "[1/9] Uploading sample RFPs to blob storage: ${ACCOUNT}/${CONTAINER}"
if [ -d "data/sample-rfps" ]; then
  if wait_for_container "$ACCOUNT" "$CONTAINER"; then
    az storage blob upload-batch \
      --account-name "$ACCOUNT" \
      --destination "$CONTAINER" \
      --source "data/sample-rfps" \
      --pattern "*.md" \
      --auth-mode key \
      --overwrite \
      --output none || { echo "      ✗ Sample RFP upload failed"; FAILED=1; }
  else
    FAILED=1
  fi
else
  echo "      ⚠ data/sample-rfps not found — skipping (add .md files there to populate)"
fi

echo "[2/9] Uploading eval examples to golden-dataset container"
if [ -d "data/eval-examples" ]; then
  if wait_for_container "$ACCOUNT" "golden-dataset"; then
    az storage blob upload-batch \
      --account-name "$ACCOUNT" \
      --destination "golden-dataset" \
      --source "data/eval-examples" \
      --pattern "*.json" \
      --auth-mode key \
      --overwrite \
      --output none || { echo "      ✗ Eval example upload failed"; FAILED=1; }
  else
    FAILED=1
  fi
else
  echo "      ⚠ data/eval-examples not found — skipping (add .json files there to populate)"
fi

echo "[3/9] Creating AI Search index and running ingestion pipeline"
if [ -f "src/ingestion/create_index.py" ]; then
  export AZURE_SEARCH_ENDPOINT="$SEARCH_ENDPOINT"
  export AZURE_OPENAI_ENDPOINT="$OPENAI_ENDPOINT"
  export AZURE_STORAGE_ACCOUNT="$ACCOUNT"
  # Without this, indexer.py falls back to its own hardcoded default, which
  # has drifted from the model Bicep actually deploys — see indexer.py.
  export AZURE_OPENAI_EMBEDDING_DEPLOYMENT="$(azd env get-value AZURE_OPENAI_EMBEDDING_DEPLOYMENT 2>/dev/null || echo text-embedding-3-small)"

  # CLI user lacks data-plane RBAC on Search and OpenAI — fetch keys via ARM
  RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP)
  SEARCH_SERVICE=$(echo "$SEARCH_ENDPOINT" | sed 's|https://||' | cut -d'.' -f1)
  OPENAI_RESOURCE=$(echo "$OPENAI_ENDPOINT" | sed 's|https://||' | cut -d'.' -f1)
  export AZURE_SEARCH_ADMIN_KEY=$(az search admin-key show \
    --resource-group "$RESOURCE_GROUP" \
    --service-name "$SEARCH_SERVICE" \
    --query primaryKey -o tsv)
  export AZURE_OPENAI_API_KEY=$(az cognitiveservices account keys list \
    --resource-group "$RESOURCE_GROUP" \
    --name "$OPENAI_RESOURCE" \
    --query key1 -o tsv)
  export AZURE_STORAGE_KEY=$(az storage account keys list \
    --resource-group "$RESOURCE_GROUP" \
    --account-name "$ACCOUNT" \
    --query "[0].value" -o tsv)

  echo "      Installing ingestion dependencies..."
  # --target avoids needing python3-venv and bypasses externally-managed-environment
  if pip3 install --target /tmp/pubhealth-ingest-deps -r src/ingestion/requirements.txt -q 2>/dev/null \
    || pip3 install --target /tmp/pubhealth-ingest-deps -r src/ingestion/requirements.txt -q; then
    if PYTHONPATH="/tmp/pubhealth-ingest-deps:$PWD/src/ingestion" python3 src/ingestion/create_index.py; then
      # pipeline.py imports local siblings (document_parser, chunker, indexer) via PYTHONPATH
      PYTHONPATH="/tmp/pubhealth-ingest-deps:$PWD/src/ingestion" python3 src/ingestion/pipeline.py \
        || { echo "      ✗ Ingestion pipeline failed"; FAILED=1; }
    else
      echo "      ✗ Search index creation failed"
      FAILED=1
    fi
  else
    echo "      ✗ Failed to install ingestion dependencies"
    FAILED=1
  fi
else
  echo "      ⚠ src/ingestion/create_index.py not found — skipping ingestion"
fi

echo "[4/9] AI Foundry"
# Unified AIServices account + project (infra/modules/foundry.bicep) — the
# account name/endpoint and the AI Search connection are both declared
# natively in Bicep now (Microsoft.CognitiveServices/accounts/connections),
# not scripted here. Nothing to provision in this step beyond reporting.
FOUNDRY_ENDPOINT=$(azd env get-value AZURE_AI_FOUNDRY_PROJECT_ENDPOINT)
FOUNDRY_PROJECT_NAME=$(azd env get-value AZURE_AI_FOUNDRY_PROJECT_NAME)
echo "      ✓ AI Foundry vars: $FOUNDRY_PROJECT_NAME @ $FOUNDRY_ENDPOINT"

echo "[5/9] Fabric setup"
# fabric/setup.py's provision() is idempotent (finds-then-creates the
# workspace/lakehouse/connection/CopyJob by name, and re-grants the current
# managed identity's workspace role every time) so it's safe to run on every
# azd provision, not just once. This also self-heals two failure modes that
# used to require manual intervention: a torn-down-and-rebuilt resource
# group orphans both the Fabric workspace (Bicep doesn't manage it) and the
# shared managed identity's role grant on it (the identity's principalId
# rotates even when the workspace survives). Gated on SHAREPOINT_SITE_ID
# being set, same as SharePoint re-wiring in step 8 below -- Fabric
# ingestion needs a SharePoint site chosen first (install.sh Phase 6).
#
# The one step that stays interactive when it's actually needed: granting
# site-level SharePoint access requires a delegated, human-signed-in Graph
# token (Microsoft rejects app-only tokens for this specific endpoint,
# regardless of what permissions they hold) -- provision() only runs that
# device-code flow when the current workspace identity doesn't match
# FABRIC_SITE_GRANT_APP_ID from a previous successful run, so ordinary
# re-provisions (workspace unchanged) don't prompt for anything.
SP_SITE_ID_FOR_FABRIC=$(azd env get-value SHAREPOINT_SITE_ID 2>/dev/null || echo "")
if [ -n "$SP_SITE_ID_FOR_FABRIC" ]; then
  TENANT_ID=$(azd env get-value AZURE_TENANT_ID 2>/dev/null || echo "")
  RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || echo "")
  MI_OID_FOR_FABRIC=$(az identity list -g "$RESOURCE_GROUP" --query "[0].principalId" -o tsv 2>/dev/null || echo "")
  LAST_GRANTED_APP_ID=$(azd env get-value FABRIC_SITE_GRANT_APP_ID 2>/dev/null || echo "")

  if pip3 install --target /tmp/pubhealth-fabric-deps -r fabric/requirements.txt -q 2>/dev/null \
    || pip3 install --target /tmp/pubhealth-fabric-deps -r fabric/requirements.txt -q; then
    if PYTHONPATH="/tmp/pubhealth-fabric-deps" python3 fabric/setup.py \
      --sharepoint-site-id "$SP_SITE_ID_FOR_FABRIC" \
      --tenant-id "$TENANT_ID" \
      --api-managed-identity-principal-id "$MI_OID_FOR_FABRIC" \
      --last-granted-app-id "$LAST_GRANTED_APP_ID" \
      --set-azd-env; then
      FABRIC_WORKSPACE=$(azd env get-value FABRIC_WORKSPACE_ID 2>/dev/null || echo "")
      FABRIC_LAKEHOUSE=$(azd env get-value FABRIC_LAKEHOUSE_ID 2>/dev/null || echo "")
      API_APP_NAME=$(az containerapp list -g "$RESOURCE_GROUP" \
        --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || echo "")
      # Deliberately NOT passing --image here even though we have it available —
      # `az containerapp update --set-env-vars --image <same image>` has been
      # observed live to revert OTHER env vars (e.g. MONTHLY_BUDGET_USD) to a
      # stale prior value not present in any config file, most likely an ARM
      # read-after-write race in how that combination rebuilds the revision
      # template right after a Bicep deployment. --set-env-vars alone (no
      # --image) reliably preserves the rest of the current template.
      if [ -n "$API_APP_NAME" ]; then
        az containerapp update -n "$API_APP_NAME" -g "$RESOURCE_GROUP" \
          --set-env-vars "FABRIC_WORKSPACE_ID=${FABRIC_WORKSPACE}" "FABRIC_LAKEHOUSE_ID=${FABRIC_LAKEHOUSE}" \
          --revision-suffix "fabricinit$(date +%s)" \
          --output none \
          && echo "      ✓ Fabric env vars set on API container: $FABRIC_WORKSPACE" \
          || { echo "      ✗ Failed to set Fabric env vars on API container"; FAILED=1; }
      else
        echo "      ✗ Could not resolve API container app — skipping Fabric env vars"
        FAILED=1
      fi
    else
      echo "      ⚠ Fabric provisioning failed (non-fatal — e.g. no active trial capacity)."
      echo "        Draft/eval writes to OneLake will no-op until this is resolved manually."
    fi
  else
    echo "      ✗ Failed to install fabric/requirements.txt — skipping Fabric setup"
    FAILED=1
  fi
else
  echo "      ℹ SHAREPOINT_SITE_ID not set yet — nothing to provision (run Phase 6 of install.sh first)"
fi

echo "[6/9] Verifying Teams bot identity (App Registration + Service Principal)"
BOT_APP_ID_CHECK=$(azd env get-value BOT_APP_ID 2>/dev/null || echo "")
if [ -n "$BOT_APP_ID_CHECK" ]; then
  if bot_identity_ensure "$BOT_APP_ID_CHECK"; then
    echo "      ✓ Bot App Registration + Service Principal confirmed"
  else
    echo "      ✗ Bot identity is broken — Teams bot will not receive messages."
    echo "        Fix with: bash scripts/install.sh --from 2"
  fi
else
  echo "      · BOT_APP_ID not set yet — nothing to verify (run Phase 2 of install.sh)"
fi

echo "[7/9] Syncing Teams app manifest to the live API endpoint"
# teams-app/manifest.json is a tracked file, not an azd-env value -- but its
# validDomains and Draft Preview static tab both hard-code the API
# container's FQDN, which changes on region moves, resource-group
# recreation, or (with a custom domain not yet in use here) basically any
# re-provision. Left stale, the Teams app silently 403s the static tab and
# fails manifest validation on upload -- hit this exact staleness after
# today's eastus -> South Central US move (bot ID survived a soft-delete
# restore; the FQDN did not survive at all). Only rewrites the fields that
# are genuinely environment-derived (id/botId, validDomains, static tab
# contentUrl) -- name/description/icons/commandLists/accentColor are left
# alone, so this can't clobber a manual content edit. Gated on
# TEAMS_APP_DEVELOPER_NAME being cached, since that's only set once,
# interactively, by install.sh Phase 5 -- first-time setup still goes
# through that, same pattern as SharePoint/Fabric below.
DEV_NAME=$(azd env get-value TEAMS_APP_DEVELOPER_NAME 2>/dev/null || echo "")
if [ -n "$BOT_APP_ID_CHECK" ] && [ -n "$DEV_NAME" ]; then
  DEV_URL=$(azd env get-value TEAMS_APP_DEVELOPER_URL 2>/dev/null || echo "")
  # Resolved independently rather than reusing an earlier step's
  # $RESOURCE_GROUP -- that assignment only happens if the ingestion (step 3)
  # or Fabric (step 5) branch actually runs, so it can't be relied on here.
  MANIFEST_RG=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || echo "")
  MANIFEST_API_APP=$(az containerapp list -g "$MANIFEST_RG" \
    --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || echo "")
  MANIFEST_FQDN=$(az containerapp show -n "$MANIFEST_API_APP" -g "$MANIFEST_RG" \
    --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null || echo "")
  if [ -n "$MANIFEST_FQDN" ]; then
    APP_ID="$BOT_APP_ID_CHECK" FQDN="$MANIFEST_FQDN" DEV_NAME="$DEV_NAME" DEV_URL="$DEV_URL" python3 - <<'PYEOF'
import json, os
with open('teams-app/manifest.json') as f:
    d = json.load(f)
d['id'] = os.environ['APP_ID']
if d.get('bots'):
    d['bots'][0]['botId'] = os.environ['APP_ID']
fqdn = os.environ['FQDN']
d['validDomains'] = [fqdn]
if d.get('staticTabs'):
    d['staticTabs'][0]['contentUrl'] = f'https://{fqdn}/drafts/latest/view'
d['developer']['name'] = os.environ['DEV_NAME']
dev_url = os.environ.get('DEV_URL', '')
if dev_url:
    d['developer']['websiteUrl'] = dev_url
    d['developer']['privacyUrl'] = dev_url
    d['developer']['termsOfUseUrl'] = dev_url
with open('teams-app/manifest.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
PYEOF
    if [ -f "teams-app/color.png" ] && [ -f "teams-app/outline.png" ]; then
      (cd teams-app && zip -j pubhealth-rfp-bot.zip manifest.json color.png outline.png -q) \
        && echo "      ✓ manifest.json synced to $MANIFEST_FQDN, pubhealth-rfp-bot.zip rebuilt" \
        || echo "      ✗ manifest.json synced but zip rebuild failed — rebuild manually"
    else
      echo "      ✓ manifest.json synced to $MANIFEST_FQDN (icons missing — zip not rebuilt)"
    fi
  else
    echo "      ✗ Could not resolve API container FQDN — skipping manifest sync"
  fi
else
  echo "      · Skipped (BOT_APP_ID or TEAMS_APP_DEVELOPER_NAME not set yet — run Phase 5 of install.sh once)"
fi

echo "[8/9] Re-wiring SharePoint (managed identity role + container env vars)"
# SharePoint access is NOT provisioned by Bicep at all — it only ever gets
# wired up by install.sh Phase 6, which sets SHAREPOINT_SITE_ID in azd env
# and assigns Sites.ReadWrite.All to the API's managed identity. A fresh
# teardown + re-provision creates a brand-new managed identity every time,
# so even with SHAREPOINT_SITE_ID still cached in azd env, the role
# assignment and container env vars are gone until this runs again — hit
# this exact gap on 2026-08-19 (generate-and-evaluate silently returned
# sharepoint_url: null with write_to_sharepoint: true, no error anywhere).
# Only runs automatically when SHAREPOINT_SITE_ID is already cached — first-time
# interactive setup (choosing a site/library) still goes through install.sh Phase 6.
SP_SITE_ID=$(azd env get-value SHAREPOINT_SITE_ID 2>/dev/null || echo "")
if [ -n "$SP_SITE_ID" ]; then
  SP_LIBRARY=$(azd env get-value SHAREPOINT_DRAFT_LIBRARY 2>/dev/null || echo "Shared Documents")
  # Resolved independently rather than reusing step 3's $RESOURCE_GROUP —
  # that assignment only happens if the ingestion branch runs.
  RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || echo "")
  MI_OID=$(az identity list -g "$RESOURCE_GROUP" --query "[0].principalId" -o tsv 2>/dev/null || echo "")
  GRAPH_SP=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv 2>/dev/null || echo "")
  if [ -n "$MI_OID" ] && [ -n "$GRAPH_SP" ]; then
    az rest --method POST \
      --url "https://graph.microsoft.com/v1.0/servicePrincipals/${MI_OID}/appRoleAssignments" \
      --body "{\"principalId\":\"${MI_OID}\",\"resourceId\":\"${GRAPH_SP}\",\"appRoleId\":\"9492366f-7969-46a4-8d15-ed1a20078fff\"}" \
      --output none 2>/dev/null \
      && echo "      ✓ Sites.ReadWrite.All assigned to managed identity" \
      || echo "      · Role assignment returned an error — likely already assigned, continuing"

    API_APP_NAME=$(az containerapp list -g "$RESOURCE_GROUP" \
      --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || echo "")
    # Deliberately NOT passing --image (see the identical comment on the
    # Fabric step above) -- combining it with --set-env-vars has been
    # observed to revert other env vars to a stale value.
    if [ -n "$API_APP_NAME" ]; then
      az containerapp update -n "$API_APP_NAME" -g "$RESOURCE_GROUP" \
        --set-env-vars "SHAREPOINT_SITE_ID=${SP_SITE_ID}" "SHAREPOINT_DRAFT_LIBRARY=${SP_LIBRARY}" \
        --revision-suffix "spinit$(date +%s)" \
        --output none \
        && echo "      ✓ SharePoint env vars set on API container" \
        || { echo "      ✗ Failed to set SharePoint env vars on API container"; FAILED=1; }
    else
      echo "      ✗ Could not resolve API container app — skipping SharePoint env vars"
      FAILED=1
    fi
  else
    echo "      ✗ Could not resolve managed identity or Graph service principal — skipping SharePoint wiring"
    FAILED=1
  fi
else
  echo "      · SHAREPOINT_SITE_ID not set — skipping (run: bash scripts/install.sh --from 6)"
fi

echo "[9/9] Writing .env file from AZD environment"
cat > .env << EOF
AZURE_SEARCH_ENDPOINT=${SEARCH_ENDPOINT}
AZURE_OPENAI_ENDPOINT=${OPENAI_ENDPOINT}
AZURE_OPENAI_GPT_DEPLOYMENT=gpt-4o
AZURE_OPENAI_MINI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_O3_DEPLOYMENT=o3-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
APPLICATIONINSIGHTS_CONNECTION_STRING=${APPINSIGHTS_CONN}
ORCHESTRATOR_URL=http://localhost:5001
MONTHLY_BUDGET_USD=500
BUDGET_WARN_THRESHOLD=0.80
BUDGET_CRITICAL_THRESHOLD=0.95
EOF
echo "      .env written (do not commit — already in .gitignore)"

echo ""
if [ "$FAILED" -eq 1 ]; then
  echo "=== Post-provision completed WITH FAILURES (see ✗ above) ==="
  echo "    Re-run this script to retry — every step here is safe to re-run."
else
  echo "=== Post-provision complete ==="
fi
echo ""
echo "Next steps:"
echo "  1. Start orchestrator:  cd src/orchestrator && dotnet run"
echo "  2. Start API:           cd src/api && uvicorn main:app --reload"
echo "  3. Run tests:           cd tests && pip install -r requirements-test.txt && pytest -v"
echo ""
echo "(Fabric provisioning runs automatically above in step [5/8] once SHAREPOINT_SITE_ID is set --"
echo " no separate fabric/setup.py invocation needed.)"

exit "$FAILED"
