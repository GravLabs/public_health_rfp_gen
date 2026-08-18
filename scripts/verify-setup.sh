#!/bin/bash
# Public Health RFP Generator — Setup Verifier
# Validates every phase of the setup and reports pass / warn / fail per check.
# Usage: bash scripts/verify-setup.sh

set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; AMBER='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

PASS=0; FAIL=0; WARN=0

ok()   { printf "  ${GREEN}✓${NC}  %s\n" "$1"; ((PASS++)) || true; }
err()  { printf "  ${RED}✗${NC}  %s\n" "$1"; ((FAIL++)) || true; }
warn() { printf "  ${AMBER}⚠${NC}  %s\n" "$1"; ((WARN++)) || true; }
info() { printf "  ${DIM}·${NC}  %s\n" "$1"; }
hdr()  { printf "\n${BOLD}${CYAN}── %s ──${NC}\n" "$1"; }

get_env() { azd env get-value "$1" 2>/dev/null || true; }

semver_ok() {
  local ver="$1" min="$2"
  local ma mi rma rmi
  ma=$(echo "$ver" | cut -d. -f1); mi=$(echo "$ver" | cut -d. -f2)
  rma=$(echo "$min" | cut -d. -f1); rmi=$(echo "$min" | cut -d. -f2)
  [ "$ma" -gt "$rma" ] || ([ "$ma" -eq "$rma" ] && [ "$mi" -ge "$rmi" ])
}

# ── Phase 1: Prerequisites ────────────────────────────────────────────────────
hdr "Phase 1 · Prerequisites"

for entry in \
  "az CLI|az version --query '\"azure-cli\"' -o tsv|2.60" \
  "azd|azd version 2>/dev/null|1.9" \
  "dotnet 8|dotnet --version 2>/dev/null|8.0" \
  "python 3.12|python3 --version 2>&1|3.12"
do
  name="${entry%%|*}"; rest="${entry#*|}"; cmd="${rest%%|*}"; min="${rest##*|}"
  ver=$(eval "$cmd" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)
  if [ -z "$ver" ]; then
    err "$name — not found (need >= $min)"
  elif semver_ok "$ver" "$min"; then
    ok "$name $ver"
  else
    err "$name $ver (need >= $min)"
  fi
done

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
  ok "Docker (daemon running)"
elif command -v docker &>/dev/null; then
  warn "Docker installed but daemon not running"
else
  err "Docker — not found"
fi

# ── Phase 2: AZD Environment ──────────────────────────────────────────────────
hdr "Phase 2 · AZD Environment"

RG=$(get_env AZURE_RESOURCE_GROUP)
LOCATION=$(get_env AZURE_LOCATION)
SUB_ID=$(get_env AZURE_SUBSCRIPTION_ID)
OAI_ENDPOINT=$(get_env AZURE_OPENAI_ENDPOINT)
SEARCH_ENDPOINT=$(get_env AZURE_SEARCH_ENDPOINT)
APPINSIGHTS=$(get_env APPLICATIONINSIGHTS_CONNECTION_STRING)
FOUNDRY_ENDPOINT=$(get_env AZURE_AI_FOUNDRY_PROJECT_ENDPOINT)

if [ -n "$SUB_ID" ]; then
  sub_name=$(az account show --subscription "$SUB_ID" --query name -o tsv 2>/dev/null || true)
  ok "AZURE_SUBSCRIPTION_ID: $SUB_ID${sub_name:+ ($sub_name)}"
else
  active_sub=$(az account show --query "{id:id,name:name}" -o tsv 2>/dev/null | tr '\t' ' ' || true)
  warn "AZURE_SUBSCRIPTION_ID not set — azd will use the active az account: $active_sub"
  warn "  To pin it: az account set --subscription <id> && azd env set AZURE_SUBSCRIPTION_ID <id>"
fi

[ -n "$RG" ]               && ok "AZURE_RESOURCE_GROUP: $RG"           || err "AZURE_RESOURCE_GROUP not set — run: azd env new"
[ -n "$LOCATION" ]         && ok "AZURE_LOCATION: $LOCATION"           || err "AZURE_LOCATION not set — run: azd env set AZURE_LOCATION eastus"
[ -n "$OAI_ENDPOINT" ]     && ok "AZURE_OPENAI_ENDPOINT set"           || err "AZURE_OPENAI_ENDPOINT not set — run azd up first"
[ -n "$SEARCH_ENDPOINT" ]  && ok "AZURE_SEARCH_ENDPOINT set"           || err "AZURE_SEARCH_ENDPOINT not set — run azd up first"
[ -n "$APPINSIGHTS" ]      && ok "APPLICATIONINSIGHTS_CONNECTION_STRING set" || warn "APPLICATIONINSIGHTS_CONNECTION_STRING not set"
[ -n "$FOUNDRY_ENDPOINT" ] && ok "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT set"    || warn "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT not set (set by post-provision hook)"

