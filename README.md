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

## Getting Started

### Prerequisites

| Tool | Version |
|---|---|
| Azure CLI | ≥ 2.60 |
| Azure Developer CLI (azd) | ≥ 1.9 |
| .NET SDK | 8.0 |
| Python | 3.12 |
| Docker Desktop | latest |

### Deploy

```bash
git clone https://github.com/GravLabs/public_health_rfp_gen.git
cd public_health_rfp_gen

az login
azd auth login

azd env new pubhealth-rfp-poc
azd env set AZURE_LOCATION eastus
azd env set MONTHLY_BUDGET_USD 500
azd env set SHAREPOINT_SITE_ID "<your-site-id>"

azd up
```

`azd up` provisions all 12 Azure resources (~15 min), builds and pushes both container images, indexes the 50-RFP corpus into AI Search, and writes `.env` for local development.

### Fabric Setup (optional — after activating 60-day trial)

```bash
python fabric/setup.py \
  --workspace-name pubhealth-rfp-poc \
  --ai-search-endpoint $(azd env get-value AZURE_SEARCH_ENDPOINT) \
  --sharepoint-site-id $(azd env get-value SHAREPOINT_SITE_ID)
```

### Golden Dataset

```bash
# Annotate eval examples interactively
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
cd tests && pip install -r requirements-test.txt && pytest -v
```

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
│   └── modules/             # 12 modules: identity, openai, ai-search,
│                            #   ai-foundry, container-apps, apim,
│                            #   bot-service, monitoring, keyvault,
│                            #   storage, doc-intelligence, budget
├── scripts/
│   └── post-provision.sh    # azd post-provision hook
├── src/
│   ├── agents/              # Azure AI Foundry Agent Service
│   │   └── coordinator.py   # Coordinator + 5 specialist agents
│   ├── api/                 # FastAPI service
│   │   ├── main.py
│   │   ├── sharepoint_client.py
│   │   ├── foundry_client.py
│   │   ├── budget_monitor.py
│   │   └── observability.py
│   ├── bot/                 # Teams Bot Framework
│   │   ├── bot.py           # ActivityHandler
│   │   └── adaptive_cards/
│   ├── evaluation/          # 5-dimension evaluation gate
│   │   ├── gate.py
│   │   ├── groundedness.py
│   │   ├── completeness.py
│   │   ├── parameter_accuracy.py
│   │   ├── compliance.py
│   │   └── coherence.py
│   ├── golden/              # Golden dataset tooling
│   │   ├── annotator_cli.py
│   │   └── eval_pairs.py
│   ├── ingestion/           # AI Search indexing pipeline
│   │   ├── create_index.py
│   │   └── pipeline.py
│   ├── orchestrator/        # Semantic Kernel (.NET)
│   ├── promptflow/          # Prompt Flow DAG
│   │   └── flow.dag.yaml
│   └── training/            # Fine-tuning data prep
│       └── prepare_finetune_data.py
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
