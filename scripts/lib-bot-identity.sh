#!/bin/bash
# Shared helpers: verify the Teams bot's Entra ID App Registration, Service
# Principal, and Bot Service are all present and actually wired together end
# to end — then prove it with a live message round-trip via Direct Line.
#
# Why this exists: Bot Connector resolves a bot's identity via its Entra ID
# App Registration + Service Principal. If either object is missing (e.g. an
# accidental `az ad app delete`, or a Service Principal that was never created
# in the first place — `az ad app create` does NOT create one automatically),
# the Bot Service resource, container env vars, and Teams manifest can all
# still look perfectly consistent — same App ID everywhere — while the bot
# silently receives zero messages. Connector fails to resolve the identity
# before it ever calls our endpoint, so nothing shows up in our own logs or
# App Insights. Config-only checks (do the IDs match across files?) cannot
# catch this — only checking Entra ID directly, or a live round-trip test,
# can. This happened in production on 2026-08-19 (App Registration deleted
# ~1hr after creation, Service Principal never existed) and left zero trace
# anywhere in our own telemetry.
#
# Source this file, then call:
#   bot_identity_ensure "$APP_ID"                 — checks/heals App Reg + SP
#   bot_identity_roundtrip_test "$RG" "$BOT_NAME"  — live message test via Direct Line
# Both print their own progress and return 0 (healthy) or 1 (needs attention).

bot_identity_ensure() {
  local APP_ID="$1"
  local healed=0

  if [ -z "$APP_ID" ]; then
    echo "  (no BOT_APP_ID set yet — nothing to verify)"
    return 0
  fi

  echo "  Checking Entra ID App Registration ($APP_ID)..."
  if az ad app show --id "$APP_ID" &>/dev/null; then
    echo "  ✓ App Registration exists"
  else
    echo "  ✗ App Registration MISSING — checking Entra ID soft-delete..."
    local deleted_id
    deleted_id=$(az rest --method GET \
      --url "https://graph.microsoft.com/v1.0/directory/deletedItems/microsoft.graph.application?\$filter=appId eq '$APP_ID'" \
      --query "value[0].id" -o tsv 2>/dev/null || true)
    if [ -n "$deleted_id" ] && [ "$deleted_id" != "None" ]; then
      echo "    Found soft-deleted App Registration ($deleted_id) — restoring..."
      if az rest --method POST \
        --url "https://graph.microsoft.com/v1.0/directory/deletedItems/$deleted_id/restore" \
        --output none 2>/dev/null; then
        echo "  ✓ App Registration restored (same App ID and secret)"
        healed=1
      else
        echo "  ✗✗ Restore call failed."
        return 1
      fi
    else
      echo "  ✗✗ App Registration is permanently gone (not in soft-delete — either purged"
      echo "     or past the 30-day retention window)."
      echo "     A NEW App Registration must be created and every downstream config"
      echo "     (Bot Service msaAppId, container MICROSOFT_APP_ID/PASSWORD,"
      echo "     teams-app/manifest.json) updated to match."
      echo "     Run: bash scripts/install.sh --from 2"
      return 1
    fi
  fi

  echo "  Checking Service Principal..."
  if az ad sp show --id "$APP_ID" &>/dev/null; then
    echo "  ✓ Service Principal exists"
  else
    echo "  ✗ Service Principal MISSING — creating..."
    if az ad sp create --id "$APP_ID" --output none 2>/dev/null; then
      echo "  ✓ Service Principal created"
      healed=1
    else
      echo "  ✗✗ Failed to create Service Principal — check permissions"
      return 1
    fi
  fi

  if [ "$healed" -eq 1 ]; then
    echo "  Identity objects were healed — giving Bot Connector a moment to pick it up..."
    sleep 15
  fi

  return 0
}

# Sends a real message through Bot Connector via the bot's Direct Line channel
# and waits for a reply — the only check that actually proves end-to-end
# delivery works, independent of Teams app install/upload state.
bot_identity_roundtrip_test() {
  local RG="$1" BOT_NAME="$2"
  local secret conv_id resp attempt

  secret=$(az bot directline show -g "$RG" -n "$BOT_NAME" --with-secrets true \
    --query "properties.properties.sites[0].key" -o tsv 2>/dev/null || true)
  if [ -z "$secret" ] || [ "$secret" == "None" ]; then
    echo "    (Direct Line channel not available on this bot — skipping live round-trip test)"
    return 0
  fi

  conv_id=$(curl -sf -X POST "https://directline.botframework.com/v3/directline/conversations" \
    -H "Authorization: Bearer $secret" --max-time 15 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['conversationId'])" 2>/dev/null || true)
  if [ -z "$conv_id" ]; then
    echo "    Could not start a Direct Line conversation"
    return 1
  fi

  curl -sf -X POST "https://directline.botframework.com/v3/directline/conversations/$conv_id/activities" \
    -H "Authorization: Bearer $secret" -H "Content-Type: application/json" \
    -d '{"type":"message","from":{"id":"install-verify"},"text":"Any recent CFR changes?"}' \
    --max-time 15 --output /dev/null 2>/dev/null || true

  for attempt in 1 2 3 4 5 6; do
    sleep 3
    resp=$(curl -sf "https://directline.botframework.com/v3/directline/conversations/$conv_id/activities" \
      -H "Authorization: Bearer $secret" --max-time 15 2>/dev/null || true)
    if [ -n "$resp" ] && echo "$resp" | python3 -c "
import json, sys
d = json.load(sys.stdin)
acts = [a for a in d.get('activities', []) if a.get('from', {}).get('id') != 'install-verify']
sys.exit(0 if acts else 1)
" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}
