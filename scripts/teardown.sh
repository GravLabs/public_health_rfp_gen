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

# `azd down` clears AZURE_LOCATION from azd env even though it's a required
# INPUT parameter, not a derived resource output (unlike AZURE_RESOURCE_GROUP,
# search/storage endpoints, etc. — those are correctly cleared since the
# resources are gone). Left unset, the next `azd up` hard-fails non-interactively
# with "prompt required" instead of reusing the region we just tore down from.
# Restore it from the value captured above so re-provisioning never needs a
# manual `azd env set AZURE_LOCATION` step.
azd env set AZURE_LOCATION "$AZURE_LOCATION"
echo "      ✓ AZURE_LOCATION restored: $AZURE_LOCATION (next 'azd up' can run unattended)"

echo ""
echo "[2/4] Purging any remaining soft-deleted Cognitive Services accounts..."
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
echo "[3/4] Purging any remaining soft-deleted API Management instances..."
# API Management soft-deletes on removal too; azd --purge doesn't cover it.
# Name/location come straight from the deleted-service list to avoid region
# string-format mismatches (e.g. 'eastus' vs 'East US').
DELETED_APIM=$(az apim deletedservice list --query "[].[name,location]" -o tsv 2>/dev/null || echo "")
if [ -n "$DELETED_APIM" ]; then
  while IFS=$'\t' read -r svc_name svc_location; do
    [ -z "$svc_name" ] && continue
    echo "      Purging deleted APIM: $svc_name ($svc_location)"
    az apim deletedservice purge --service-name "$svc_name" --location "$svc_location" 2>/dev/null || true
  done <<< "$DELETED_APIM"
else
  echo "      No soft-deleted APIM instances found."
fi

echo ""
echo "[4/4] Clearing local AZD environment state..."
if [ -d ".azure" ]; then
  echo ""
  echo "  WARNING: .azure/ holds saved secrets (BOT_APP_SECRET, SHAREPOINT_SITE_ID, etc.)."
  echo "  If you delete it, install.sh will re-prompt for the bot client secret on next run."
  echo "  Answer 'no' unless you are fully resetting for a different tenant/org."
  echo ""
  read -r -p "Delete entire .azure/ directory? (yes/no) [no]: " del_local
  if [ "$del_local" = "yes" ]; then
    rm -rf ".azure"
    echo "      Removed .azure/"
    echo "      NOTE: You will need to re-enter BOT_APP_SECRET during the next install."
  else
    echo "      Kept .azure/ — secrets preserved for next install."
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
