"""Building the index from ingested text, with the guard that makes it safe.

Every chunk offset is relative to one specific canonical text produced by one
specific version of the extractor and chunker. Indexing against anything else
would not fail loudly -- it would shift citations by a few characters and keep
working, which is the worst possible outcome for a system whose whole claim is
that quotes are verifiable. So the record is checked first and a mismatch is a
hard error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tariffrag import CHUNKER_VERSION
from tariffrag.index import store
from tariffrag.index.dense import Embedder, create_vector_table, upsert_vectors
from tariffrag.ingest.chunk import build_chunks, group_paragraphs
from tariffrag.ingest.extract import load_canonical
from tariffrag.ingest.manifest import Manifest
from tariffrag.ingest.pipeline import RECORD_SUFFIX, load_sections
from tariffrag.models import Chunk, IngestRecord

__all__ = ["BuildResult", "StaleIngestError", "build_index", "load_record"]


class StaleIngestError(RuntimeError):
    """The ingested text does not match the manifest or the current code."""


@dataclass(frozen=True, slots=True)
class BuildResult:
    documents: int
    chunks: int
    blocks: int
    embedder: str | None = None


def load_record(doc_id: str, text_dir: Path) -> IngestRecord:
    raw = json.loads((text_dir / f"{doc_id}{RECORD_SUFFIX}").read_text(encoding="utf-8"))
    raw["excluded_toc_pages"] = tuple(raw["excluded_toc_pages"])
    return IngestRecord(**raw)


def _check(record: IngestRecord, manifest: Manifest, text_dir: Path) -> None:
    document = manifest.by_id(record.doc_id)
    if document is None:
        raise StaleIngestError(f"{record.doc_id} was ingested but is not in the manifest.")
    if document.source.sha256_pdf != record.sha256_pdf:
        raise StaleIngestError(
            f"{record.doc_id}: the PDF changed since it was ingested. Re-run `tariffrag ingest`."
        )
    if record.chunker_version != CHUNKER_VERSION:
        raise StaleIngestError(
            f"{record.doc_id}: ingested under chunker version {record.chunker_version}, "
            f"current is {CHUNKER_VERSION}. Re-run `tariffrag ingest`."
        )
    # load_canonical verifies the text against its own recorded hash; this checks
    # that the same text is the one the ingest record describes.
    canonical = load_canonical(record.doc_id, text_dir)
    if canonical.sha256 != record.sha256_canonical_text:
        raise StaleIngestError(
            f"{record.doc_id}: canonical text does not match the ingest record. "
            "Every stored offset is relative to that text, so re-run `tariffrag ingest`."
        )


def build_index(
    manifest: Manifest,
    text_dir: Path,
    index_path: Path,
    embedder: Embedder | None = None,
) -> BuildResult:
    """Chunk every ingested document and write the index.

    Embedding is optional: without it the index is lexical-only and still
    usable, which keeps the project runnable with no model download.
    """
    if index_path.exists():
        index_path.unlink()

    connection = store.connect(index_path)
    store.create_schema(connection)

    indexed: list[Chunk] = []
    documents = []
    for record_path in sorted(text_dir.glob(f"*{RECORD_SUFFIX}")):
        doc_id = json.loads(record_path.read_text(encoding="utf-8"))["doc_id"]
        record = load_record(doc_id, text_dir)
        _check(record, manifest, text_dir)

        document = manifest.by_id(doc_id)
        assert document is not None  # _check already established this
        documents.append(document)

        canonical = load_canonical(doc_id, text_dir)
        sections = load_sections(doc_id, text_dir)
        store.insert_sections(connection, sections)

        chunks = build_chunks(sections, canonical, group_paragraphs(canonical))
        store.insert_chunks(connection, chunks)
        indexed.extend(chunks)

    store.insert_documents(connection, documents)

    if embedder is not None and indexed:
        create_vector_table(connection, embedder.dimensions)
        # Breadcrumb is prepended before embedding: a subsection's own text often
        # omits the vocabulary a question uses, which lives in its parents.
        payload = [f"{chunk.breadcrumb}\n\n{chunk.text}" for chunk in indexed]
        vectors = embedder.embed_documents(payload)
        upsert_vectors(connection, [chunk.chunk_id for chunk in indexed], vectors)

    connection.commit()
    connection.close()

    return BuildResult(
        documents=len(documents),
        chunks=len(indexed),
        blocks=sum(len(c.blocks) for c in indexed),
        embedder=embedder.name if embedder else None,
    )
