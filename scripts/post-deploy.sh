#!/bin/bash
# Wire up the Bot Service endpoint after container apps are deployed.
# AZD runs this after 'azd deploy' — at that point the API container app FQDN is known.
set -e

echo "=== Post-deploy: updating Bot Service endpoint ==="

RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || echo "")
BOT_NAME=$(azd env get-value AZURE_BOT_NAME 2>/dev/null || echo "")
CLIENT_ID=$(azd env get-value AZURE_CLIENT_ID 2>/dev/null || echo "")

if [ -z "$RESOURCE_GROUP" ] || [ -z "$BOT_NAME" ]; then
  echo "  ⚠ AZURE_RESOURCE_GROUP or AZURE_BOT_NAME not set — skipping bot endpoint update"
  exit 0
fi

# Resolve the API container app FQDN — AZD tags it with azd-service-name=api
API_FQDN=$(az containerapp list \
  --resource-group "$RESOURCE_GROUP" \
  --query "[?tags.\"azd-service-name\"=='api'].properties.configuration.ingress.fqdn | [0]" \
  -o tsv 2>/dev/null || echo "")

if [ -z "$API_FQDN" ]; then
  echo "  ⚠ Could not resolve API container app FQDN — skipping bot endpoint update"
  exit 0
fi

MESSAGING_ENDPOINT="https://${API_FQDN}/api/messages"
echo "  API endpoint: ${MESSAGING_ENDPOINT}"

# Update the Bot Service messaging endpoint
az bot update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$BOT_NAME" \
  --endpoint "$MESSAGING_ENDPOINT" \
  --output none
echo "  ✓ Bot Service endpoint updated"

# MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD, MICROSOFT_APP_TYPE are injected via Bicep env vars

# Persist the messaging endpoint for reference
azd env set BOT_MESSAGING_ENDPOINT "$MESSAGING_ENDPOINT"

echo ""
echo "=== Post-deploy complete ==="
echo "  Bot Service: ${BOT_NAME}"
echo "  Messaging endpoint: ${MESSAGING_ENDPOINT}"
