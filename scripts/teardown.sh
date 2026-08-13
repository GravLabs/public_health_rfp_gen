#!/bin/bash
# Tears down all Azure resources for this environment.
# Purges soft-deleted Key Vault and Cognitive Services so the next `azd up` starts clean.
set -e

echo "=== Teardown: Public Health RFP POC ==="
echo ""

# Confirm before destroying
read -r -p "This will DELETE all Azure resources. Type 'yes' to continue: " confirm
if [ "$confirm" != "yes" ] && [ "$confirm" != "y" ]; then
  echo "Aborted."
  exit 0
fi

# Resolve environment values before tearing down
RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || echo "")
AZURE_LOCATION=$(azd env get-value AZURE_LOCATION 2>/dev/null || echo "eastus")
ENV_NAME=$(azd env get-value AZURE_ENV_NAME 2>/dev/null || echo "")

echo ""
echo "[1/3] Running azd down (deletes resource group + all resources)..."
# --force skips interactive confirmation, --purge purges soft-deleted KV and Cognitive Services
azd down --force --purge

echo ""
echo "[2/3] Purging any remaining soft-deleted Cognitive Services accounts..."
# azd --purge covers Key Vault; Cognitive Services soft-delete needs a separate purge
if [ -n "$RESOURCE_GROUP" ] && [ -n "$AZURE_LOCATION" ]; then
  DELETED=$(az cognitiveservices account list-deleted \
    --query "[?location=='${AZURE_LOCATION}'].name" -o tsv 2>/dev/null || echo "")
  if [ -n "$DELETED" ]; then
    while IFS= read -r acct; do
      echo "      Purging deleted Cognitive Services: $acct"
      az cognitiveservices account purge \
        --name "$acct" \
        --location "$AZURE_LOCATION" \
        --resource-group "$RESOURCE_GROUP" 2>/dev/null || true
    done <<< "$DELETED"
  else
    echo "      No soft-deleted Cognitive Services found."
  fi
fi

echo ""
echo "[3/3] Clearing local AZD environment state..."
if [ -n "$ENV_NAME" ] && [ -d ".azure/$ENV_NAME" ]; then
  read -r -p "Delete local .azure/$ENV_NAME directory? (yes/no): " del_local
  if [ "$del_local" = "yes" ]; then
    rm -rf ".azure/$ENV_NAME"
    echo "      Removed .azure/$ENV_NAME"
  fi
fi

echo ""
echo "=== Teardown complete ==="
echo ""
echo "To redeploy from scratch:"
echo "  azd env new <env-name>"
echo "  azd env set AZURE_LOCATION eastus"
echo "  azd env set APIM_PUBLISHER_EMAIL your@email.com"
echo "  azd env set OWNER_EMAIL your@email.com"
echo "  azd up"
