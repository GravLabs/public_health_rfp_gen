"""
Parses RFP documents (Markdown, PDF, DOCX) into structured sections.
Uses Azure AI Document Intelligence for PDF/DOCX; direct parse for Markdown.
"""

import os
import re
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import TokenCredential


DOC_INTEL_ENDPOINT = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")

SECTION_PATTERNS = {
    "background": r"(?i)(background|purpose|overview)",
    "funding_parameters": r"(?i)(funding param|award amount|total funding|funding available)",
    "eligibility": r"(?i)(eligib)",
    "scope_of_work": r"(?i)(scope|priority area|activities|deliverable)",
    "reporting_requirements": r"(?i)(report)",
    "budget_requirements": r"(?i)(budget|allowable cost|unallowable)",
    "evaluation_criteria": r"(?i)(evaluat|scoring|criteria)",
    "submission_instructions": r"(?i)(submit|deadline|application|instruction)",
}

RFP_METADATA_PATTERNS = {
    "rfp_id": r"RFP[- ]ID[:\s]+([A-Z0-9\-]+)",
    "fiscal_year": r"(?:FY|Fiscal Year)[:\s]*(\d{4})",
    "federal_sponsor": r"Federal Sponsor[:\s]+(.+?)(?:\n|$)",
    "program_area": r"Program Area[:\s]+(.+?)(?:\n|$)",
}


def _detect_section(heading: str) -> str:
    for section_type, pattern in SECTION_PATTERNS.items():
        if re.search(pattern, heading):
            return section_type
    return "general"


def _extract_metadata(text: str) -> dict:
    meta = {}
    for key, pattern in RFP_METADATA_PATTERNS.items():
        m = re.search(pattern, text)
        if m:
            meta[key] = m.group(1).strip()
    if "fiscal_year" not in meta:
        yr = re.search(r"20(2[0-9])", text)
        meta["fiscal_year"] = yr.group(0) if yr else "unknown"
    return meta


def parse_markdown(content: bytes, source: str) -> dict:
    text = content.decode("utf-8", errors="replace")
    meta = _extract_metadata(text)
    meta.setdefault("rfp_id", source.split("/")[-1].replace(".md", ""))

    sections = []
    current_heading = "general"
    current_lines = []

    for line in text.splitlines():
        if line.startswith("#"):
            if current_lines:
                sections.append({
                    "section_type": _detect_section(current_heading),
                    "heading": current_heading,
                    "content": "\n".join(current_lines).strip(),
                })
            current_heading = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append({
            "section_type": _detect_section(current_heading),
            "heading": current_heading,
            "content": "\n".join(current_lines).strip(),
        })

    return {**meta, "sections": [s for s in sections if s["content"]]}


def parse_pdf_or_docx(content: bytes, source: str, credential: TokenCredential) -> dict:
    client = DocumentIntelligenceClient(endpoint=DOC_INTEL_ENDPOINT, credential=credential)
    poller = client.begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=content),
    )
    result = poller.result()

    # Reconstruct text with section breaks from Document Intelligence paragraphs
    full_text = "\n".join(p.content for p in (result.paragraphs or []))
    meta = _extract_metadata(full_text)
    meta.setdefault("rfp_id", source.split("/")[-1].rsplit(".", 1)[0])

    # Simple section split on heading paragraphs
    sections = []
    current_heading = "general"
    current_content = []

    for para in result.paragraphs or []:
        role = getattr(para, "role", None)
        if role in ("title", "sectionHeading"):
            if current_content:
                sections.append({
                    "section_type": _detect_section(current_heading),
                    "heading": current_heading,
                    "content": " ".join(current_content).strip(),
                })
            current_heading = para.content
            current_content = []
        else:
            current_content.append(para.content)

    if current_content:
        sections.append({
            "section_type": _detect_section(current_heading),
            "heading": current_heading,
            "content": " ".join(current_content).strip(),
        })

    return {**meta, "sections": [s for s in sections if s["content"]]}


def parse_document(content: bytes, source: str, credential=None) -> dict:
    if source.endswith(".md") or source.endswith(".txt"):
        return parse_markdown(content, source)
    return parse_pdf_or_docx(content, source, credential)
