"""Turning the corpus into canonical text, section trees and cross-references.

Reads the manifest to decide what to ingest -- reserved and excluded documents
are skipped by status, not by a hard-coded list -- and writes, per document, the
canonical text, its layout, the section tree, the cross-references, and an
:class:`IngestRecord` describing the run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pdfplumber

from tariffrag import CHUNKER_VERSION
from tariffrag.ingest.extract import (
    CanonicalText,
    build_canonical,
    build_sections,
    save_canonical,
)
from tariffrag.ingest.manifest import Manifest
from tariffrag.ingest.structure import parse_document
from tariffrag.ingest.xref import XrefReport, extract_xrefs
from tariffrag.models import Document, IngestRecord, Section

__all__ = ["DocumentIngest", "ingest_corpus", "load_sections"]

SECTIONS_SUFFIX = ".sections.json"
XREFS_SUFFIX = ".xrefs.json"
RECORD_SUFFIX = ".ingest.json"


@dataclass(slots=True)
class DocumentIngest:
    """One document's ingest result, held while the corpus-wide pass completes."""

    document: Document
    canonical: CanonicalText
    sections: tuple[Section, ...]
    xrefs: XrefReport | None = None


def _appendix_map(manifest: Manifest) -> dict[str, str]:
    """Letter -> doc id, so ``Appendix G`` resolves across documents."""
    mapping: dict[str, str] = {}
    for doc in manifest.documents:
        _, _, short = doc.doc_id.rpartition(":")
        if short.startswith("append_") and len(short) == len("append_") + 1:
            mapping[short[-1].upper()] = doc.doc_id
    return mapping


def ingest_corpus(manifest: Manifest, repo_root: Path, text_dir: Path) -> list[IngestRecord]:
    """Ingest every active document in the manifest.

    Runs in two passes because cross-reference resolution needs the whole
    corpus: Market Rule 1 is split across files whose sections cite each other,
    so resolving within one document alone understates the rate badly (87% corpus
    wide against 75% document local, measured).
    """
    parsed: list[DocumentIngest] = []
    for doc in manifest.ingestable:
        structure = parse_document(repo_root / doc.source.path)
        canonical = build_canonical(doc.doc_id, structure)
        sections = build_sections(doc.doc_id, structure, canonical)
        parsed.append(DocumentIngest(doc, canonical, sections))

    corpus_sections = {
        item.document.doc_id: {s.section_id for s in item.sections if s.section_id}
        for item in parsed
    }
    appendix_docs = _appendix_map(manifest)

    records: list[IngestRecord] = []
    for item in parsed:
        canonical = item.canonical
        item.xrefs = extract_xrefs(
            item.document.doc_id,
            item.canonical,
            item.sections,
            appendix_docs,
            corpus_sections,
        )
        records.append(_write(item, text_dir))
    return records


def _write(item: DocumentIngest, text_dir: Path) -> IngestRecord:
    canonical = item.canonical
    doc = item.document
    save_canonical(canonical, text_dir)

    (text_dir / f"{doc.doc_id}{SECTIONS_SUFFIX}").write_text(
        json.dumps([asdict(s) for s in item.sections], indent=1), encoding="utf-8"
    )
    assert item.xrefs is not None
    (text_dir / f"{doc.doc_id}{XREFS_SUFFIX}").write_text(
        json.dumps([asdict(x) for x in item.xrefs.references], indent=1), encoding="utf-8"
    )

    covered = sum(s.char_end - s.char_start for s in item.sections)
    total = len(canonical.text)
    record = IngestRecord(
        doc_id=doc.doc_id,
        sha256_pdf=doc.source.sha256_pdf,
        sha256_canonical_text=canonical.sha256,
        extractor="pdfplumber",
        extractor_version=str(pdfplumber.__version__),
        chunker_version=CHUNKER_VERSION,
        page_count=doc.page_count,
        pages_kept=len(canonical.pages),
        excluded_toc_pages=canonical.excluded_toc_pages,
        section_count=len(item.sections),
        coverage=covered / total if total else 0.0,
        xref_count=len(item.xrefs.references),
        xref_resolved=item.xrefs.resolved,
    )
    (text_dir / f"{doc.doc_id}{RECORD_SUFFIX}").write_text(
        json.dumps(asdict(record), indent=1), encoding="utf-8"
    )
    return record


def load_sections(doc_id: str, text_dir: Path) -> tuple[Section, ...]:
    raw = json.loads((text_dir / f"{doc_id}{SECTIONS_SUFFIX}").read_text(encoding="utf-8"))
    return tuple(
        Section(**{**row, "breadcrumb": tuple(tuple(pair) for pair in row["breadcrumb"])})
        for row in raw
    )
