#!/bin/bash
# Public Health RFP Generator — Guided Installer
#
# Walks through all six setup phases interactively, prompting for
# configuration details and confirming before long-running operations.
#
# Usage: bash scripts/install.sh [--from PHASE]
#   --from PHASE   Start from a specific phase (1-6), useful for resuming

# Don't use set -e — interactive installers need explicit error handling
set -uo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
GRN='\033[0;32m'; RED='\033[0;31m'; AMB='\033[0;33m'
CYN='\033[0;36m'; BLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# ── Print helpers ─────────────────────────────────────────────────────────────
ok()   { printf "  ${GRN}✓${NC}  %s\n" "$1"; }
err()  { printf "  ${RED}✗${NC}  %s\n" "$1"; }
warn() { printf "  ${AMB}⚠${NC}  %s\n" "$1"; }
info() { printf "  ${DIM}·${NC}  %s\n" "$1"; }

phase_hdr() {
  local num="$1" title="$2" time="${3:-}"
  printf "\n${BLD}${CYN}"
  printf "═%.0s" {1..52}
  printf "${NC}\n"
  printf "${BLD}${CYN}  Phase %s — %-38s${NC}\n" "$num" "$title"
  [ -n "$time" ] && printf "${DIM}  %s${NC}\n" "$time"
  printf "${BLD}${CYN}"
  printf "═%.0s" {1..52}
  printf "${NC}\n\n"
}

step_hdr() { printf "\n${BLD}▸ %s${NC}\n" "$1"; }

ask() {
  # ask "Prompt" ["default"] — prints prompt, reads input, echoes result
  local msg="$1" default="${2:-}"
  if [ -n "$default" ]; then
    printf "${BLD}%s${NC} [%s]: " "$msg" "$default" >&2
  else
    printf "${BLD}%s${NC}: " "$msg" >&2
  fi
  local val; read -r val
  printf '%s' "${val:-$default}"
}

ask_yn() {
  # ask_yn "Question" ["y"|"n"] — returns 0=yes, 1=no
  local msg="$1" default="${2:-y}"
  while true; do
    if [ "$default" = "y" ]; then
      printf "${BLD}%s${NC} [Y/n]: " "$msg"
    else
      printf "${BLD}%s${NC} [y/N]: " "$msg"
    fi
    local yn; read -r yn
    yn="${yn:-$default}"
    case "$yn" in
      [Yy]*) return 0 ;;
      [Nn]*) return 1 ;;
      *) printf "  Please enter y or n.\n" ;;
    esac
  done
}

press_enter() {
  printf "\n${DIM}  Press Enter to continue...${NC} "
  read -r
}

die() { err "$1"; exit 1; }

get_env() { local v; v=$(azd env get-value "$1" 2>/dev/null) && echo "$v" || true; }

# Global state (set across phases)
APP_ID=""
APP_PASSWORD=""
RG=""
API_APP=""

