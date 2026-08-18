#!/bin/bash
set -e

echo "=== Post-provision: Public Health RFP POC ==="

# ── Resolve AZD environment variables ────────────────────────────────────────
ACCOUNT=$(azd env get-value AZURE_STORAGE_ACCOUNT)
SEARCH_ENDPOINT=$(azd env get-value AZURE_SEARCH_ENDPOINT)
OPENAI_ENDPOINT=$(azd env get-value AZURE_OPENAI_ENDPOINT)
APPINSIGHTS_CONN=$(azd env get-value APPLICATIONINSIGHTS_CONNECTION_STRING)
FOUNDRY_PROJECT=$(azd env get-value AZURE_AI_FOUNDRY_PROJECT_NAME 2>/dev/null || echo "")
CONTAINER="rfp-corpus"

echo ""
echo "[1/6] Uploading sample RFPs to blob storage: ${ACCOUNT}/${CONTAINER}"
if [ -d "data/sample-rfps" ]; then
  az storage blob upload-batch \
    --account-name "$ACCOUNT" \
    --destination "$CONTAINER" \
    --source "data/sample-rfps" \
    --pattern "*.md" \
    --auth-mode key \
    --overwrite \
    --output none
else
  echo "      ⚠ data/sample-rfps not found — skipping (add .md files there to populate)"
fi

echo "[2/6] Uploading eval examples to golden-dataset container"
if [ -d "data/eval-examples" ]; then
  az storage blob upload-batch \
    --account-name "$ACCOUNT" \
    --destination "golden-dataset" \
    --source "data/eval-examples" \
    --pattern "*.json" \
    --auth-mode key \
    --overwrite \
    --output none
else
  echo "      ⚠ data/eval-examples not found — skipping (add .json files there to populate)"
fi

echo "[3/6] Creating AI Search index and running ingestion pipeline"
if [ -f "src/ingestion/create_index.py" ]; then
  export AZURE_SEARCH_ENDPOINT="$SEARCH_ENDPOINT"
  export AZURE_OPENAI_ENDPOINT="$OPENAI_ENDPOINT"
  export AZURE_STORAGE_ACCOUNT="$ACCOUNT"

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
  pip3 install --target /tmp/pubhealth-ingest-deps -r src/ingestion/requirements.txt -q 2>/dev/null || \
    pip3 install --target /tmp/pubhealth-ingest-deps -r src/ingestion/requirements.txt -q
  PYTHONPATH="/tmp/pubhealth-ingest-deps:$PWD/src/ingestion" python3 src/ingestion/create_index.py
  # pipeline.py imports local siblings (document_parser, chunker, indexer) via PYTHONPATH
  PYTHONPATH="/tmp/pubhealth-ingest-deps:$PWD/src/ingestion" python3 src/ingestion/pipeline.py
else
  echo "      ⚠ src/ingestion/create_index.py not found — skipping ingestion"
fi

echo "[4/6] Setting up AI Foundry connections"
# Known project endpoint (eastus hub: mlw-pubhealth-hub-2gdlihbjsb5rk)
FOUNDRY_ENDPOINT=$(azd env get-value AZURE_AI_FOUNDRY_PROJECT_ENDPOINT 2>/dev/null || echo "https://eastus.api.azureml.ms")
FOUNDRY_PROJECT_NAME=$(azd env get-value AZURE_AI_FOUNDRY_PROJECT_NAME 2>/dev/null || echo "mlw-pubhealth-rfp-2gdlihbjsb5rk")
FOUNDRY_HUB=$(azd env get-value AZURE_AI_FOUNDRY_HUB_NAME 2>/dev/null || echo "mlw-pubhealth-hub-2gdlihbjsb5rk")
azd env set AZURE_AI_FOUNDRY_PROJECT_ENDPOINT "$FOUNDRY_ENDPOINT"
azd env set AZURE_AI_FOUNDRY_PROJECT_NAME "$FOUNDRY_PROJECT_NAME"
azd env set AZURE_AI_FOUNDRY_HUB_NAME "$FOUNDRY_HUB"
echo "      ✓ AI Foundry vars set: $FOUNDRY_PROJECT_NAME @ $FOUNDRY_ENDPOINT"

echo "[5/6] Fabric setup"
FABRIC_WORKSPACE=$(azd env get-value FABRIC_WORKSPACE_ID 2>/dev/null || echo "")
if [ -n "$FABRIC_WORKSPACE" ]; then
  echo "      Fabric workspace already provisioned: $FABRIC_WORKSPACE"
else
  echo "      ℹ Fabric not provisioned yet."
  echo "      To provision, run:"
  echo "        python fabric/setup.py \\"
  echo "          --workspace-name pubhealth-rfp-poc \\"
  echo "          --ai-search-endpoint \$AZURE_SEARCH_ENDPOINT \\"
  echo "          --sharepoint-site-id <YOUR_SITE_ID>"
fi

echo "[6/6] Writing .env file from AZD environment"
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

echo "[7/7] Restoring bot App ID on API container (protects against Bicep reset)"
# azd provision may recreate the container app, resetting MICROSOFT_APP_ID to the old value.
# Always enforce the correct App Registration (pubhealth-rfp-bot-v2: 26b9c245).
BOT_APP_ID="26b9c245-880d-458b-9edf-809c1a7f534a"
RESOURCE_GROUP_NAME=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || echo "")
if [ -n "$RESOURCE_GROUP_NAME" ]; then
  API_APP_NAME=$(az containerapp list \
    --resource-group "$RESOURCE_GROUP_NAME" \
    --query "[?tags.\"azd-service-name\"=='api'].name | [0]" \
    -o tsv 2>/dev/null || echo "")
  if [ -n "$API_APP_NAME" ]; then
    CURRENT_IMAGE=$(az containerapp show -n "$API_APP_NAME" -g "$RESOURCE_GROUP_NAME" \
      --query "properties.template.containers[0].image" -o tsv 2>/dev/null || echo "")
    if [ -n "$CURRENT_IMAGE" ]; then
      az containerapp update \
        --name "$API_APP_NAME" \
        --resource-group "$RESOURCE_GROUP_NAME" \
        --image "$CURRENT_IMAGE" \
        --set-env-vars "MICROSOFT_APP_ID=$BOT_APP_ID" \
        --revision-suffix "botfix-$(date +%s)" \
        --output none 2>/dev/null && echo "  ✓ MICROSOFT_APP_ID=$BOT_APP_ID set on $API_APP_NAME"
    fi
  fi
else
  echo "  ⚠ AZURE_RESOURCE_GROUP not set — skipping bot App ID restore"
fi

echo ""
echo "=== Post-provision complete ==="
echo ""
echo "Next steps:"
echo "  1. Run Fabric provisioning:  python fabric/setup.py ..."
echo "  2. Start orchestrator:       cd src/orchestrator && dotnet run"
echo "  3. Start API:                cd src/api && uvicorn main:app --reload"
echo "  4. Run tests:                cd tests && pip install -r requirements-test.txt && pytest -v"
