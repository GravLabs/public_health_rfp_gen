# Post-provision hook — Public Health RFP POC (Windows / PowerShell)
# Equivalent to scripts/post-provision.sh
param()
$ErrorActionPreference = 'Stop'

Write-Host "=== Post-provision: Public Health RFP POC ==="

# Resolve AZD environment variables
$ACCOUNT          = azd env get-value AZURE_STORAGE_ACCOUNT
$SEARCH_ENDPOINT  = azd env get-value AZURE_SEARCH_ENDPOINT
$OPENAI_ENDPOINT  = azd env get-value AZURE_OPENAI_ENDPOINT
$APPINSIGHTS_CONN = azd env get-value APPLICATIONINSIGHTS_CONNECTION_STRING
$FOUNDRY_PROJECT  = try { azd env get-value AZURE_AI_FOUNDRY_PROJECT_NAME } catch { "" }
$CONTAINER        = "rfp-corpus"

Write-Host ""
Write-Host "[1/6] Uploading 50 sample RFPs to blob storage: $ACCOUNT/$CONTAINER"
az storage blob upload-batch `
    --account-name $ACCOUNT `
    --destination  $CONTAINER `
    --source       "data/sample-rfps" `
    --pattern      "*.md" `
    --auth-mode    login `
    --overwrite

Write-Host "[2/6] Uploading 20 eval examples to golden-dataset container"
az storage blob upload-batch `
    --account-name $ACCOUNT `
    --destination  "golden-dataset" `
    --source       "data/eval-examples" `
    --pattern      "*.json" `
    --auth-mode    login `
    --overwrite

Write-Host "[3/6] Creating AI Search index and running ingestion pipeline"
$env:AZURE_SEARCH_ENDPOINT = $SEARCH_ENDPOINT
$env:AZURE_OPENAI_ENDPOINT = $OPENAI_ENDPOINT
python src/ingestion/create_index.py
python src/ingestion/pipeline.py

Write-Host "[4/6] Setting up AI Foundry connections"
if ($FOUNDRY_PROJECT) {
    Write-Host "      AI Foundry project: $FOUNDRY_PROJECT"
    $endpoint = try { azd env get-value AZURE_AI_FOUNDRY_PROJECT_ENDPOINT } catch { "" }
    if ($endpoint) { azd env set AZURE_AI_FOUNDRY_PROJECT_ENDPOINT $endpoint }
    Write-Host "      OK AI Foundry environment vars set"
} else {
    Write-Host "      WARNING: AZURE_AI_FOUNDRY_PROJECT_NAME not found — skipping Foundry setup"
}

Write-Host "[5/6] Fabric setup"
$FABRIC_WORKSPACE = try { azd env get-value FABRIC_WORKSPACE_ID } catch { "" }
if ($FABRIC_WORKSPACE) {
    Write-Host "      Fabric workspace already provisioned: $FABRIC_WORKSPACE"
} else {
    Write-Host "      INFO: Fabric not provisioned yet."
    Write-Host "      To provision, run:"
    Write-Host "        python fabric/setup.py ``"
    Write-Host "          --workspace-name pubhealth-rfp-poc ``"
    Write-Host "          --ai-search-endpoint `$env:AZURE_SEARCH_ENDPOINT ``"
    Write-Host "          --sharepoint-site-id <YOUR_SITE_ID>"
}

Write-Host "[6/6] Writing .env file from AZD environment"
@"
AZURE_SEARCH_ENDPOINT=$SEARCH_ENDPOINT
AZURE_OPENAI_ENDPOINT=$OPENAI_ENDPOINT
AZURE_OPENAI_GPT_DEPLOYMENT=gpt-4o
AZURE_OPENAI_MINI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_O3_DEPLOYMENT=o3-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
APPLICATIONINSIGHTS_CONNECTION_STRING=$APPINSIGHTS_CONN
ORCHESTRATOR_URL=http://localhost:5001
MONTHLY_BUDGET_USD=500
BUDGET_WARN_THRESHOLD=0.80
BUDGET_CRITICAL_THRESHOLD=0.95
"@ | Set-Content -Path ".env" -Encoding UTF8
Write-Host "      .env written (do not commit — already in .gitignore)"

Write-Host ""
Write-Host "=== Post-provision complete ==="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Run Fabric provisioning:  python fabric/setup.py ..."
Write-Host "  2. Start orchestrator:       cd src/orchestrator; dotnet run"
Write-Host "  3. Start API:                cd src/api; uvicorn main:app --reload"
Write-Host "  4. Run tests:                cd tests; pip install -r requirements-test.txt; pytest -v"
