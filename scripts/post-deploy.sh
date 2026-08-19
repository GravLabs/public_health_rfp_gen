#!/bin/bash
# Wire up the Bot Service endpoint after container apps are deployed.
# AZD runs this after 'azd deploy' — at that point the API container app FQDN is known.
set -e

echo "=== Post-deploy: updating Bot Service endpoint ==="

source "$(dirname "${BASH_SOURCE[0]}")/lib-bot-identity.sh"

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

# Bicep @secure() params don't propagate from AZD env — set MICROSOFT_APP_PASSWORD directly
API_APP_NAME=$(az containerapp list \
  --resource-group "$RESOURCE_GROUP" \
  --query "[?tags.\"azd-service-name\"=='api'].name | [0]" \
  -o tsv 2>/dev/null || echo "")
BOT_APP_SECRET=$(azd env get-value BOT_APP_SECRET 2>/dev/null || echo "")
if [ -n "$BOT_APP_SECRET" ] && [ -n "$API_APP_NAME" ]; then
  az containerapp update \
    --name "$API_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --set-env-vars "MICROSOFT_APP_PASSWORD=$BOT_APP_SECRET" \
    --output none 2>/dev/null && echo "  ✓ MICROSOFT_APP_PASSWORD set on API container"
fi

# Persist the messaging endpoint for reference
azd env set BOT_MESSAGING_ENDPOINT "$MESSAGING_ENDPOINT"

echo ""
echo "=== Verifying bot identity end to end ==="
BOT_APP_ID_CHECK=$(azd env get-value BOT_APP_ID 2>/dev/null || echo "")
if [ -n "$BOT_APP_ID_CHECK" ]; then
  if bot_identity_ensure "$BOT_APP_ID_CHECK"; then
    echo "  Sending a live test message via Direct Line (bypasses Teams entirely)..."
    if bot_identity_roundtrip_test "$RESOURCE_GROUP" "$BOT_NAME"; then
      echo "  ✓ Bot responded to a live test message — identity chain confirmed working."
    else
      echo "  ✗✗ Bot did NOT respond to a live test message."
      echo "     Teams will not work either — this is the same delivery path."
      echo "     Check container logs: az containerapp logs show -n $API_APP_NAME -g $RESOURCE_GROUP --tail 100"
    fi
  else
    echo "  ✗✗ Bot identity is broken — see errors above. Teams will not receive messages."
    echo "     Fix with: bash scripts/install.sh --from 2"
  fi
else
  echo "  · BOT_APP_ID not set — skipping identity check"
fi

echo ""
echo "=== Post-deploy complete ==="
echo "  Bot Service: ${BOT_NAME}"
echo "  Messaging endpoint: ${MESSAGING_ENDPOINT}"
