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

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

__all__ = [
    "ISO",
    "Chunk",
    "CitationBlock",
    "CrossReference",
    "DefinedTerm",
    "DocStatus",
    "DocType",
    "Document",
    "EffectiveDate",
    "Origin",
    "RevisionSource",
    "Section",
    "SourceRef",
    "TitleSource",
    "format_page_ranges",
    "parse_page_ranges",
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


class Origin(StrEnum):
    """How a source document reached the corpus."""

    SUPPLIED = "supplied"
    """Provided directly. Carries no verified URL."""
    FETCHED = "fetched"
    """Downloaded from the ISO, with the URL recorded."""


class DocStatus(StrEnum):
    """Whether a document takes part in the corpus, and why not if it does not."""

    ACTIVE = "active"
    RESERVED = "reserved"
    """An intentionally blank tariff section. Excluded, but nothing is lost."""
    EXCLUDED = "excluded"
    """Content exists but could not be extracted. Excluded *with* loss."""


class TitleSource(StrEnum):
    """Where a document's title was recovered from.

    Recorded for the same reason as ``RevisionSource``: a title read off a cover
    page and one derived from a filename deserve different trust.
    """

    COVER_PAGE = "cover_page"
    FIRST_HEADING = "first_heading"
    FILENAME = "filename"


def format_page_ranges(pages: Iterable[int]) -> str:
    """Compact a page list for storage, e.g. ``(1, 2, 3, 7)`` -> ``"1-3,7"``."""
    ordered = sorted(set(pages))
    if not ordered:
        return ""
    spans: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        spans.append((start, previous))
        start = previous = page
    spans.append((start, previous))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def parse_page_ranges(text: str) -> tuple[int, ...]:
    """Inverse of :func:`format_page_ranges`."""
    pages: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            pages.extend(range(int(first), int(last) + 1))
        else:
            pages.append(int(part))
    return tuple(pages)


@dataclass(frozen=True, slots=True)
class EffectiveDate:
    """One effective-date stamp and the pages it governs.

    ISO-NE prints this on every page, and the value *differs within a single
    document* -- Market Rule 1 Sections 13-14 carry four. So versioning is
    per-section rather than per-document, and the pages each stamp covers are
    what lets a section be attributed to one.

    ``date_text`` is kept alongside the parsed ``date`` so an unrecognised format
    degrades to the raw string instead of being dropped.
    """

    date_text: str
    docket: str
    pages: tuple[int, ...]
    date: date | None = None

    @property
    def page_span(self) -> str:
        return format_page_ranges(self.pages)

    def covers(self, page: int) -> bool:
        return page in self.pages


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where a document came from and what it hashed to when recorded."""

    origin: Origin
    path: str
    """Repo-relative path, e.g. ``corpus/isone/mr1/mr1_sec_13_14.pdf``."""
    retrieved_at: datetime
    sha256_pdf: str
    url: str | None = None
    """Left ``None`` until a fetcher verifies one.

    A plausible-looking URL that nobody checked is worse than no URL: it would
    put an unverified claim into the provenance chain that citations rest on.
    """


@dataclass(frozen=True, slots=True)
class Document:
    """One source document as the manifest records it.

    Deliberately holds only what is knowable *before* extraction. The canonical
    text hash and chunker version belong to the ingest step that produces them,
    not here -- recording them at manifest time would mean inventing values.
    """

    doc_id: str
    iso: ISO
    doc_type: DocType
    title: str
    title_source: TitleSource
    source: SourceRef
    page_count: int
    status: DocStatus
    extractor: str
    extractor_version: str
    effective_dates: tuple[EffectiveDate, ...] = ()
    revision: str | None = None
    revision_source: RevisionSource = RevisionSource.UNKNOWN

    @property
    def is_ingestable(self) -> bool:
        return self.status is DocStatus.ACTIVE

    @property
    def primary_effective_date(self) -> EffectiveDate | None:
        """The stamp governing the most pages.

        A document-level label needs one date, but a document may carry several.
        Section-level citations use the stamp covering their own pages instead,
        via :meth:`effective_date_for_page`.
        """
        if not self.effective_dates:
            return None
        return max(self.effective_dates, key=lambda eff: len(eff.pages))

    def effective_date_for_page(self, page: int) -> EffectiveDate | None:
        """The stamp governing ``page``, which is what a section citation needs."""
        for eff in self.effective_dates:
            if eff.covers(page):
                return eff
        return None

    def cite_label(self, page: int | None = None) -> str:
        """Human-facing provenance label.

        With a page, uses that page's own effective date, since ISO-NE versions
        at finer granularity than the file.
        """
        eff = self.effective_date_for_page(page) if page is not None else None
        eff = eff or self.primary_effective_date

        qualifiers = []
        if self.revision:
            qualifiers.append(f"Rev. {self.revision}")
        if eff:
            shown = eff.date.isoformat() if eff.date else eff.date_text
            qualifiers.append(f"eff. {shown}")
        if not qualifiers:
            return self.title
        return f"{self.title} ({', '.join(qualifiers)})"


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