# ── Phase 3: Azure Resources ───────────────────────────────────────────────────
hdr "Phase 3 · Azure Resources"

if [ -z "$RG" ]; then
  warn "Resource group unknown — skipping Azure resource checks"
else
  for svc in api orchestrator; do
    app_name=$(az containerapp list -g "$RG" \
      --query "[?tags.\"azd-service-name\"=='$svc'].name | [0]" -o tsv 2>/dev/null || true)
    if [ -n "$app_name" ] && [ "$app_name" != "null" ]; then
      rev_state=$(az containerapp show -n "$app_name" -g "$RG" \
        --query "properties.latestRevisionName" -o tsv 2>/dev/null || true)
      ok "Container App [$svc]: $app_name (latest: ${rev_state:-unknown})"
    else
      err "Container App [$svc] not found in $RG"
    fi
  done

  if [ -n "$OAI_ENDPOINT" ]; then
    OAI_RES=$(echo "$OAI_ENDPOINT" | sed 's|https://||' | cut -d'.' -f1)
    for dep in gpt-4o gpt-4o-mini text-embedding-3-small; do
      state=$(az cognitiveservices account deployment show \
        --name "$OAI_RES" --resource-group "$RG" --deployment-name "$dep" \
        --query "properties.provisioningState" -o tsv 2>/dev/null || true)
      [ "$state" = "Succeeded" ] && ok "OpenAI deployment: $dep" || warn "OpenAI deployment $dep: ${state:-not found}"
    done
  fi

  if [ -n "$SEARCH_ENDPOINT" ]; then
    SEARCH_SVC=$(echo "$SEARCH_ENDPOINT" | sed 's|https://||' | cut -d'.' -f1)
    SEARCH_KEY=$(az search admin-key show --resource-group "$RG" --service-name "$SEARCH_SVC" \
      --query primaryKey -o tsv 2>/dev/null || true)
    if [ -n "$SEARCH_KEY" ]; then
      doc_count=$(curl -sf "${SEARCH_ENDPOINT}/indexes/pubhealth-rfp-index/docs/\$count?api-version=2024-05-01-preview" \
        -H "api-key: $SEARCH_KEY" --max-time 10 2>/dev/null || echo "0")
      if [ "${doc_count:-0}" -gt 0 ]; then
        ok "Search index pubhealth-rfp-index: $doc_count documents"
      else
        err "Search index: 0 documents — re-run ingestion pipeline"
      fi
    else
      warn "Cannot retrieve search key — skipping doc count check"
    fi
  fi
fi

# ── Phase 4: Bot Setup ─────────────────────────────────────────────────────────
hdr "Phase 4 · Teams Bot"

if [ -z "$RG" ]; then
  warn "Resource group unknown — skipping bot checks"
