"""
SharePoint integration via Microsoft Graph API.
Reads historical RFP documents from SharePoint for corpus ingestion
and writes generated drafts back for human review.
Uses DefaultAzureCredential — requires the managed identity to have
Sites.ReadWrite.All on the target SharePoint site.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

import httpx
from azure.identity import DefaultAzureCredential
from azure.identity import get_bearer_token_provider

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class SharePointClient:
    def __init__(self, site_id: str, credential: Optional[DefaultAzureCredential] = None):
        """
        Args:
            site_id: SharePoint site ID in format {tenant}.sharepoint.com,{site-guid},{web-guid}
                     or the hostname/path form: sites/{hostname}:/sites/{site-name}
        """
        self._site_id = site_id
        self._credential = credential or DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(self._credential, GRAPH_SCOPE)

    def _headers(self) -> dict[str, str]:
        token = self._token_provider()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ── Read operations ──────────────────────────────────────────────────────

    async def list_library_files(self, library_name: str = "Documents") -> list[dict]:
        """List all files in a SharePoint document library."""
        url = f"{GRAPH_BASE}/sites/{self._site_id}/drives"
        async with httpx.AsyncClient(timeout=30) as client:
            drives_resp = await client.get(url, headers=self._headers())
            drives_resp.raise_for_status()
            drives = drives_resp.json()["value"]

        drive = next((d for d in drives if d["name"] == library_name), None)
        if not drive:
            raise ValueError(f"Library '{library_name}' not found on site {self._site_id}")

        drive_id = drive["id"]
        items_url = f"{GRAPH_BASE}/drives/{drive_id}/root/children"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(items_url, headers=self._headers())
            resp.raise_for_status()

        files = [
            {"id": item["id"], "name": item["name"], "size": item.get("size", 0),
             "web_url": item.get("webUrl"), "drive_id": drive_id,
             "mime_type": item.get("file", {}).get("mimeType")}
            for item in resp.json()["value"]
            if "file" in item
        ]
        log.info("Found %d files in SharePoint library '%s'", len(files), library_name)
        return files

    async def download_file(self, drive_id: str, item_id: str) -> bytes:
        """Download a file from SharePoint by drive and item ID."""
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
        return resp.content

    async def download_library_to_local(
        self, library_name: str, dest_dir: Path, extensions: tuple[str, ...] = (".pdf", ".docx", ".md")
    ) -> list[Path]:
        """Download all matching files from a SharePoint library to a local directory."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = await self.list_library_files(library_name)
        downloaded: list[Path] = []

        for f in files:
            if not any(f["name"].lower().endswith(ext) for ext in extensions):
                continue
            content = await self.download_file(f["drive_id"], f["id"])
            dest = dest_dir / f["name"]
            dest.write_bytes(content)
            downloaded.append(dest)
            log.info("Downloaded %s (%d bytes)", f["name"], len(content))

        return downloaded

    # ── Write operations ─────────────────────────────────────────────────────

    async def upload_draft(
        self, library_name: str, file_name: str, content: str, folder: str = "GeneratedDrafts"
    ) -> str:
        """Upload a generated RFP draft (markdown) to SharePoint. Returns the web URL."""
        url = f"{GRAPH_BASE}/sites/{self._site_id}/drives"
        async with httpx.AsyncClient(timeout=30) as client:
            drives_resp = await client.get(url, headers=self._headers())
            drives_resp.raise_for_status()
            drives = drives_resp.json()["value"]

        drive = next((d for d in drives if d["name"] == library_name), drives[0])
        drive_id = drive["id"]

        upload_url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{folder}/{file_name}:/content"
        headers = {**self._headers(), "Content-Type": "text/markdown"}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(upload_url, headers=headers, content=content.encode())
            resp.raise_for_status()

        web_url = resp.json().get("webUrl", "")
        log.info("Draft uploaded to SharePoint: %s", web_url)
        return web_url

    async def upload_draft_docx(
        self,
        library_name: str,
        rfp_id: str,
        draft_id: str,
        sections: dict[str, str],
        folder: str = "GeneratedDrafts",
    ) -> str:
        """Assemble a Word document from RFP sections and upload to SharePoint."""
        docx_bytes = SharePointClient.draft_to_docx(rfp_id, sections)
        file_name = f"{rfp_id}_{draft_id}.docx"

        url = f"{GRAPH_BASE}/sites/{self._site_id}/drives"
        async with httpx.AsyncClient(timeout=30) as client:
            drives_resp = await client.get(url, headers=self._headers())
            drives_resp.raise_for_status()
            drives = drives_resp.json()["value"]

        drive = next((d for d in drives if d["name"] == library_name), drives[0])
        drive_id = drive["id"]

        upload_url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{folder}/{file_name}:/content"
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        headers = {**self._headers(), "Content-Type": content_type}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(upload_url, headers=headers, content=docx_bytes)
            resp.raise_for_status()

        web_url = resp.json().get("webUrl", "")
        log.info("Word draft uploaded to SharePoint: %s", web_url)
        return web_url

    async def create_review_item(self, site_list_name: str, draft_metadata: dict) -> str:
        """Create a list item in a SharePoint review tracking list."""
        url = f"{GRAPH_BASE}/sites/{self._site_id}/lists/{site_list_name}/items"
        payload = {"fields": draft_metadata}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
        return resp.json()["id"]

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def draft_to_docx(rfp_id: str, sections: dict[str, str]) -> bytes:
        """Assemble an RFP draft as a Word (.docx) document. Returns raw bytes."""
        from docx import Document
        from docx.shared import Pt
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        SECTION_HEADINGS = {
            "background":             "1. Background and Purpose",
            "funding_parameters":     "2. Funding Parameters",
            "eligibility":            "3. Eligibility Criteria",
            "scope_of_work":          "4. Scope of Work",
            "reporting_requirements": "5. Reporting Requirements",
            "budget_requirements":    "6. Budget Requirements",
            "evaluation_criteria":    "7. Evaluation Criteria",
            "submission_instructions":"8. Submission Instructions",
        }

        doc = Document()

        # Cover
        title = doc.add_heading("REQUEST FOR PROPOSALS", level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER

        sub = doc.add_paragraph(f"RFP ID: {rfp_id}")
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub.runs[0].italic = True

        notice = doc.add_paragraph("AI-generated draft — pending human review and approval before release.")
        notice.runs[0].font.size = Pt(9)
        notice.runs[0].italic = True

        doc.add_paragraph("")  # spacer

        for key, heading in SECTION_HEADINGS.items():
            doc.add_heading(heading, level=1)
            content = sections.get(key, "Not generated.")
            doc.add_paragraph(content)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf.getvalue()

    @staticmethod
    def draft_to_markdown(rfp_id: str, sections: dict[str, str]) -> str:
        """Render an RFP draft dictionary into a Markdown document."""
        header_map = {
            "background": "1. Background and Purpose",
            "funding_parameters": "2. Funding Parameters",
            "eligibility": "3. Eligibility Criteria",
            "scope_of_work": "4. Scope of Work",
            "reporting_requirements": "5. Reporting Requirements",
            "budget_requirements": "6. Budget Requirements",
            "evaluation_criteria": "7. Evaluation Criteria",
            "submission_instructions": "8. Submission Instructions",
        }
        lines = [f"# REQUEST FOR PROPOSALS\n**RFP ID:** {rfp_id} *(AI-generated draft — pending human review)*\n\n---\n"]
        for key, heading in header_map.items():
            lines.append(f"## {heading}\n\n{sections.get(key, '_Not generated_')}\n\n---\n")
        return "\n".join(lines)