# ── Phase 1: Prerequisites ────────────────────────────────────────────────────
phase_1() {
  phase_hdr "1/6" "Prerequisites" "~5 min"

  semver_ok() {
    local ver="$1" min="$2"
    [ -z "$ver" ] && return 1
    local ma mi rma rmi
    ma=$(echo "$ver" | cut -d. -f1)
    mi=$(echo "$ver" | cut -d. -f2 | tr -dc '0-9')
    rma=$(echo "$min" | cut -d. -f1)
    rmi=$(echo "$min" | cut -d. -f2 | tr -dc '0-9')
    [ "${ma:-0}" -gt "$rma" ] || ([ "${ma:-0}" -eq "$rma" ] && [ "${mi:-0}" -ge "$rmi" ])
  }

  local failed=0
  for entry in \
    "az CLI|az version --query '\"azure-cli\"' -o tsv|2.60|https://aka.ms/install-azure-cli" \
    "azd|azd version|1.9|https://aka.ms/install-azd" \
    "dotnet 8|dotnet --version|8.0|https://dot.net/v1/dotnet-install.sh" \
    "python 3.12|python3 --version 2>&1|3.12|https://www.python.org/downloads"
  do
    local name="${entry%%|*}" rest="${entry#*|}"
    local cmd="${rest%%|*}" rest2="${rest#*|}"
    local min="${rest2%%|*}" url="${rest2##*|}"
    local ver; ver=$(eval "$cmd" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)
    if [ -z "$ver" ]; then
      err "$name — not found (need >= $min)"
      info "Install: $url"
      ((failed++)) || true
    elif semver_ok "$ver" "$min"; then
      ok "$name $ver"
    else
      err "$name $ver — need >= $min"
      info "Upgrade: $url"
      ((failed++)) || true
    fi
  done

  if command -v docker &>/dev/null; then
    if docker info &>/dev/null 2>&1; then
      ok "Docker (daemon running)"
    else
      warn "Docker installed but daemon not running — start Docker Desktop before deploying"
    fi
  else
    err "Docker — not found (https://www.docker.com/products/docker-desktop)"
    ((failed++)) || true
  fi

  if [ "${failed:-0}" -gt 0 ]; then
    echo ""
    die "${failed} prerequisite(s) missing. Install them and re-run this script."
  fi

  echo ""
  ok "All prerequisites satisfied."
}

# ── Phase 2: Authenticate & Select Subscription ───────────────────────────────
phase_2() {
  phase_hdr "2/6" "Authenticate & Select Subscription" "~2 min"

  step_hdr "Azure CLI login"
  if az account show &>/dev/null 2>&1; then
    local current_name; current_name=$(az account show --query name -o tsv 2>/dev/null || echo "unknown")
    ok "Already logged in as: $current_name"
    if ! ask_yn "Use this account?"; then
      az login --output none
    fi
  else
    info "Opening browser for az login..."
    az login --output none
  fi
  ok "Azure CLI authenticated"

  step_hdr "Azure Developer CLI login"
  info "Opening browser for azd auth login..."
  azd auth login --output none
  ok "azd authenticated"

  step_hdr "Subscription"
  echo ""
  az account list --query "[].{Name:name, ID:id, State:state}" -o table 2>/dev/null || true
  echo ""

  local current_id; current_id=$(az account show --query id -o tsv 2>/dev/null || true)
  local current_name; current_name=$(az account show --query name -o tsv 2>/dev/null || true)
  info "Active subscription: ${current_name} (${current_id})"
  echo ""

  local sub_id="$current_id"
  if ! ask_yn "Deploy to this subscription?"; then
    sub_id=$(ask "Enter subscription ID or name")
    az account set --subscription "$sub_id" --output none
    sub_id=$(az account show --query id -o tsv)
    local sub_name; sub_name=$(az account show --query name -o tsv)
    ok "Switched to: $sub_name ($sub_id)"
  fi

  # Pin for azd
  azd env set AZURE_SUBSCRIPTION_ID "$sub_id" 2>/dev/null || true
  ok "AZURE_SUBSCRIPTION_ID: $sub_id"
}