else
  API_APP=$(az containerapp list -g "$RG" \
    --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || true)

  if [ -n "$API_APP" ] && [ "$API_APP" != "null" ]; then
    CONTAINER_APP_ID=$(az containerapp show -n "$API_APP" -g "$RG" \
      --query "properties.template.containers[0].env[?name=='MICROSOFT_APP_ID'].value | [0]" \
      -o tsv 2>/dev/null || true)
    [ -n "$CONTAINER_APP_ID" ] && [ "$CONTAINER_APP_ID" != "null" ] \
      && ok "MICROSOFT_APP_ID on container: $CONTAINER_APP_ID" \
      || err "MICROSOFT_APP_ID not set on API container — see Phase 3 of quickstart"

    CONTAINER_PW=$(az containerapp show -n "$API_APP" -g "$RG" \
      --query "properties.template.containers[0].env[?name=='MICROSOFT_APP_PASSWORD'].value | [0]" \
      -o tsv 2>/dev/null || true)
    [ -n "$CONTAINER_PW" ] && [ "$CONTAINER_PW" != "null" ] \
      && ok "MICROSOFT_APP_PASSWORD on container: [set]" \
      || err "MICROSOFT_APP_PASSWORD not set on API container"
  fi

  MANIFEST_BOT_ID=$(python3 -c \
    "import json; d=json.load(open('teams-app/manifest.json')); print(d.get('bots',[{}])[0].get('botId',''))" \
    2>/dev/null || true)
  [ -n "$MANIFEST_BOT_ID" ] \
    && ok "teams-app/manifest.json botId: $MANIFEST_BOT_ID" \
    || err "manifest.json botId empty — update it with your App Registration appId"

  if [ -n "${CONTAINER_APP_ID:-}" ] && [ -n "$MANIFEST_BOT_ID" ] && \
     [ "$CONTAINER_APP_ID" != "null" ] && [ "$CONTAINER_APP_ID" != "$MANIFEST_BOT_ID" ]; then
    err "App ID mismatch: container ($CONTAINER_APP_ID) ≠ manifest ($MANIFEST_BOT_ID)"
  elif [ -n "${CONTAINER_APP_ID:-}" ] && [ -n "$MANIFEST_BOT_ID" ] && \
       [ "$CONTAINER_APP_ID" != "null" ]; then
    ok "App IDs consistent across container and manifest"
  fi

  BOT_NAME=$(az resource list -g "$RG" --resource-type Microsoft.BotService/botServices \
    --query "[0].name" -o tsv 2>/dev/null || true)
  if [ -n "$BOT_NAME" ] && [ "$BOT_NAME" != "null" ]; then
    BOT_ENDPOINT=$(az bot show -g "$RG" -n "$BOT_NAME" \
      --query "properties.endpoint" -o tsv 2>/dev/null || true)
    [[ "${BOT_ENDPOINT:-}" == https://* ]] \
      && ok "Bot Service endpoint: $BOT_ENDPOINT" \
      || err "Bot Service endpoint missing — run: az bot update -g $RG -n $BOT_NAME --endpoint https://<fqdn>/api/messages"
  else
    err "No Bot Service found in $RG"
  fi
fi

# ── Phase 5: SharePoint ────────────────────────────────────────────────────────
hdr "Phase 5 · SharePoint"

SP_SITE_ID=$(get_env SHAREPOINT_SITE_ID || true)
SP_LIBRARY=$(get_env SHAREPOINT_DRAFT_LIBRARY || true)

[ -n "$SP_SITE_ID" ] \
  && ok "SHAREPOINT_SITE_ID set" \
  || warn "SHAREPOINT_SITE_ID not set — Approve & Save button will return 503"

[ -n "$SP_LIBRARY" ] \
  && ok "SHAREPOINT_DRAFT_LIBRARY: $SP_LIBRARY" \
  || warn "SHAREPOINT_DRAFT_LIBRARY not set (API defaults to 'Generated Drafts')"

# ── Phase 6: API Health + Smoke Test ──────────────────────────────────────────
hdr "Phase 6 · API Health & Smoke Test"

if [ -z "$RG" ]; then
  warn "Resource group unknown — skipping API health check"
else
  API_APP=$(az containerapp list -g "$RG" \
    --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || true)
  if [ -n "$API_APP" ] && [ "$API_APP" != "null" ]; then
    FQDN=$(az containerapp show -n "$API_APP" -g "$RG" \
      --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null || true)
    if [ -n "$FQDN" ]; then
      health=$(curl -sf "https://$FQDN/health" --max-time 15 2>/dev/null || true)
      if echo "${health:-}" | grep -q '"status"'; then
        ok "GET /health: https://$FQDN/health"
      else
        err "GET /health unreachable — container may still be starting"
      fi

      info "Running smoke test: POST /classify ..."
      classify=$(curl -sf -X POST "https://$FQDN/classify" \
        -H "Content-Type: application/json" \
        -d '{"text":"influenza surveillance program, CDC, 24 months"}' \
        --max-time 20 2>/dev/null || true)
      if echo "${classify:-}" | grep -q "program_area"; then
        area=$(echo "$classify" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("program_area","?"))' 2>/dev/null || echo "?")
        ok "POST /classify → program_area: $area"
      else
        err "POST /classify returned no program_area — check container logs"
      fi
    else
      err "Could not resolve API container FQDN"
    fi
  fi
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
printf "${BOLD}%s${NC}\n" "────────────────────────────────────────"
printf "${BOLD}  ${GREEN}%d passed${NC}  ${RED}%d failed${NC}  ${AMBER}%d warnings${NC}\n" "$PASS" "$FAIL" "$WARN"
printf "${BOLD}%s${NC}\n" "────────────────────────────────────────"

if [ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]; then
  printf "\n${GREEN}${BOLD}All checks passed.${NC} Open Teams and send:\n"
  printf "  Draft a 24-month influenza surveillance RFP. CDC, \$2.5M, awards \$150K–\$400K.\n\n"
elif [ "$FAIL" -eq 0 ]; then
  printf "\n${AMBER}${BOLD}Passed with warnings.${NC} Review items above before testing in Teams.\n\n"
else
  printf "\n${RED}${BOLD}%d check(s) failed.${NC} Complete the flagged phases before testing in Teams.\n\n" "$FAIL"
fi
