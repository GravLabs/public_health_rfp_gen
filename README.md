# Public Health RFP Generator

> An Azure AI accelerator for automating the generation, evaluation, and governance of government grant RFPs in public health laboratory settings.

[![Azure](https://img.shields.io/badge/Azure-AI%20Foundry-0078D4?logo=microsoftazure)](https://azure.microsoft.com/en-us/products/ai-foundry)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Documentation

| Audience | Document |
|---|---|
| First-time setup | [Setup Quickstart](https://gravlabs.github.io/public_health_rfp_gen/quickstart.html) |
| Program Officers, Grants Administrators | [Business Overview](https://gravlabs.github.io/public_health_rfp_gen/business.html) |
| Engineers and Cloud Architects | [Technical Architecture Reference](https://gravlabs.github.io/public_health_rfp_gen/technical.html) |

---

## What It Does

Program officers describe an RFP in plain language in Microsoft Teams. The platform retrieves relevant historical awards from AI Search, generates all 8 required sections via GPT-4o, and runs a 5-dimension evaluation gate. When the draft passes, the user clicks **Approve & Save to SharePoint** — the bot assembles a formatted Word document and uploads it directly. The full cycle takes minutes instead of weeks.

**Five capabilities, all from Teams:**

| Capability | Sample prompt |
|---|---|
| **Generate RFP** | `Draft an influenza surveillance RFP, CDC, $2.5M, 24 months` |
| **Classify program area** | `Classify: whole genome sequencing surveillance program` |
| **Review proposal** | Paste or attach a DOCX/PDF proposal |
| **Budget audit** | Paste a budget narrative with a dollar amount |
| **Regulatory watch** | `Any recent CFR changes affecting public health labs?` |

---

## Install

**Prerequisites:** az CLI ≥ 2.60, azd ≥ 1.9, .NET SDK 8, Python 3.12, Docker Desktop.

### Guided (recommended)

```bash
git clone https://github.com/GravLabs/public_health_rfp_gen.git
cd public_health_rfp_gen
bash scripts/install.sh
```

Walks through all six phases interactively: prerequisites check → login & subscription → `azd up` → bot credentials → Teams app upload → SharePoint access. Resume from any phase with `--from N`.

### Manual

```bash
az login && azd auth login

az account set --subscription "<subscription-id>"

azd env new pubhealth-rfp-poc
azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
azd env set AZURE_LOCATION eastus
azd env set APIM_PUBLISHER_EMAIL "you@example.com"
azd env set OWNER_EMAIL "you@example.com"

azd up   # ~20 min
```

See [docs/quickstart.html](docs/quickstart.html) for copy-pasteable commands covering all phases including bot setup, Teams manifest, and SharePoint permissions.

> **Use `azd deploy` for code changes, not `azd provision`.** Provision re-runs Bicep and can reset secure params. The post-provision hook restores the bot App ID, but deploy is faster and safer.

---

## What `azd up` Provisions

| Resource | Name pattern | Purpose |
|---|---|---|
| Resource Group | `rg-dev-pubhealth-rfp-*` | Container for all resources |
| User-Assigned Identity | `id-pubhealth-*` | Keyless auth across all services |
| Key Vault | `kvph*` | Secrets storage |
| Storage Account | `stpubhealth*` | ADLS Gen2 for RFP corpus |
| Azure OpenAI | `cog-pubhealth-oai-*` | GPT-4o, gpt-4o-mini, text-embedding-3-small |
| Document Intelligence | `cog-pubhealth-di-*` | PDF/DOCX parsing |
| AI Search | `srch-pubhealth-*` | Hybrid + semantic search index |
| API Management | `apim-pubhealth-*` | AI gateway (Standard v2 SKU) — semantic caching, token budgets, usage metrics, JWT-validated access to the Foundry account |
| Container Registry | `crpubhealth*` | Docker image storage |
| Container Apps Environment | `cae-pubhealth-*` | API + orchestrator hosting |
| AI Foundry Hub + Project | `mlw-pubhealth-hub/rfp-*` | AI Foundry project workspace |
| Bot Service | `bot-pubhealth-rfp-*` | Teams channel relay |
| Log Analytics + App Insights | `log-` / `appi-pubhealth-*` | Logs and traces |
| Budget Alert | `pubhealth-rfp-poc-budget` | $500/mo spend guard |

---

## Teardown

```bash
bash scripts/teardown.sh
```

Runs `azd down --force --purge`, purges soft-deleted Cognitive Services and APIM, deletes the bot's Entra ID App Registration + Service Principal, and optionally removes local `.azure/<env>` state.

---

## Local Development

After `azd up`, `.env` is written to the project root. Source it before local runs.

```bash
# API (FastAPI)
cd src/api && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Orchestrator (.NET)
cd src/orchestrator && dotnet restore && dotnet run

# Re-index corpus
cd src/ingestion && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python create_index.py && python pipeline.py

# Tests
cd tests && pip install -r requirements-test.txt && pytest -v
```

---

## Environment Variables

| Variable | Value / Source | Used by |
|---|---|---|
| `AZURE_SUBSCRIPTION_ID` | Selected subscription ID | azd provision target |
| `AZURE_OPENAI_ENDPOINT` | AI Gateway URL (APIM) for the API/orchestrator containers; raw OpenAI resource for local ingestion tooling | API, orchestrator, evaluation, local ingestion |
| `AZURE_APIM_GATEWAY_URL` | API Management gateway URL | API, orchestrator |
| `AZURE_OPENAI_GPT_DEPLOYMENT` | `gpt-4o` | API, orchestrator |
| `AZURE_OPENAI_MINI_DEPLOYMENT` | `gpt-4o-mini` | Evaluators, classification |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `text-embedding-3-small` | Ingestion |
| `AZURE_SEARCH_ENDPOINT` | AI Search resource | Ingestion, orchestrator |
| `AZURE_STORAGE_ACCOUNT` | Storage account name | Post-provision, ingestion |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights | API, orchestrator |
| `AZURE_AI_FOUNDRY_PROJECT_ENDPOINT` | `https://eastus.api.azureml.ms` | AI Foundry tracing |
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
│   ├── eval-examples/       # Labeled evaluation examples
│   └── golden-annotated/    # Human-annotated golden dataset
├── docs/
│   ├── quickstart.html      # Interactive setup guide (GitHub Pages)
│   ├── business.html        # Business overview (GitHub Pages)
│   └── technical.html       # Technical architecture reference (GitHub Pages)
├── fabric/
│   ├── setup.py             # Fabric workspace provisioner
│   └── eval_analytics.ipynb # Spark notebook → 6 Power BI Delta tables
├── infra/
│   ├── main.bicep           # Root Bicep (subscription scope)
│   └── modules/             # 14 resource modules
├── scripts/
│   ├── install.sh           # Guided interactive installer (all 6 phases)
│   ├── verify-setup.sh      # Post-install health check + smoke test
│   ├── post-provision.sh    # azd post-provision hook (7 steps)
│   ├── post-deploy.sh       # azd post-deploy hook
│   └── teardown.sh          # Full resource teardown
├── src/
│   ├── api/                 # FastAPI + Teams bot handler
│   │   ├── main.py          # Endpoints including POST /export/{draft_id}
│   │   ├── bot/bot.py       # 5 capability handlers + Adaptive Card builders
│   │   └── sharepoint_client.py  # Graph API upload with MD→Word conversion
│   ├── evaluation/          # 5-dimension gate (direct httpx, no SDK)
│   ├── orchestrator/        # Semantic Kernel (.NET) — 8-section streaming
│   ├── ingestion/           # AI Search indexing pipeline
│   └── agents/              # Azure AI Foundry Agent Service
├── teams-app/
│   ├── manifest.json        # Teams app manifest
│   └── pubhealth-rfp-bot.zip
└── tests/                   # Pytest suite
```

---

## POC Limitations

One known limitation is accepted for this POC and documented here for production planning:

| Limitation | Impact | Production fix |
|---|---|---|
| **Draft cache is in-memory** | If the API container restarts between generation and the user clicking "Approve", the draft is lost and the user sees a 404. (The content itself isn't fully gone — every generation and edit is also archived to Fabric OneLake unconditionally — but re-approving to SharePoint after a restart isn't possible without more code.) | Replace `_draft_cache` in `src/api/main.py` with a Redis cache or Cosmos DB item with a short TTL. |

---

## Adapting for Your Organization

1. **Replace the corpus** — swap `data/sample-rfps/` with your RFP documents and re-run ingestion
2. **Tune thresholds** — edit `THRESHOLDS` in `src/evaluation/gate.py`
3. **Add compliance rules** — extend `src/evaluation/evaluators/compliance.py`
4. **Change program taxonomy** — update `_TAXONOMY` in `src/api/main.py`

---

## License

MIT — see [LICENSE](LICENSE) for details.