# ── Phase 3: AZD Environment & Deploy ────────────────────────────────────────
phase_3() {
  phase_hdr "3/6" "AZD Environment & Deploy" "~25 min"

  step_hdr "AZD environment"
  local env_name; env_name=$(ask "Environment name" "pubhealth-rfp-poc")

  # Check if this named environment already exists and is deployed
  local existing_rg=""
  if azd env select "$env_name" &>/dev/null 2>&1; then
    existing_rg=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | tr -d '[:space:]' || true)
  fi

  if [ -n "$existing_rg" ]; then
    warn "Environment '$env_name' is already deployed."
    info "Resource group: $existing_rg"
    echo ""
    if ask_yn "Skip setup and just re-run azd up?"; then
      RG="$existing_rg"
      if ! azd up; then
        die "azd up failed — check the errors above, then re-run: bash scripts/install.sh --from 3"
      fi
      RG=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | tr -d '[:space:]' || true)
      ok "Deployment complete. Resource group: $RG"
      return
    fi
    if ask_yn "Skip deployment entirely (already fully deployed)?"; then
      RG="$existing_rg"
      return
    fi
  fi

  local location; location=$(ask "Azure region" "eastus")
  local pub_email; pub_email=$(ask "Your email (for API Management publisher + budget alerts)")

  if ! azd env select "$env_name" &>/dev/null 2>&1; then
    azd env new "$env_name"
  fi
  azd env set AZURE_LOCATION "$location"
  azd env set APIM_PUBLISHER_EMAIL "$pub_email"
  azd env set OWNER_EMAIL "$pub_email"
  # Apply subscription ID saved in phase_2 (was set before env existed — re-apply now)
  local sub_id; sub_id=$(az account show --query id -o tsv 2>/dev/null || true)
  [ -n "$sub_id" ] && azd env set AZURE_SUBSCRIPTION_ID "$sub_id"
  ok "Environment '$env_name' configured."

  step_hdr "Deploy to Azure"
  echo ""
  info "azd up provisions ~20 resources and takes approximately 20 minutes."
  info "Resources: OpenAI · AI Search · Container Apps · APIM · Bot Service · AI Foundry · Key Vault"
  echo ""
  warn "Do not close this terminal during deployment."
  echo ""

  if ask_yn "Start deployment now?"; then
    if ! azd up; then
      die "azd up failed — check the errors above, then re-run: bash scripts/install.sh --from 3"
    fi
    RG=$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | tr -d '[:space:]' || true)
    [ -z "$RG" ] && die "AZURE_RESOURCE_GROUP not set after azd up — provision may have partially failed."
    ok "Deployment complete. Resource group: $RG"
  else
    echo ""
    info "Run 'azd up' when ready, then re-run: bash scripts/install.sh --from 4"
    exit 0
  fi
}

