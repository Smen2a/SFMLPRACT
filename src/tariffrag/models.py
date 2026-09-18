"""Core data model.

The central structural idea is that three different units are kept distinct:

``Section``
    The *parse* unit -- one node of a document's numbered hierarchy, exactly as
    the tariff lays it out (e.g. ``III.13.1.2.3``).

``Chunk``
    The *retrieval* unit -- sections packed or split into embedding-sized
    pieces, never crossing a parent boundary.

``CitationBlock``
    The *citation* unit -- a paragraph or enumerated item, which becomes one
    entry in a ``search_result`` block's ``content`` array. Block size is the
    citation-precision knob: one oversized block yields citations that mean
    "somewhere in these 1200 tokens".

Every unit carries ``[char_start, char_end)`` offsets into its document's
canonical text, so a citation returned by the API resolves back to a page and a
verbatim span.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

__all__ = [
    "ISO",
    "Chunk",
    "CitationBlock",
    "CrossReference",
    "DefinedTerm",
    "DocType",
    "Document",
    "RevisionSource",
    "Section",
]


class ISO(StrEnum):
    """Independent System Operator a document belongs to."""

    ISONE = "ISONE"
    NYISO = "NYISO"


class DocType(StrEnum):
    TARIFF = "tariff"
    MANUAL = "manual"


class RevisionSource(StrEnum):
    """Where a document's revision/effective date was recovered from.

    ISO-NE encodes revision and date in the filename
    (``manual_20_forward_capacity_market_rev27_2023_04_06.pdf``), while NYISO's
    UUID-suffixed URLs carry nothing, so the revision must be read off the cover
    page or footer. Recording *where* the revision came from -- and allowing
    ``UNKNOWN`` -- is more honest than guessing.
    """

    URL = "url"
    FILENAME = "filename"
    COVER_PAGE = "cover_page"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Document:
    """One source PDF and the provenance needed to reproduce it."""

    doc_id: str
    iso: ISO
    doc_type: DocType
    title: str
    source_index_url: str
    """The ISO's index page -- stable, and what the resolver re-scrapes."""
    resolved_pdf_url: str
    """The deep link at retrieval time. Transient; never used as a citation id."""
    retrieved_at: datetime
    sha256_pdf: str
    sha256_canonical_text: str
    page_count: int
    extractor: str
    extractor_version: str
    chunker_version: str
    revision: str | None = None
    effective_date: date | None = None
    revision_source: RevisionSource = RevisionSource.UNKNOWN

    def cite_label(self) -> str:
        """Human-facing provenance label, e.g. ``M-20 (Rev. 27, eff. 2023-04-06)``."""
        parts = [self.title]
        qualifiers = []
        if self.revision:
            qualifiers.append(f"Rev. {self.revision}")
        if self.effective_date:
            qualifiers.append(f"eff. {self.effective_date.isoformat()}")
        if qualifiers:
            parts.append(f"({', '.join(qualifiers)})")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Section:
    """One node of a document's numbered hierarchy."""

    doc_id: str
    section_id: str
    """Normalized identifier, e.g. ``III.13.1.2.3``."""
    heading: str
    breadcrumb: tuple[tuple[str, str], ...]
    """Ancestor ``(section_id, heading)`` pairs, outermost first."""
    depth: int
    parent_id: str | None
    page_start: int
    page_end: int
    char_start: int
    char_end: int

    def render_breadcrumb(self) -> str:
        """``III.13 Capacity Market > III.13.1 Qualification`` for prompt titles."""
        trail = [f"{sid} {heading}".strip() for sid, heading in self.breadcrumb]
        trail.append(f"{self.section_id} {self.heading}".strip())
        return " > ".join(trail)


@dataclass(frozen=True, slots=True)
class CitationBlock:
    """A paragraph or enumerated item -- one entry in ``search_result.content``."""

    block_id: str
    chunk_id: str
    ordinal: int
    """Position within the parent chunk's ``content`` array (0-based)."""
    text: str
    char_start: int
    char_end: int


@dataclass(slots=True)
class Chunk:
    """The retrieval unit: a packed or split run of section text."""

    chunk_id: str
    doc_id: str
    section_id: str
    ordinal: int
    """Which piece of its section this is, when a section was split."""
    breadcrumb: str
    text: str
    char_start: int
    char_end: int
    page_start: int
    page_end: int
    token_count: int
    blocks: list[CitationBlock] = field(default_factory=list)
    continued_from: str | None = None
    continues_in: str | None = None

    def source_uri(self) -> str:
        """Stable internal citation id, e.g. ``isone:mr1:III.13.1.2.3:c2``.

        Deliberately *not* a live URL: ISO deep links rotate, so the grounding
        record must not embed one. The manifest resolves this to a URL at render
        time.
        """
        return f"{self.doc_id}:{self.section_id}:c{self.ordinal}"


@dataclass(frozen=True, slots=True)
class DefinedTerm:
    """A capitalized Defined Term parsed from a tariff's definitions section."""

    term: str
    iso: ISO
    definition_text: str
    source_doc_id: str
    source_section_id: str
    char_start: int
    char_end: int
    aliases: tuple[str, ...] = ()
    """Acronym expansions recovered from parentheticals, e.g. ``CSO``."""


@dataclass(frozen=True, slots=True)
class CrossReference:
    """An intra-corpus reference such as ``as defined in Section III.12.2``.

    Resolution rate doubles as a parser-quality metric: if a large share of
    references fail to resolve, the section tree is wrong.
    """

    from_chunk_id: str
    raw_text: str
    char_start: int
    char_end: int
    to_doc_id: str | None = None
    to_section_id: str | None = None

    @property
    def resolved(self) -> bool:
        return self.to_section_id is not None
