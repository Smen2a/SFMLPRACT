"""Tests for the core data model."""

from __future__ import annotations

from datetime import UTC, date, datetime

from tariffrag.models import (
    ISO,
    Chunk,
    CrossReference,
    DocStatus,
    DocType,
    Document,
    EffectiveDate,
    Origin,
    RevisionSource,
    Section,
    SourceRef,
    TitleSource,
)


def _document(**overrides: object) -> Document:
    base: dict[str, object] = {
        "doc_id": "isone:mr1:append_a",
        "iso": ISO.ISONE,
        "doc_type": DocType.TARIFF,
        "title": "MARKET MONITORING, REPORTING AND MARKET POWER MITIGATION",
        "title_source": TitleSource.COVER_PAGE,
        "source": SourceRef(
            origin=Origin.SUPPLIED,
            path="corpus/isone/mr1/mr1_append_a.pdf",
            retrieved_at=datetime(2026, 9, 18, tzinfo=UTC),
            sha256_pdf="a" * 64,
        ),
        "page_count": 103,
        "status": DocStatus.ACTIVE,
        "extractor": "pdfplumber",
        "extractor_version": "0.11.10",
    }
    base.update(overrides)
    return Document(**base)  # type: ignore[arg-type]


def test_cite_label_uses_the_primary_effective_date() -> None:
    doc = _document(
        effective_dates=(
            EffectiveDate("5/3/25", "ER25-2149-000", tuple(range(6, 103)), date(2025, 5, 3)),
            EffectiveDate("March 31, 2026", "ER26-925-000", (1, 2, 3), date(2026, 3, 31)),
        ),
        revision_source=RevisionSource.COVER_PAGE,
    )
    assert doc.cite_label().endswith("(eff. 2025-05-03)")


def test_cite_label_resolves_the_stamp_for_a_given_page() -> None:
    """ISO-NE versions at finer granularity than the file.

    A section on page 1 is in force from a different date than the bulk of the
    document, so a citation must resolve its own page's stamp.
    """
    doc = _document(
        effective_dates=(
            EffectiveDate("5/3/25", "ER25-2149-000", tuple(range(6, 103)), date(2025, 5, 3)),
            EffectiveDate("March 31, 2026", "ER26-925-000", (1, 2, 3), date(2026, 3, 31)),
        )
    )
    assert "eff. 2026-03-31" in doc.cite_label(page=1)
    assert "eff. 2025-05-03" in doc.cite_label(page=50)


def test_cite_label_omits_absent_qualifiers() -> None:
    """Appendices C, D and G carry no stamp at all, and must still render."""
    doc = _document()
    assert doc.primary_effective_date is None
    assert doc.cite_label() == "MARKET MONITORING, REPORTING AND MARKET POWER MITIGATION"


def test_unparseable_stamp_falls_back_to_raw_text() -> None:
    doc = _document(effective_dates=(EffectiveDate("sometime", "ER1-1-1", (1,), None),))
    assert "eff. sometime" in doc.cite_label()


def test_only_active_documents_are_ingestable() -> None:
    assert _document().is_ingestable
    assert not _document(status=DocStatus.RESERVED).is_ingestable
    assert not _document(status=DocStatus.EXCLUDED).is_ingestable


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
        from_doc_id="isone:mr1:sec_13_14",
        from_section_id="III.13.1",
        raw_text="Section III.12.2",
        char_start=0,
        char_end=16,
    )
    assert not unresolved.resolved

    resolved = CrossReference(
        from_doc_id="isone:mr1:sec_13_14",
        from_section_id="III.13.1",
        raw_text="Section III.12.2",
        char_start=0,
        char_end=16,
        to_doc_id="isone:mr1:sec_1_12",
        to_section_id="III.12.2",
    )
    assert resolved.resolved