# ── Phase 4: Teams Bot Setup ──────────────────────────────────────────────────
phase_4() {
  phase_hdr "4/6" "Teams Bot Setup" "~10 min"

  RG=$(get_env AZURE_RESOURCE_GROUP)
  [ -z "$RG" ] && die "AZURE_RESOURCE_GROUP not set — complete Phase 3 first."

  API_APP=$(az containerapp list -g "$RG" \
    --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || true)
  [ -z "$API_APP" ] || [ "$API_APP" = "null" ] && die "API Container App not found in $RG."

  # Detect existing bot credentials
  local existing_id; existing_id=$(az containerapp show -n "$API_APP" -g "$RG" \
    --query "properties.template.containers[0].env[?name=='MICROSOFT_APP_ID'].value | [0]" \
    -o tsv 2>/dev/null || true)

  if [ -n "$existing_id" ] && [ "$existing_id" != "null" ]; then
    ok "Bot App ID already set on container: $existing_id"
    APP_ID="$existing_id"
    # Check if the secret is already stored in azd env
    local stored_secret; stored_secret=$(get_env BOT_APP_SECRET)
    if [ -n "$stored_secret" ]; then
      ok "BOT_APP_SECRET found in azd env — re-applying to container."
      APP_PASSWORD="$stored_secret"
    else
      warn "BOT_APP_SECRET not in azd env — need to (re-)enter the client secret."
      APP_PASSWORD=$(ask "Client secret for App Registration $APP_ID")
      azd env set BOT_APP_SECRET "$APP_PASSWORD"
      ok "BOT_APP_SECRET saved to AZD env"
    fi
  else
    step_hdr "App Registration"
    echo ""
    if ask_yn "Do you have an existing App Registration to reuse?" "n"; then
      APP_ID=$(ask "App Registration ID (appId)")
      APP_PASSWORD=$(ask "Client secret (password)")
    else
      local app_name; app_name=$(ask "App Registration display name" "pubhealth-rfp-bot")
      info "Creating App Registration '$app_name'..."
      APP_ID=$(az ad app create \
        --display-name "$app_name" \
        --sign-in-audience AzureADMyOrg \
        --query appId -o tsv 2>/dev/null)
      [ -z "$APP_ID" ] && die "Failed to create App Registration."
      ok "Created: $APP_ID"

      info "Creating client secret (2-year expiry)..."
      APP_PASSWORD=$(az ad app credential reset \
        --id "$APP_ID" --years 2 \
        --query password -o tsv 2>/dev/null)
      [ -z "$APP_PASSWORD" ] && die "Failed to create client secret."
      ok "Secret created."
    fi

    step_hdr "Apply credentials"
    azd env set BOT_APP_SECRET "$APP_PASSWORD"
    ok "BOT_APP_SECRET saved to AZD env"
  fi

  local image; image=$(az containerapp show -n "$API_APP" -g "$RG" \
    --query "properties.template.containers[0].image" -o tsv 2>/dev/null)

  info "Updating API container (creates new revision)..."
  az containerapp update -n "$API_APP" -g "$RG" \
    --image "$image" \
    --set-env-vars \
      "MICROSOFT_APP_ID=${APP_ID}" \
      "MICROSOFT_APP_PASSWORD=${APP_PASSWORD}" \
    --revision-suffix "botinit$(date +%s)" \
    --output none
  ok "Container updated with bot credentials."

  step_hdr "Bot Service endpoint"
  local bot_name; bot_name=$(az resource list -g "$RG" \
    --resource-type Microsoft.BotService/botServices \
    --query "[0].name" -o tsv 2>/dev/null || true)
  [ -z "$bot_name" ] || [ "$bot_name" = "null" ] && die "Bot Service not found in $RG."

  local fqdn; fqdn=$(az containerapp show -n "$API_APP" -g "$RG" \
    --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null)

  az bot update -g "$RG" -n "$bot_name" \
    --endpoint "https://${fqdn}/api/messages" \
    --output none
  ok "Bot endpoint: https://${fqdn}/api/messages"
}

