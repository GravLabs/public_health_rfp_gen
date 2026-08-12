"""
Contextual chunking: splits parsed RFP sections into overlapping chunks,
prepending a document-level summary to each chunk for retrieval quality.
"""

import hashlib
import re
from dataclasses import dataclass, field


CHUNK_SIZE = 512      # tokens (approximate — using words as proxy)
CHUNK_OVERLAP = 64
WORDS_PER_TOKEN = 0.75  # conservative estimate


@dataclass
class Chunk:
    id: str
    content: str
    context_summary: str
    section_type: str
    rfp_id: str
    fiscal_year: str
    program_area: str
    federal_sponsor: str
    source_file: str
    chunk_index: int


def _approximate_tokens(text: str) -> int:
    return int(len(text.split()) / WORDS_PER_TOKEN)


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    chunk_words = int(chunk_size * WORDS_PER_TOKEN)
    overlap_words = int(overlap * WORDS_PER_TOKEN)
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += chunk_words - overlap_words
    return chunks


def chunk_document(parsed: dict, source_file: str) -> list[Chunk]:
    """
    parsed: output of document_parser.parse_document
    Returns list of Chunk objects ready for embedding and indexing.
    """
    rfp_id = parsed.get("rfp_id", "unknown")
    fiscal_year = parsed.get("fiscal_year", "unknown")
    program_area = parsed.get("program_area", "unknown")
    federal_sponsor = parsed.get("federal_sponsor", "unknown")

    # Document-level summary prepended to every chunk for context
    context_summary = (
        f"Public Health RFP {rfp_id} | {program_area} | Sponsor: {federal_sponsor} | FY {fiscal_year}"
    )

    chunks: list[Chunk] = []
    chunk_index = 0

    for section in parsed.get("sections", []):
        section_type = section.get("section_type", "general")
        text = section.get("content", "").strip()
        if not text:
            continue

        for sub_chunk in _chunk_text(text):
            chunk_id = hashlib.md5(f"{rfp_id}:{chunk_index}:{sub_chunk[:50]}".encode()).hexdigest()
            chunks.append(Chunk(
                id=chunk_id,
                content=sub_chunk,
                context_summary=context_summary,
                section_type=section_type,
                rfp_id=rfp_id,
                fiscal_year=fiscal_year,
                program_area=program_area,
                federal_sponsor=federal_sponsor,
                source_file=source_file,
                chunk_index=chunk_index,
            ))
            chunk_index += 1

    return chunks
