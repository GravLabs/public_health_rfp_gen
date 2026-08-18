# Public Health RFP Generator

> An Azure AI accelerator for automating the generation, evaluation, and governance of government grant RFPs in public health laboratory settings.

[![Azure](https://img.shields.io/badge/Azure-AI%20Foundry-0078D4?logo=microsoftazure)](https://azure.microsoft.com/en-us/products/ai-foundry)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Documentation

| Audience | Document |
|---|---|
| Anyone setting up for the first time | [Setup Quickstart](https://gravlabs.github.io/public_health_rfp_gen/quickstart.html) |
| Program Officers, Grants Administrators, Compliance Officers | [Business Overview](https://gravlabs.github.io/public_health_rfp_gen/business.html) |
| Engineers and Cloud Architects | [Technical Architecture Reference](https://gravlabs.github.io/public_health_rfp_gen/technical.html) |

---

## What It Does

Program officers describe an RFP in plain language in Microsoft Teams. The platform retrieves relevant historical awards from AI Search, generates all 8 required sections via GPT-4o, and runs a 5-dimension evaluation gate before the user approves it for upload to SharePoint as a formatted Word document. The full cycle takes minutes instead of weeks.

**Five bot capabilities** — all accessible from Teams:

| Capability | Sample prompt |
|---|---|
| **Generate RFP** | `Draft an influenza surveillance RFP, CDC, $2.5M, 24 months` |
| **Classify program area** | `Classify: whole genome sequencing surveillance program` |
| **Review proposal** | Paste or attach a DOCX/PDF proposal |
| **Budget audit** | Paste a budget narrative with a dollar amount |
| **Regulatory watch** | `Any recent CFR changes affecting public health labs?` |

After an RFP passes the evaluation gate, an **Approve & Save to SharePoint** button appears in the card. Clicking it uploads a properly-formatted Word document (headings, bullet lists, tables) to the `GeneratedDrafts` folder in SharePoint and returns a direct link.

---

## Prerequisites

Install all tools before running `azd up`. On Windows, use PowerShell unless otherwise noted.

### Azure CLI
```bash
# Linux/macOS
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Windows (PowerShell)
winget install Microsoft.AzureCLI

# Verify
az version   # need >= 2.60
```

### Azure Developer CLI (azd)
```bash
# Linux/macOS
curl -fsSL https://aka.ms/install-azd.sh | bash

# Windows (PowerShell)
winget install Microsoft.Azd

# Verify
azd version   # need >= 1.9
```

### .NET SDK 8
```bash
# Linux
wget https://dot.net/v1/dotnet-install.sh && bash dotnet-install.sh --channel 8.0

# Windows
winget install Microsoft.DotNet.SDK.8

# Verify
dotnet --version   # need 8.x
```

### Python 3.12
```bash
# Ubuntu/Debian
sudo apt install python3.12 python3.12-venv python3-pip -y

# macOS
brew install python@3.12

# Windows
winget install Python.Python.3.12

# Verify
python3 --version   # need 3.12.x
```

### Docker Desktop
Download from https://www.docker.com/products/docker-desktop and install. Required to build container images.

```bash
docker info   # must show server running
```

---

## Deploy to Azure

```bash
git clone https://github.com/GravLabs/public_health_rfp_gen.git
cd public_health_rfp_gen

# Authenticate
az login
azd auth login

# Create environment and set required variables
azd env new pubhealth-rfp-poc
azd env set AZURE_LOCATION eastus
azd env set APIM_PUBLISHER_EMAIL "your-email@example.com"
azd env set OWNER_EMAIL "your-email@example.com"

# Deploy (provision + build containers + post-provision hook ~20 min)
azd up
```

`azd up` runs in three phases:

1. **Provision** — creates all Azure resources via Bicep (~15 min)
2. **Deploy** — builds and pushes Docker images for the API and orchestrator
3. **Post-provision hook** — uploads the 50-RFP corpus, creates the AI Search index, runs ingestion, writes `.env`, wires AI Foundry env vars, restores bot App ID

> **After `azd up`**: Use `azd deploy` (not `azd provision`) for all subsequent code changes. Running `azd provision` re-runs Bicep and may reset secure params; the `post-provision.sh` hook restores the bot App ID automatically, but it is slower than `azd deploy`.

### What `azd up` provisions

| Resource | Name pattern | Purpose |
|---|---|---|
| Resource Group | `rg-dev-pubhealth-rfp-*` | Container for all resources |
| User-Assigned Identity | `id-pubhealth-*` | Keyless auth across all services |
| Key Vault | `kvph*` | Secrets storage |
| Storage Account | `stpubhealth*` | ADLS Gen2 for RFP corpus |
| ML Storage Account | `stml*` | Standard storage for AI Foundry |
| Azure OpenAI | `cog-pubhealth-oai-*` | GPT-4o, gpt-4o-mini, text-embedding-3-small |
| Document Intelligence | `cog-pubhealth-di-*` | PDF/DOCX parsing |
| AI Search | `srch-pubhealth-*` | Hybrid + semantic search index |
| Log Analytics | `log-pubhealth-*` | Centralized logs |
| Application Insights | `appi-pubhealth-*` | Request tracing + metrics |
| API Management | `apim-pubhealth-*` | AI gateway (Consumption SKU) |
| Container Registry | `crpubhealth*` | Docker image storage |
| Container Apps Environment | `cae-pubhealth-*` | Hosting for API + orchestrator |
| AI Foundry Hub + Project | `mlw-pubhealth-hub/rfp-*` | AI Foundry project workspace |
| Bot Service | `bot-pubhealth-rfp-*` | Teams channel relay |
| Budget Alert | `pubhealth-rfp-poc-budget` | $500/mo spend guard |

---

## Teams Bot Setup

The bot requires a one-time App Registration and manifest install that is not automated by `azd up`.

### 1. Create App Registration

```bash
# Create a new App Registration for the bot
az ad app create --display-name "pubhealth-rfp-bot" \
  --sign-in-audience AzureADMyOrg

# Note the appId from the output, then create a secret:
az ad app credential reset --id <appId> --years 2
# Note the password from the output
```

### 2. Set Bot Credentials

```bash
azd env set BOT_APP_SECRET "<password-from-above>"

# Then update the API container with the bot credentials
RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP)
API_APP_NAME=$(az containerapp list -g $RESOURCE_GROUP \
  --query "[?tags.\"azd-service-name\"=='api'].name|[0]" -o tsv)
CURRENT_IMAGE=$(az containerapp show -n $API_APP_NAME -g $RESOURCE_GROUP \
  --query "properties.template.containers[0].image" -o tsv)

az containerapp update -n $API_APP_NAME -g $RESOURCE_GROUP \
  --image "$CURRENT_IMAGE" \
  --set-env-vars \
    "MICROSOFT_APP_ID=<appId>" \
    "MICROSOFT_APP_PASSWORD=<password>" \
  --revision-suffix botinit --output none
```

### 3. Update Bot Service

```bash
BOT_NAME=$(az bot show -g $RESOURCE_GROUP --query "name" -o tsv 2>/dev/null || \
           az resource list -g $RESOURCE_GROUP --resource-type Microsoft.BotService/botServices \
             --query "[0].name" -o tsv)
API_FQDN=$(az containerapp show -n $API_APP_NAME -g $RESOURCE_GROUP \
  --query "properties.configuration.ingress.fqdn" -o tsv)

az bot update -g $RESOURCE_GROUP -n $BOT_NAME \
  --endpoint "https://$API_FQDN/api/messages"
```

### 4. Install the Teams App

```bash
cd teams-app
# Update manifest.json: set "id" and "botId" to your appId
# Then rebuild the ZIP:
zip -j pubhealth-rfp-bot.zip manifest.json color.png outline.png
```

Upload `pubhealth-rfp-bot.zip` via **Teams Admin Center → Manage apps → Upload** or via **Teams → Apps → Upload a custom app**.

### 5. Grant SharePoint Permission

For the Approve & Save to SharePoint flow, the managed identity needs Graph access:

```bash
MI_OBJECT_ID=$(az identity show -g $RESOURCE_GROUP \
  --name "id-pubhealth-*" --query principalId -o tsv)
GRAPH_SP=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)
ROLE_ID="9492366f-7969-46a4-8d15-ed1a20078fff"  # Sites.ReadWrite.All

az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$MI_OBJECT_ID/appRoleAssignments" \
  --body "{\"principalId\":\"$MI_OBJECT_ID\",\"resourceId\":\"$GRAPH_SP\",\"appRoleId\":\"$ROLE_ID\"}"
```

Then set the SharePoint site ID on the container:

```bash
az containerapp update -n $API_APP_NAME -g $RESOURCE_GROUP \
  --image "$CURRENT_IMAGE" \
  --set-env-vars \
    "SHAREPOINT_SITE_ID=<tenant>.sharepoint.com,<site-guid>,<web-guid>" \
    "SHAREPOINT_DRAFT_LIBRARY=Generated Drafts" \
  --revision-suffix sharepointinit --output none
```

---

## Manual Steps After `azd up`

### AI Foundry Connections (optional — for fine-tuning and eval runs)

The hub is provisioned. To wire OpenAI and AI Search connections for use in AI Foundry Studio:

1. Open [AI Foundry Studio](https://ai.azure.com)
2. Select the `mlw-pubhealth-hub-*` hub
3. Go to **Settings → Connections → New connection**
4. Add Azure OpenAI: endpoint from `azd env get-value AZURE_OPENAI_ENDPOINT`
5. Add Azure AI Search: endpoint from `azd env get-value AZURE_SEARCH_ENDPOINT`
6. Set auth type to **Managed Identity** on both

### Fabric Setup (optional — requires 60-day trial activation)

```bash
python fabric/setup.py \
  --workspace-name pubhealth-rfp-poc \
  --ai-search-endpoint "$(azd env get-value AZURE_SEARCH_ENDPOINT)" \
  --sharepoint-site-id "<YOUR_SITE_ID>"
```

### APIM Advanced Policies (requires Standard v2 tier)

Token limiting, semantic caching, and token metric emission are not available in the Consumption SKU. To enable:

1. Upgrade APIM to Standard v2 (~$140/mo) in the Azure Portal
2. Restore the full policy XML from git history (commit before `e144e0e`)

---

## Teardown

```bash
# Linux/macOS
./scripts/teardown.sh

# Windows (PowerShell)
.\scripts\teardown.ps1
```

Runs `azd down --force --purge`, purges soft-deleted Cognitive Services accounts, and optionally removes local `.azure/<env>` state.

> After teardown, re-create the environment before redeploying:
> ```bash
> azd env new <env-name>
> azd env set AZURE_LOCATION eastus
> azd env set APIM_PUBLISHER_EMAIL your@email.com
> azd env set OWNER_EMAIL your@email.com
> azd up
> ```

---

## Local Development

After `azd up`, `.env` is written to the project root. Source it before local runs.

### API (FastAPI)

```bash
cd src/api
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Orchestrator (.NET Semantic Kernel)

```bash
cd src/orchestrator
dotnet restore
dotnet run
```

### Ingestion pipeline (re-index)

```bash
cd src/ingestion
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AZURE_SEARCH_ENDPOINT="$(azd env get-value AZURE_SEARCH_ENDPOINT)"
export AZURE_OPENAI_ENDPOINT="$(azd env get-value AZURE_OPENAI_ENDPOINT)"
export AZURE_STORAGE_ACCOUNT="$(azd env get-value AZURE_STORAGE_ACCOUNT)"

python create_index.py     # create/update search index schema
python pipeline.py         # ingest all blobs from rfp-corpus container
```

### Evaluation gate

```bash
cd src/evaluation
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python gate.py --draft-file <path-to-draft.md>
```

### Tests

```bash
cd tests
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest -v
```

---

## Environment Variables

`azd up` writes `.env` to the project root. Key variables:

| Variable | Source | Used by |
|---|---|---|
| `AZURE_SUBSCRIPTION_ID` | Selected subscription | azd provision target |
| `AZURE_OPENAI_ENDPOINT` | OpenAI resource | API, ingestion, evaluation |
| `AZURE_OPENAI_GPT_DEPLOYMENT` | `gpt-4o` | API, orchestrator |
| `AZURE_OPENAI_MINI_DEPLOYMENT` | `gpt-4o-mini` | Evaluators, classification |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `text-embedding-3-small` | Ingestion |
| `AZURE_SEARCH_ENDPOINT` | AI Search resource | Ingestion, orchestrator |
| `AZURE_STORAGE_ACCOUNT` | Storage account name | Post-provision, ingestion |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights | API, orchestrator |
| `AZURE_AI_FOUNDRY_PROJECT_ENDPOINT` | `https://eastus.api.azureml.ms` | AI Foundry tracing |
| `AZURE_AI_FOUNDRY_PROJECT_NAME` | `mlw-pubhealth-rfp-*` | AI Foundry project |
| `MICROSOFT_APP_ID` | Bot App Registration | Bot auth |
| `MICROSOFT_APP_PASSWORD` | Bot App Registration secret | Bot auth |
| `SHAREPOINT_SITE_ID` | SharePoint site | Draft upload |
| `SHAREPOINT_DRAFT_LIBRARY` | `Generated Drafts` | Draft upload |

---

## Repository Structure

```
.
├── data/
│   ├── sample-rfps/         # 50 synthetic RFP documents (Markdown)
│   ├── eval-examples/       # Labeled good/bad evaluation examples
│   └── golden-annotated/    # Human-annotated golden dataset (generated)
├── docs/
│   ├── business.html        # Business overview (GitHub Pages)
│   └── technical.html       # Technical architecture reference (GitHub Pages)
├── fabric/
│   ├── setup.py             # Fabric workspace provisioner
│   └── eval_analytics.ipynb # Spark notebook → 6 Power BI Delta tables
├── infra/
│   ├── main.bicep           # Root Bicep (subscription scope)
│   └── modules/             # 14 modules (identity, openai, ai-search,
│                            #   ai-foundry, container-apps, apim,
│                            #   bot-service, monitoring, keyvault,
│                            #   storage, doc-intelligence, budget, ...)
├── scripts/
│   ├── post-provision.sh    # azd post-provision hook (7 steps)
│   ├── post-deploy.sh       # azd post-deploy hook (bot endpoint + password)
│   └── load-env-vars.sh     # Load .env into azd environment
├── src/
│   ├── api/                 # FastAPI service + Teams bot handler
│   │   ├── main.py          # 10 endpoints + /export SharePoint upload
│   │   ├── bot/bot.py       # 5 capability handlers + Adaptive Card builders
│   │   └── sharepoint_client.py  # Graph API: upload DOCX with MD→Word conversion
│   ├── evaluation/          # 5-dimension evaluation gate (no SDK dependency)
│   │   ├── gate.py          # Orchestrator — PASS/FAIL decision
│   │   └── evaluators/      # completeness, param_accuracy, compliance,
│   │                        #   groundedness, coherence (direct httpx calls)
│   ├── orchestrator/        # Semantic Kernel (.NET) — 8-section streaming generator
│   ├── ingestion/           # AI Search indexing pipeline
│   ├── agents/              # Azure AI Foundry Agent Service
│   └── training/            # Fine-tuning data prep
├── teams-app/
│   ├── manifest.json        # Teams app manifest (supportsFiles: true)
│   └── pubhealth-rfp-bot.zip
└── tests/                   # Pytest suite
```

---

## Adapting for Your Organization

1. **Replace the corpus** — swap `data/sample-rfps/` with your historical RFP documents and re-run the ingestion pipeline
2. **Tune thresholds** — edit the `THRESHOLDS` dict in `src/evaluation/gate.py`
3. **Add compliance rules** — extend `src/evaluation/evaluators/compliance.py` with required phrases or prohibited terms
4. **Change program taxonomy** — update `_TAXONOMY` in `src/api/main.py` and the orchestrator system prompt

---

## Contributing

Pull requests welcome. For major changes, open an issue first. Areas where contributions are especially useful:

- Additional evaluator dimensions (readability, format compliance)
- Power BI report templates for the Fabric Delta tables
- Additional synthetic RFP examples for new program areas

---

## License

MIT — see [LICENSE](LICENSE) for details.
