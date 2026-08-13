# Tears down all Azure resources for this environment.
# Purges soft-deleted Key Vault and Cognitive Services so the next `azd up` starts clean.
$ErrorActionPreference = "Stop"

Write-Host "=== Teardown: Public Health RFP POC ===" -ForegroundColor Cyan
Write-Host ""

$confirm = Read-Host "This will DELETE all Azure resources. Type 'yes' to continue"
if ($confirm -ne "yes") {
    Write-Host "Aborted."
    exit 0
}

# Resolve environment values before tearing down
$resourceGroup = (azd env get-value AZURE_RESOURCE_GROUP 2>$null) ?? ""
$location      = (azd env get-value AZURE_LOCATION      2>$null) ?? "eastus"
$envName       = (azd env get-value AZURE_ENV_NAME      2>$null) ?? ""

Write-Host ""
Write-Host "[1/3] Running azd down..." -ForegroundColor Yellow
azd down --force --purge

Write-Host ""
Write-Host "[2/3] Purging soft-deleted Cognitive Services accounts..." -ForegroundColor Yellow
if ($resourceGroup -and $location) {
    $deleted = az cognitiveservices account list-deleted `
        --query "[?location=='$location'].name" -o tsv 2>$null
    if ($deleted) {
        foreach ($acct in $deleted -split "`n" | Where-Object { $_ }) {
            Write-Host "      Purging: $acct"
            az cognitiveservices account purge `
                --name $acct --location $location --resource-group $resourceGroup 2>$null
        }
    } else {
        Write-Host "      No soft-deleted Cognitive Services found."
    }
}

Write-Host ""
Write-Host "[3/3] Clearing local AZD environment state..." -ForegroundColor Yellow
if ($envName -and (Test-Path ".azure\$envName")) {
    $delLocal = Read-Host "Delete local .azure\$envName directory? (yes/no)"
    if ($delLocal -eq "yes") {
        Remove-Item -Recurse -Force ".azure\$envName"
        Write-Host "      Removed .azure\$envName"
    }
}

Write-Host ""
Write-Host "=== Teardown complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "To redeploy from scratch:"
Write-Host "  azd env new <env-name>"
Write-Host "  azd env set AZURE_LOCATION eastus"
Write-Host "  azd env set APIM_PUBLISHER_EMAIL your@email.com"
Write-Host "  azd env set OWNER_EMAIL your@email.com"
Write-Host "  azd up"
