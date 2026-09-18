"""Tests for the core data model."""

from __future__ import annotations

from datetime import UTC, date, datetime

from tariffrag.models import (
    ISO,
    Chunk,
    CrossReference,
    DocType,
    Document,
    RevisionSource,
    Section,
)


def _document(**overrides: object) -> Document:
    base: dict[str, object] = {
        "doc_id": "isone:m20",
        "iso": ISO.ISONE,
        "doc_type": DocType.MANUAL,
        "title": "ISO New England Manual M-20",
        "source_index_url": "https://www.iso-ne.com/manuals",
        "resolved_pdf_url": "https://www.iso-ne.com/static-assets/m20_rev27.pdf",
        "retrieved_at": datetime(2026, 9, 18, tzinfo=UTC),
        "sha256_pdf": "a" * 64,
        "sha256_canonical_text": "b" * 64,
        "page_count": 212,
        "extractor": "pdfplumber",
        "extractor_version": "0.11.4",
        "chunker_version": "0",
    }
    base.update(overrides)
    return Document(**base)  # type: ignore[arg-type]


def test_cite_label_includes_revision_and_effective_date() -> None:
    doc = _document(
        revision="27",
        effective_date=date(2023, 4, 6),
        revision_source=RevisionSource.FILENAME,
    )
    assert doc.cite_label() == "ISO New England Manual M-20 (Rev. 27, eff. 2023-04-06)"


def test_cite_label_omits_absent_qualifiers() -> None:
    """A document whose revision could not be recovered still renders cleanly."""
    doc = _document()
    assert doc.revision_source is RevisionSource.UNKNOWN
    assert doc.cite_label() == "ISO New England Manual M-20"


def test_render_breadcrumb_appends_the_section_itself() -> None:
    section = Section(
        doc_id="isone:mr1",
        section_id="III.13.1",
        heading="Qualification Process",
        breadcrumb=(("III", "Market Rule 1"), ("III.13", "Forward Capacity Market")),
        depth=2,
        parent_id="III.13",
        page_start=400,
        page_end=412,
        char_start=1_000,
        char_end=9_000,
    )
    assert section.render_breadcrumb() == (
        "III Market Rule 1 > III.13 Forward Capacity Market > III.13.1 Qualification Process"
    )


def test_source_uri_is_stable_and_carries_no_url() -> None:
    """Citation ids must not embed ISO deep links, which rotate."""
    chunk = Chunk(
        chunk_id="c-1",
        doc_id="isone:mr1",
        section_id="III.13.1.2.3",
        ordinal=2,
        breadcrumb="III.13 Forward Capacity Market",
        text="...",
        char_start=0,
        char_end=10,
        page_start=1,
        page_end=1,
        token_count=2,
    )
    uri = chunk.source_uri()
    assert uri == "isone:mr1:III.13.1.2.3:c2"
    assert "http" not in uri


def test_cross_reference_resolution_flag() -> None:
    unresolved = CrossReference(
        from_chunk_id="c-1", raw_text="Section III.12.2", char_start=0, char_end=16
    )
    assert not unresolved.resolved

    resolved = CrossReference(
        from_chunk_id="c-1",
        raw_text="Section III.12.2",
        char_start=0,
        char_end=16,
        to_doc_id="isone:mr1",
        to_section_id="III.12.2",
    )
    assert resolved.resolved
