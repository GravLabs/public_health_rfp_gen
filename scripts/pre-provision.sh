#!/bin/bash
# Verify (and self-heal) the Teams bot's Entra ID identity BEFORE Bicep runs.
# Bot Service is msaAppType SingleTenant, so Azure validates BOT_APP_ID against
# the tenant at provision time — if the App Registration or Service Principal
# is missing, provisioning still succeeds but produces a Bot Service that will
# never receive a single message, with no error anywhere. Catching it here
# (before provision) is cheaper than discovering it after a full deploy.
set -e

source "$(dirname "${BASH_SOURCE[0]}")/lib-bot-identity.sh"

BOT_APP_ID_CHECK=$(azd env get-value BOT_APP_ID 2>/dev/null || echo "")

if [ -z "$BOT_APP_ID_CHECK" ]; then
  # First-ever run, or Phase 2 of install.sh hasn't created one yet — nothing
  # to verify. install.sh's own ensure_bot_app_registration() is what creates
  # and validates it before `azd up` is invoked.
  exit 0
fi

echo "=== Pre-provision: verifying Teams bot identity ==="
if bot_identity_ensure "$BOT_APP_ID_CHECK"; then
  echo "  ✓ Bot identity confirmed before provisioning"
else
  echo "  ✗✗ Bot identity is broken and could not be healed automatically."
  echo "     Provisioning would create a Bot Service pointing at a dead identity —"
  echo "     it would succeed, but the bot would silently receive zero messages."
  echo "     Fix first with: bash scripts/install.sh --from 2"
  exit 1
fi