# ── Phase 5: Teams App Install ────────────────────────────────────────────────
phase_5() {
  phase_hdr "5/6" "Teams App Install" "~5 min"

  # Resolve APP_ID if not set by phase_4 (--from 5 case)
  if [ -z "$APP_ID" ]; then
    RG=$(get_env AZURE_RESOURCE_GROUP)
    [ -z "$RG" ] && die "AZURE_RESOURCE_GROUP not set — complete Phase 3 first."
    API_APP=$(az containerapp list -g "$RG" \
      --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || true)
    APP_ID=$(az containerapp show -n "$API_APP" -g "$RG" \
      --query "properties.template.containers[0].env[?name=='MICROSOFT_APP_ID'].value | [0]" \
      -o tsv 2>/dev/null || true)
    [ -z "$APP_ID" ] || [ "$APP_ID" = "null" ] && \
      APP_ID=$(ask "App Registration ID (appId) — needed to update the manifest")
  fi

  step_hdr "Update manifest.json"
  info "Setting id and botId to $APP_ID ..."
  local api_fqdn=""
  if [ -n "${RG:-}" ]; then
    local api_app
    api_app=$(az containerapp list -g "$RG" \
      --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || true)
    [ -n "$api_app" ] && api_fqdn=$(az containerapp show -n "$api_app" -g "$RG" \
      --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null || true)
  fi
  python3 - <<PYEOF
import json, sys
with open('teams-app/manifest.json') as f:
    d = json.load(f)
d['id'] = '${APP_ID}'
if d.get('bots'):
    d['bots'][0]['botId'] = '${APP_ID}'
fqdn = '${api_fqdn}'
if fqdn:
    d['validDomains'] = [fqdn]
with open('teams-app/manifest.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
PYEOF
  ok "manifest.json updated."

  step_hdr "Rebuild app package"
  ( cd teams-app && zip -j pubhealth-rfp-bot.zip manifest.json color.png outline.png -q )
  ok "teams-app/pubhealth-rfp-bot.zip rebuilt."

  step_hdr "Upload to Teams"
  echo ""
  info "This step is manual — Teams does not have an upload API."
  echo ""
  printf "  ${BLD}Option A — Teams Admin Center (requires admin):${NC}\n"
  printf "    1. Go to: https://admin.teams.microsoft.com/policies/manage-apps\n"
  printf "    2. Click 'Upload new app'\n"
  printf "    3. Select: %s/teams-app/pubhealth-rfp-bot.zip\n" "$(pwd)"
  echo ""
  printf "  ${BLD}Option B — Sideload (if custom app upload is enabled):${NC}\n"
  printf "    1. In Teams: Apps → Manage your apps → Upload an app\n"
  printf "    2. Select: %s/teams-app/pubhealth-rfp-bot.zip\n" "$(pwd)"
  echo ""

  press_enter
  ok "Teams app install — confirmed by user."
}

# ── Phase 6: SharePoint (optional) ────────────────────────────────────────────
phase_6() {
  phase_hdr "6/6" "SharePoint Access (optional)" "~5 min"

  echo "  The 'Approve & Save to SharePoint' button requires the managed identity"
  echo "  to have Sites.ReadWrite.All on Microsoft Graph."
  echo ""

  if ! ask_yn "Configure SharePoint now?"; then
    warn "Skipped. The approval button will return an error until SharePoint is configured."
    info "Run this script again with --from 6 when ready."
    return
  fi

  RG=$(get_env AZURE_RESOURCE_GROUP)
  [ -z "$RG" ] && die "AZURE_RESOURCE_GROUP not set — complete Phase 3 first."
  API_APP=$(az containerapp list -g "$RG" \
    --query "[?tags.\"azd-service-name\"=='api'].name | [0]" -o tsv 2>/dev/null || true)
  local image; image=$(az containerapp show -n "$API_APP" -g "$RG" \
    --query "properties.template.containers[0].image" -o tsv 2>/dev/null)

  step_hdr "Assign Sites.ReadWrite.All to managed identity"
  local mi_oid; mi_oid=$(az identity list -g "$RG" --query "[0].principalId" -o tsv 2>/dev/null || true)
  [ -z "$mi_oid" ] && die "Managed identity not found in $RG."

  local graph_sp; graph_sp=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 \
    --query id -o tsv 2>/dev/null)
  local role_id="9492366f-7969-46a4-8d15-ed1a20078fff"

  az rest --method POST \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/${mi_oid}/appRoleAssignments" \
    --body "{\"principalId\":\"${mi_oid}\",\"resourceId\":\"${graph_sp}\",\"appRoleId\":\"${role_id}\"}" \
    --output none 2>/dev/null \
    && ok "Sites.ReadWrite.All assigned to managed identity." \
    || warn "Role assignment returned an error — it may already be assigned."

  step_hdr "SharePoint site"

  # Check azd env for previously saved values
  local stored_site_id; stored_site_id=$(get_env SHAREPOINT_SITE_ID)
  local stored_library; stored_library=$(get_env SHAREPOINT_DRAFT_LIBRARY)

  if [ -n "$stored_site_id" ]; then
    ok "SHAREPOINT_SITE_ID already in azd env: $stored_site_id"
    site_id="$stored_site_id"
    library="${stored_library:-Shared Documents}"
  else
    local sp_host; sp_host=$(ask "SharePoint hostname" "gravitonlabs.sharepoint.com")
    local sp_path; sp_path=$(ask "Site path (leave blank for root site)" "")

    local graph_url
    if [ -z "$sp_path" ]; then
      graph_url="https://graph.microsoft.com/v1.0/sites/${sp_host}"
    else
      graph_url="https://graph.microsoft.com/v1.0/sites/${sp_host}:${sp_path}"
    fi

    info "Looking up site ID via Graph API..."
    local site_id; site_id=$(az rest --method GET \
      --url "$graph_url" \
      --query id -o tsv 2>/dev/null || true)

    if [ -z "$site_id" ]; then
      err "Could not retrieve site ID. Check the hostname and path."
      info "You can set it manually later:"
      info "  azd env set SHAREPOINT_SITE_ID <id>"
      info "  bash scripts/install.sh --from 6"
      return
    fi
    ok "Site ID: $site_id"

    local library; library=$(ask "Draft document library name" "Shared Documents")

    azd env set SHAREPOINT_SITE_ID "$site_id"
    azd env set SHAREPOINT_DRAFT_LIBRARY "$library"
    ok "Saved to azd env"
  fi

  az containerapp update -n "$API_APP" -g "$RG" \
    --image "$image" \
    --set-env-vars \
      "SHAREPOINT_SITE_ID=${site_id}" \
      "SHAREPOINT_DRAFT_LIBRARY=${library}" \
    --revision-suffix "spinit$(date +%s)" \
    --output none
  ok "SharePoint configured: '$library' in site $site_id"
}

# ── Verify & first prompt ─────────────────────────────────────────────────────
finish() {
  echo ""
  printf "${BLD}${CYN}"
  printf "═%.0s" {1..52}
  printf "${NC}\n"
  printf "${BLD}${GRN}  Setup complete${NC}\n"
  printf "${BLD}${CYN}"
  printf "═%.0s" {1..52}
  printf "${NC}\n\n"

  if ask_yn "Run verify-setup.sh now?" "y"; then
    echo ""
    bash "$(dirname "$0")/verify-setup.sh" || true
  fi

  echo ""
  printf "${BLD}Send your first prompt in Teams:${NC}\n\n"
  printf "  Draft a 24-month influenza surveillance RFP. CDC funding, \$2.5M total,\n"
  printf "  awards between \$150K–\$400K, no cost share required.\n\n"
  info "Sections appear live in the card as each of 8 completes (~2–3 min)."
  info "When the gate passes, click 'Approve & Save to SharePoint' to upload."
  echo ""
}

# ── Entry point ───────────────────────────────────────────────────────────────
main() {
  local start_phase=1

  # Parse --from flag
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --from)
        start_phase="${2:-1}"
        shift 2
        ;;
      --from=*)
        start_phase="${1#--from=}"
        shift
        ;;
      *)
        shift
        ;;
    esac
  done

  # Ensure we're in the repo root
  if [ ! -f "azure.yaml" ] && [ ! -f "azd.yaml" ] && [ ! -d "src" ]; then
    die "Run this script from the repository root (public_health_rfp_gen/)."
  fi

  clear
  printf "\n${BLD}Public Health RFP Generator${NC}\n"
  printf "${DIM}Guided Installer · Azure AI${NC}\n\n"
  printf "  Phases: Prerequisites → Auth → Deploy → Bot → Teams → SharePoint\n"
  printf "  Total:  ~50 min  |  Azure cost: ~\$0.50 to provision\n"

  if [ "$start_phase" -gt 1 ]; then
    echo ""
    warn "Resuming from Phase $start_phase."
  fi

  echo ""
  if ! ask_yn "Ready to begin?"; then
    echo "  Exiting."
    exit 0
  fi

  [ "$start_phase" -le 1 ] && phase_1
  [ "$start_phase" -le 2 ] && phase_2
  [ "$start_phase" -le 3 ] && phase_3
  [ "$start_phase" -le 4 ] && phase_4
  [ "$start_phase" -le 5 ] && phase_5
  [ "$start_phase" -le 6 ] && phase_6

  finish
}

main "$@"
