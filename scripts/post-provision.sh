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

  echo "      Installing ingestion dependencies..."
  # --target avoids needing python3-venv and bypasses externally-managed-environment
  pip3 install --target /tmp/aphl-ingest-deps -r src/ingestion/requirements.txt -q 2>/dev/null || \
    pip3 install --target /tmp/aphl-ingest-deps -r src/ingestion/requirements.txt -q
  PYTHONPATH="/tmp/aphl-ingest-deps:$PWD/src/ingestion" python3 src/ingestion/create_index.py
  # pipeline.py imports local siblings (document_parser, chunker, indexer) via PYTHONPATH
  PYTHONPATH="/tmp/aphl-ingest-deps:$PWD/src/ingestion" python3 src/ingestion/pipeline.py
else
  echo "      ⚠ src/ingestion/create_index.py not found — skipping ingestion"
fi

echo "[4/6] Setting up AI Foundry connections"
if [ -n "$FOUNDRY_PROJECT" ]; then
  echo "      AI Foundry project: $FOUNDRY_PROJECT"
  # Export for downstream services
  azd env set AZURE_AI_FOUNDRY_PROJECT_ENDPOINT "$(azd env get-value AZURE_AI_FOUNDRY_PROJECT_ENDPOINT 2>/dev/null || echo '')"
  echo "      ✓ AI Foundry environment vars set"
else
  echo "      ⚠ AZURE_AI_FOUNDRY_PROJECT_NAME not found — skipping Foundry setup"
fi

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

echo ""
echo "=== Post-provision complete ==="
echo ""
echo "Next steps:"
echo "  1. Run Fabric provisioning:  python fabric/setup.py ..."
echo "  2. Start orchestrator:       cd src/orchestrator && dotnet run"
echo "  3. Start API:                cd src/api && uvicorn main:app --reload"
echo "  4. Run tests:                cd tests && pip install -r requirements-test.txt && pytest -v"
