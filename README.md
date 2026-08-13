# Public Health RFP Generator

> An open-source Azure AI accelerator for automating the generation, evaluation, and governance of government grant RFPs in public health settings.

[![Azure](https://img.shields.io/badge/Azure-AI%20Foundry-0078D4?logo=microsoftazure)](https://azure.microsoft.com/en-us/products/ai-foundry)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Documentation

| Audience | Document |
|---|---|
| Program Officers, Grants Administrators, Compliance Officers | [Business Overview](https://gravlabs.github.io/public_health_rfp_gen/business.html) |
| Engineers and Cloud Architects | [Technical Architecture Reference](https://gravlabs.github.io/public_health_rfp_gen/technical.html) |

---

## What It Does

Program officers describe an RFP in plain language in Microsoft Teams. The platform retrieves relevant historical awards, generates all 8 required sections via a fine-tuned GPT-4o model, and runs a 5-dimension evaluation gate before writing the draft to SharePoint. The full cycle takes minutes instead of weeks.

Six capabilities are supported: RFP drafting, quality gate, proposal review, program classification, regulatory watch, and budget audit — all accessible from Teams without leaving the tools your team already uses.

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
Download from https://www.docker.com/products/docker-desktop and install. Required to build the API and orchestrator container images.

```bash
# Verify
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

# Optional: load from .env file (copy .env.example and fill in values)
# ./scripts/load-env-vars.sh .env

# Deploy (provision + build containers + post-provision hook ~20 min)
azd up
```

`azd up` runs in three phases:

1. **Provision** — creates all Azure resources via Bicep (~15 min)
2. **Deploy** — builds and pushes Docker images for the API and orchestrator
3. **Post-provision hook** — uploads the 50-RFP corpus, creates the AI Search index, runs the ingestion pipeline, writes `.env`

### What `azd up` provisions

| Resource | Name pattern | Purpose |
|---|---|---|
| Resource Group | `rg-dev-pubhealth-rfp-*` | Container for all resources |
| User-Assigned Identity | `id-pubhealth-*` | Keyless auth across all services |
| Key Vault | `kvph*` | Secrets storage |
| Storage Account | `stpubhealth*` | ADLS Gen2 for RFP corpus |
| ML Storage Account | `stml*` | Standard storage for AI Foundry |
| Azure OpenAI | `cog-pubhealth-oai-*` | GPT-4o + text-embedding-3-large |
| Document Intelligence | `cog-pubhealth-di-*` | PDF/DOCX parsing |
| AI Search | `srch-pubhealth-*` | Hybrid + semantic search index |
| Log Analytics | `log-pubhealth-*` | Centralized logs |
| Application Insights | `appi-pubhealth-*` | Request tracing + metrics |
| API Management | `apim-pubhealth-*` | AI gateway (Consumption SKU) |
| Container Registry | `crpubhealth*` | Docker image storage |
| Container Apps Environment | `cae-pubhealth-*` | Hosting for API + orchestrator |
| AI Foundry Hub + Project | `mlw-pubhealth-hub/rfp-*` | AI Foundry project workspace |
| Budget Alert | `pubhealth-rfp-poc-budget` | $500/mo spend guard |

---

## What Is Not Yet Set Up

These items require manual steps after `azd up` completes:

### 1. Teams Bot (deferred)

The bot service Bicep was removed because a prior partial deployment left the ARM resource in a conflicted state. To set it up:

```bash
# Option A: delete the stuck resource, then re-enable in main.bicep
az bot delete \
  --name "bot-pubhealth-rfp-<resourceToken>" \
  --resource-group "rg-dev-pubhealth-rfp-<resourceToken>"

# Then uncomment the botService module in infra/main.bicep and re-run:
azd up
```

```bash
# Option B: create manually via portal
# Azure Portal → Create a resource → Azure Bot
# Type: UserAssignedMSI, App ID: (client ID from azd env get-value AZURE_CLIENT_ID)
# Enable Teams channel after creation
```

### 2. AI Foundry Connections

The hub was provisioned but OpenAI and AI Search connections must be wired manually (Bicep removed them because the `authType: 'ManagedIdentity'` credential body requires extra plumbing):

1. Open [AI Foundry Studio](https://ai.azure.com)
2. Select the `mlw-pubhealth-hub-*` hub
3. Go to **Settings → Connections → New connection**
4. Add Azure OpenAI: endpoint from `azd env get-value AZURE_OPENAI_ENDPOINT`
5. Add Azure AI Search: endpoint from `azd env get-value AZURE_SEARCH_ENDPOINT`
6. Set auth type to **Managed Identity** on both

### 3. Additional OpenAI Models (eastus gap)

`gpt-4o-mini` and `o3-mini` are not available in `eastus`. They are referenced in the `.env` file but no deployment exists. Options:

```bash
# Option A: switch region (requires full redeploy)
azd env set AZURE_LOCATION eastus2
azd up

# Option B: wait — Microsoft is expanding eastus availability
# Option C: use swedencentral or westeurope
azd env set AZURE_LOCATION swedencentral
azd up
```

### 4. Fabric Setup (optional — requires 60-day trial activation)

```bash
python fabric/setup.py \
  --workspace-name pubhealth-rfp-poc \
  --ai-search-endpoint "$(azd env get-value AZURE_SEARCH_ENDPOINT)" \
  --sharepoint-site-id "<YOUR_SITE_ID>"
```

### 5. APIM Advanced Policies (requires Standard v2 tier)

Token limiting, semantic caching, and token metric emission are not available in the Consumption SKU. To enable:

1. Upgrade APIM to Standard v2 (~$140/mo) in the Azure Portal
2. Restore the full policy XML from git history (commit before `e144e0e`)

---

## Local Development

After `azd up` completes, `.env` is written to the project root. Use it for local runs.

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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export AZURE_SEARCH_ENDPOINT="$(azd env get-value AZURE_SEARCH_ENDPOINT)"
export AZURE_OPENAI_ENDPOINT="$(azd env get-value AZURE_OPENAI_ENDPOINT)"
export AZURE_STORAGE_ACCOUNT="$(azd env get-value AZURE_STORAGE_ACCOUNT)"

python create_index.py     # create/update search index schema
python pipeline.py         # ingest all blobs from rfp-corpus container
python pipeline.py --file "2024/cdc-epi.md"   # ingest a single file
```

### Evaluation gate

```bash
cd src/evaluation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python gate.py --draft-file <path-to-draft.md>
```

### Golden dataset tools

```bash
# Annotate drafts interactively
python src/golden/annotator_cli.py \
  --draft-dir data/eval-examples \
  --output-dir data/golden-annotated \
  --annotator-id reviewer-1

# Build eval pairs + calibration report
python src/golden/eval_pairs.py \
  --annotated-dir data/golden-annotated \
  --output-dir data/golden-dataset
```

### Tests

```bash
cd tests
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt
pytest -v
```

---

## Environment Variables

`azd up` writes `.env` to the project root after provisioning. To reload it into the AZD environment:

```bash
./scripts/load-env-vars.sh .env
```

Key variables:

| Variable | Source | Used by |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | OpenAI resource | API, ingestion, evaluation |
| `AZURE_SEARCH_ENDPOINT` | AI Search resource | Ingestion, API |
| `AZURE_STORAGE_ACCOUNT` | Storage account name | Post-provision, ingestion |
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | Doc Intelligence | Ingestion pipeline |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights | API, orchestrator |
| `AZURE_APIM_GATEWAY_URL` | APIM gateway | API clients |
| `AZURE_AI_FOUNDRY_PROJECT_ENDPOINT` | AI Foundry | Agents |

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
│   ├── post-provision.sh    # azd post-provision hook
│   └── load-env-vars.sh     # Load .env into azd environment
├── src/
│   ├── agents/              # Azure AI Foundry Agent Service
│   │   └── coordinator.py   # Coordinator + 5 specialist agents
│   ├── api/                 # FastAPI service
│   ├── bot/                 # Teams Bot Framework
│   ├── evaluation/          # 5-dimension evaluation gate
│   ├── golden/              # Golden dataset tooling
│   ├── ingestion/           # AI Search indexing pipeline
│   ├── orchestrator/        # Semantic Kernel (.NET)
│   ├── promptflow/          # Prompt Flow DAG
│   └── training/            # Fine-tuning data prep
└── tests/                   # Pytest suite
```

---

## Adapting for Your Organization

1. **Replace the corpus** — swap `data/sample-rfps/` with your historical RFP documents and re-run the ingestion pipeline
2. **Tune thresholds** — edit the `THRESHOLD` constant in each evaluator under `src/evaluation/`
3. **Add compliance rules** — extend `src/evaluation/compliance.py` with required phrases or prohibited terms
4. **Change program taxonomy** — update the taxonomy in `src/agents/` and `src/orchestrator/`

---

## Contributing

Pull requests welcome. For major changes, open an issue first. Areas where contributions are especially useful:

- Additional evaluator dimensions (readability, format compliance)
- PDF/DOCX corpus ingestion support
- Power BI report templates for the Fabric Delta tables
- Additional synthetic RFP examples for new program areas

---

## License

MIT — see [LICENSE](LICENSE) for details.
