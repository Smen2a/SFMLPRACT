"""Parser spike: measure whether a PDF's section structure is recoverable.

This is diagnostic tooling, deliberately kept out of the ingest path. It exists
to answer one question before any chunking or extraction decision is locked in:
*can we recover ``III.13.1.2.3`` boundaries from this document's text?*

Everything downstream -- chunk quality, breadcrumbs, gold-set section ids,
cross-reference resolution, citation rendering -- assumes the answer is yes. The
spike finds out cheaply, per document, and returns a verdict:

``GO``
    Structure is recoverable using the full signal cascade.
``DEGRADED``
    Recoverable, but a signal is unavailable or the sequence is still noisy.
``EMPTY``
    An intentionally blank tariff section (``[RESERVED]``). Excluded from the
    corpus, but nothing is lost -- distinct from ``NO_GO`` so nobody goes
    looking for an OCR fix for a document that has no content to recover.
``NO_GO``
    Content exists but is not extractable -- a scan. Claude's citations require
    a text layer, so such a document cannot be cited and must be dropped with
    the exclusion stated in the README.
"""

from __future__ import annotations

import bisect
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

import pdfplumber
from rich.console import Console
from rich.table import Table

from tariffrag.models import EffectiveDate, TitleSource

__all__ = [
    "CrossRefAudit",
    "ParsedId",
    "SpikeReport",
    "Verdict",
    "parse_section_id",
    "render_report",
    "spike_document",
]

# --- Thresholds -----------------------------------------------------------

MIN_CHARS_PER_PAGE = 100
"""Below this median, treat the document as having no usable text layer."""

REPEATED_LINE_PAGE_RATIO = 0.15
"""Fraction of pages a furniture band must cover.

Deliberately low, because the variant-ratio rule below is what actually
separates furniture from body text. Measured on Market Rule 1, the footer
occupies two distinct bands -- one covering 77% of pages and a second covering
20% where the body text runs short -- so a high threshold silently drops the
second one.
"""

BAND_TOLERANCE = 3.0
"""Vertical tolerance, in points, for "the same height" on another page."""

MARGIN_BAND_RATIO = 0.12
"""Running headers/footers live in the top/bottom 12% of the page.

Without this positional constraint, body text that happens to repeat at the same
height on enough pages is misclassified as furniture.
"""

MIN_FURNITURE_PAGES = 2
"""A band must recur on at least this many pages to be "running" furniture.

A percentage alone is meaningless on a short document: one page out of two is
50% coverage but is not recurrence.
"""

MAX_FURNITURE_VARIANT_RATIO = 0.25
"""Distinct text variants a furniture position may have, per page it occupies.

Position alone is not enough: the first body line of each page also sits at a
consistent height. A running header repeats -- few variants across many pages
(ISO-NE footers vary only by effective date: 3 forms over 235 pages) -- whereas
body text differs on every page, giving roughly one variant per page.
"""

TOC_ID_DENSITY = 0.5
"""A page where this fraction of lines parse as section ids is a contents page."""

RESERVED_MAX_CHARS = 400
"""A reserved placeholder carries only cover boilerplate and a marker.

ISO-NE publishes repealed or not-yet-used appendices as one page reading
``[RESERVED]`` under the usual cover text, which lands above the text-layer
threshold. Appendix L, by contrast, is a genuine one-page appendix with 1,451
characters of substance, so the marker alone is not sufficient evidence.
"""

MAX_HEADING_REMAINDER = 120
"""Longer than this and the line is prose, not a title."""

HEADING_SCORE_THRESHOLD = 0.6

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

_ROMAN_SEGMENTED = re.compile(r"^(?P<roman>[IVXLCDM]+)(?P<rest>(?:\.(?:\d+|[A-Z]))+)\b")
"""Market Rule 1 numbering, in all three observed forms.

``III.13.1.2.3`` (body), ``III.A.1.1`` (appendix) and ``III.13.A.1`` (a lettered
subsection inside a numbered one) differ only in whether a given segment is a
number or a letter, so one pattern covers them rather than three.
"""

_LETTERED = re.compile(r"^(?P<kind>Appendix|Attachment|Schedule)\s+(?P<letter>[A-Z])\b")

_PLAIN_DOTTED = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\b")
"""Manual style: ``2.1.3``."""

_INLINE_REF = re.compile(
    r"\b(?:Section|Sections|Appendix|Appendices|Attachment|Schedule)\s+"
    r"(?:[IVXLCDM]+(?:\.[A-Z0-9]+)+|\d+(?:\.\d+)*|[A-Z])\b"
)
"""Matches a cross-reference such as ``as defined in Section III.12.2``."""

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
"""Embedded-font subset tag, e.g. ``CPJYEE+TimesNewRomanPSMT``.

The tag differs per file and per subset, so the same face appears under several
names unless it is stripped before any font comparison.
"""

_RESERVED = re.compile(r"\[?\s*RESERVED", re.IGNORECASE)

_COVER_BOILERPLATE = re.compile(r"^(SECTION\s+[IVXLCDM]+|MARKET\s+RULE\s+\d+)\.?$", re.IGNORECASE)
"""Masthead lines shared by every cover page, carrying no title information."""

MAX_TITLE_LINES = 6
"""A cover-page title never runs longer than this."""

MIN_TITLE_UPPERCASE = 0.6
"""Fraction of a title line's letters that must be uppercase.

Cover-page titles are set in capitals ("STANDARD MARKET DESIGN"); the body
prose that can follow them on the same page is not. Appendix L runs title
straight into prose with no blank separator and its date stamp only in the
footer, so without this the whole page becomes the title.
"""

_APPENDIX_MARKER = re.compile(r"^APPENDIX\s+[A-Z]\.?$", re.IGNORECASE)
"""The title follows this line, so anything gathered before it is discarded."""

_STAMP_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y")
"""The three forms ISO-NE uses. Two-digit years resolve by Python's %y rule
(00-68 -> 20xx), so "3/31/26" is 2026 -- a tariff will never carry a 1926 date."""

_MULTI_SENTENCE = re.compile(r"\.\s+[A-Z]")
"""A sentence boundary inside the remainder: prose, not a heading."""

_EFFECTIVE_DATE = re.compile(
    # ISO-NE separates the date from the docket with an en or em dash, so the
    # dash class is written as explicit escapes rather than literal characters.
    r"Effective\s+Date:\s*(?P<date>[^\u2013\u2014\-]+?)\s*[\u2013\u2014-]\s*"
    # ISO-NE writes the docket label as "Docket No.", "Docket #", or omits it.
    r"(?:Docket\s*(?:No\.?|#):?\s*)?(?P<docket>[A-Z]{2}\d[\w-]*)",
    re.IGNORECASE,
)
"""ISO-NE stamps every page with its effective date and FERC docket number."""


def _base_font(name: str) -> str:
    return _SUBSET_PREFIX.sub("", name)


def _roman_to_int(text: str) -> int | None:
    total, prev = 0, 0
    for char in reversed(text.upper()):
        value = _ROMAN.get(char)
        if value is None:
            return None
        total = total - value if value < prev else total + value
        prev = max(prev, value)
    return total or None


class ParsedId(NamedTuple):
    """A parsed section id plus the numbering scheme that produced it.

    The scheme matters for sequence validation: ``Appendix A`` and ``III.A.1``
    come from different numbering systems and their sort keys are not mutually
    comparable. Validating them as one sequence lets a stray id from one scheme
    invalidate every heading in the other.
    """

    display: str
    sort_key: tuple[int, ...]
    scheme: str


def parse_section_id(text: str) -> ParsedId | None:
    """Parse a section id at the start of ``text``.

    >>> parse_section_id("III.13.1.2 Qualification").display
    'III.13.1.2'
    >>> parse_section_id("III.A.1.1 Mission Statement").scheme
    'roman_letter'
    >>> parse_section_id("The Market Participant shall")
    """
    stripped = text.strip()

    if match := _ROMAN_SEGMENTED.match(stripped):
        major = _roman_to_int(match.group("roman"))
        if major is None:
            return None
        rest = match.group("rest")
        segments = [part for part in rest.split(".") if part]
        # Letters sort above numbers at the same depth, so a lettered segment can
        # never collide with a numbered one.
        key = tuple(int(seg) if seg.isdigit() else 1000 + ord(seg) for seg in segments)
        # Only the appendix form -- a letter directly after the roman major -- is
        # a separate numbering scheme. A lettered segment deeper in the tree
        # belongs to the body sequence and must be validated alongside it.
        scheme = "roman_letter" if segments and not segments[0].isdigit() else "roman_dotted"
        return ParsedId(f"{match.group('roman')}{rest}", (major, *key), scheme)

    if match := _LETTERED.match(stripped):
        letter = match.group("letter")
        return ParsedId(f"{match.group('kind')} {letter}", (ord(letter),), "lettered")

    if match := _PLAIN_DOTTED.match(stripped):
        num = match.group("num")
        # A bare integer is far more often a page number or list marker.
        if "." not in num:
            return None
        return ParsedId(num, tuple(int(part) for part in num.split(".")), "plain")

    return None


# --- Data model -----------------------------------------------------------


class Verdict(StrEnum):
    GO = "go"
    DEGRADED = "degraded"
    EMPTY = "empty"
    NO_GO = "no_go"


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    page_no: int
    top: float
    x0: float
    fontname: str
    size: float

    @property
    def is_bold(self) -> bool:
        return "bold" in self.fontname.lower()


@dataclass(frozen=True, slots=True)
class TextLayerReport:
    page_count: int
    total_chars: int
    median_chars_per_page: float
    empty_page_ratio: float
    image_page_ratio: float
    reserved_marker: bool

    @property
    def has_text_layer(self) -> bool:
        return self.median_chars_per_page >= MIN_CHARS_PER_PAGE

    @property
    def is_reserved_placeholder(self) -> bool:
        """An intentionally blank tariff section, not a failed extraction."""
        return (
            self.reserved_marker
            and self.total_chars < RESERVED_MAX_CHARS
            and self.image_page_ratio <= 0.5
        )


@dataclass(frozen=True, slots=True)
class FontProfile:
    body_font: str
    body_size: float
    inventory: tuple[tuple[str, float, int], ...]
    headings_typographically_distinct: bool


@dataclass(frozen=True, slots=True)
class RepeatedLine:
    text: str
    top: float
    page_ratio: float
    variants: int = 1
    """Distinct text variants seen at this position.

    ISO-NE footers carry a per-page effective date, so one footer legitimately
    has many textual forms at a single position -- which is why furniture is
    identified positionally rather than by matching text.
    """


@dataclass(frozen=True, slots=True)
class HeaderCandidate:
    line: Line
    section_id: str
    sort_key: tuple[int, ...]
    scheme: str
    signals: dict[str, float]
    score: float
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None and self.score >= HEADING_SCORE_THRESHOLD

    def demoted(self, reason: str) -> HeaderCandidate:
        return HeaderCandidate(
            self.line,
            self.section_id,
            self.sort_key,
            self.scheme,
            self.signals,
            self.score,
            reason,
        )


@dataclass(frozen=True, slots=True)
class SequenceReport:
    accepted_ids: tuple[str, ...]
    violations: tuple[str, ...]
    gaps: tuple[str, ...]
    demoted: int = 0


@dataclass(frozen=True, slots=True)
class CrossRefAudit:
    """How the document's cross-references interacted with heading detection."""

    inline_refs_seen: int
    prose_lines_starting_with_id: int
    misread_as_headings: int
    """Must be zero: a promoted reference fabricates a section that does not
    exist and silently corrupts every boundary after it."""


@dataclass(slots=True)
class SpikeReport:
    path: Path
    text_layer: TextLayerReport
    fonts: FontProfile | None
    repeated_lines: tuple[RepeatedLine, ...]
    candidates: tuple[HeaderCandidate, ...]
    sequence: SequenceReport | None
    cross_refs: CrossRefAudit
    toc_pages: tuple[int, ...] = ()
    effective_dates: tuple[EffectiveDate, ...] = ()
    title: str = ""
    title_source: TitleSource = TitleSource.FILENAME
    notes: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> tuple[HeaderCandidate, ...]:
        return tuple(c for c in self.candidates if c.accepted)

    @property
    def verdict(self) -> Verdict:
        if self.text_layer.is_reserved_placeholder:
            return Verdict.EMPTY
        if not self.text_layer.has_text_layer:
            return Verdict.NO_GO
        if self.cross_refs.misread_as_headings:
            return Verdict.DEGRADED
        if self.sequence and self.sequence.violations:
            return Verdict.DEGRADED
        if self.fonts is None or not self.fonts.headings_typographically_distinct:
            return Verdict.DEGRADED
        return Verdict.GO


# --- Extraction -----------------------------------------------------------


def _dominant_font(chars: list[dict[str, Any]]) -> tuple[str, float]:
    if not chars:
        return ("", 0.0)
    counter = Counter(
        (_base_font(str(c.get("fontname", ""))), round(float(c.get("size", 0)), 1)) for c in chars
    )
    (font, size), _ = counter.most_common(1)[0]
    return (font, size)


def _read_lines(pdf_path: Path) -> tuple[list[Line], TextLayerReport, float]:
    lines: list[Line] = []
    chars_per_page: list[int] = []
    pages_with_images = 0
    page_height = 0.0
    reserved = False

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            page_height = max(page_height, float(page.height))
            chars_per_page.append(len(page.chars))
            if page.images:
                pages_with_images += 1
            for raw in page.extract_text_lines():
                text = str(raw.get("text", "")).strip()
                if not text:
                    continue
                if _RESERVED.search(text):
                    reserved = True
                font, size = _dominant_font(list(raw.get("chars", [])))
                lines.append(
                    Line(
                        text=text,
                        page_no=page_no,
                        top=round(float(raw.get("top", 0.0)), 1),
                        x0=round(float(raw.get("x0", 0.0)), 1),
                        fontname=font,
                        size=size,
                    )
                )

    page_count = len(chars_per_page)
    report = TextLayerReport(
        page_count=page_count,
        total_chars=sum(chars_per_page),
        median_chars_per_page=statistics.median(chars_per_page) if chars_per_page else 0.0,
        empty_page_ratio=(
            sum(1 for n in chars_per_page if n < MIN_CHARS_PER_PAGE) / page_count
            if page_count
            else 1.0
        ),
        image_page_ratio=pages_with_images / page_count if page_count else 0.0,
        reserved_marker=reserved,
    )
    return lines, report, page_height


def _cluster_positions(tops: list[float]) -> list[list[float]]:
    """Group vertical positions that are the same line allowing for drift."""
    clusters: list[list[float]] = []
    for top in sorted(tops):
        if clusters and top - clusters[-1][-1] <= BAND_TOLERANCE:
            clusters[-1].append(top)
        else:
            clusters.append([top])
    return clusters


def _find_repeated_lines(
    lines: list[Line], page_count: int, page_height: float
) -> list[RepeatedLine]:
    """Find running headers and footers positionally, with clustering.

    Three measured facts shape this, none of which held under the obvious
    implementation:

    1. Matching on text fails -- ISO-NE stamps each page with its own effective
       date, so one footer takes several textual forms.
    2. Exact position fails -- the footer drifts between bands (710.5 on 97
       pages, 709.2 on 83, 734.7 on 23), so no single height dominates. Nearby
       positions must be clustered.
    3. Position alone over-matches -- the first body line of each page is just as
       positionally stable as a header. What separates them is variance: a footer
       has 3 text forms across 235 pages, body text has one per page.
    """
    if page_count < 2 or page_height <= 0:
        return []

    top_band = page_height * MARGIN_BAND_RATIO
    bottom_band = page_height * (1.0 - MARGIN_BAND_RATIO)

    margin_lines = [ln for ln in lines if not (top_band < ln.top < bottom_band)]
    if not margin_lines:
        return []

    repeated: list[RepeatedLine] = []
    for cluster in _cluster_positions([ln.top for ln in margin_lines]):
        members = {round(t, 1) for t in cluster}
        group = [ln for ln in margin_lines if round(ln.top, 1) in members]
        pages = {ln.page_no for ln in group}
        texts = Counter(re.sub(r"\d+", "#", ln.text) for ln in group)
        coverage = len(pages) / page_count
        if len(pages) < MIN_FURNITURE_PAGES or coverage < REPEATED_LINE_PAGE_RATIO:
            continue
        if len(texts) > max(1.0, MAX_FURNITURE_VARIANT_RATIO * len(pages)):
            continue
        repeated.append(
            RepeatedLine(
                text=texts.most_common(1)[0][0],
                top=round(sum(cluster) / len(cluster), 1),
                page_ratio=coverage,
                variants=len(texts),
            )
        )
    return sorted(repeated, key=lambda r: r.top)


def _parse_stamp_date(text: str) -> date | None:
    for fmt in _STAMP_DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _looks_like_title(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    uppercase = sum(1 for c in letters if c.isupper()) / len(letters)
    return uppercase >= MIN_TITLE_UPPERCASE


def _extract_title(
    lines: list[Line], accepted: list[HeaderCandidate], fallback: str
) -> tuple[str, TitleSource]:
    """Recover a document title, recording which of three sources supplied it.

    Measured across Market Rule 1: twelve documents carry a cover page, three
    open directly on their first heading, and two are bare ``[RESERVED.]`` with
    no title at all.

    Titles are kept exactly as printed rather than case-normalised. "AUCTION
    REVENUE RIGHTS AND INCREMENTAL ARRs" is the document's own rendering, and
    title-casing it would produce "Arrs".
    """
    page_one = [line for line in lines if line.page_no == 1]

    # A cover page never opens with a section id; a section file always does.
    if page_one and parse_section_id(page_one[0].text) is None:
        parts: list[str] = []
        for line in page_one:
            text = line.text.strip()
            if _EFFECTIVE_DATE.search(text):
                break
            if _COVER_BOILERPLATE.match(text) or _RESERVED.match(text):
                continue
            if _APPENDIX_MARKER.match(text):
                # Appendix K omits the masthead, so the marker -- not a fixed
                # line offset -- is what anchors the title.
                parts.clear()
                continue
            if parse_section_id(text) or not _looks_like_title(text):
                break
            parts.append(text)
            if len(parts) >= MAX_TITLE_LINES:
                break
        title = " ".join(parts).strip()
        if title:
            return title, TitleSource.COVER_PAGE

        # A reserved appendix has a cover page whose only content *is* the
        # reserved marker. "APPENDIX B RESERVED FOR FUTURE USE" is the
        # document's own wording and beats falling through to a filename.
        marker_parts = [
            line.text.strip()
            for line in page_one
            if not _COVER_BOILERPLATE.match(line.text.strip())
            and not _EFFECTIVE_DATE.search(line.text)
        ]
        if marker_parts:
            return " ".join(marker_parts).strip(), TitleSource.COVER_PAGE

    if accepted:
        first = accepted[0]
        remainder, _ = _heading_shape(first.line.text, first.section_id)
        if remainder:
            return remainder.rstrip("."), TitleSource.FIRST_HEADING

    return fallback, TitleSource.FILENAME


def _find_effective_dates(lines: list[Line]) -> list[EffectiveDate]:
    """Collect each effective-date stamp and the pages it governs.

    Versioning metadata, not furniture to strip: ISO-NE gives effective dates at
    finer granularity than the document, so different sections of one PDF are in
    force from different dates. Retaining the page numbers is what lets Phase 1
    attribute a date to a section spanning a given page range.
    """
    pages_by_stamp: dict[tuple[str, str], set[int]] = defaultdict(set)
    for line in lines:
        if match := _EFFECTIVE_DATE.search(line.text):
            key = (match.group("date").strip(), match.group("docket").strip())
            pages_by_stamp[key].add(line.page_no)

    stamps = [
        EffectiveDate(
            date_text=date_text,
            docket=docket,
            pages=tuple(sorted(pages)),
            date=_parse_stamp_date(date_text),
        )
        for (date_text, docket), pages in pages_by_stamp.items()
    ]
    return sorted(stamps, key=lambda eff: (-len(eff.pages), eff.date_text))


def _find_toc_pages(lines: list[Line], furniture_tops: set[float]) -> set[int]:
    """Identify contents pages, whose entries mimic headings perfectly.

    A table of contents lists every section id with its title, which every
    signal in the cascade reads as a genuine heading. Detect the page rather
    than the line: a page densely packed with ids is a contents page.
    """
    by_page: dict[int, list[Line]] = defaultdict(list)
    for line in lines:
        if not _in_band(line.top, furniture_tops):
            by_page[line.page_no].append(line)

    toc: set[int] = set()
    for page_no, page_lines in by_page.items():
        if not page_lines:
            continue
        if any(line.text.strip().lower().startswith("table of contents") for line in page_lines):
            toc.add(page_no)
            continue
        ids = sum(1 for line in page_lines if parse_section_id(line.text))
        if len(page_lines) >= 5 and ids / len(page_lines) >= TOC_ID_DENSITY:
            toc.add(page_no)
    return toc


# --- Detection ------------------------------------------------------------


def _in_band(top: float, furniture_tops: set[float]) -> bool:
    return any(abs(top - t) <= BAND_TOLERANCE for t in furniture_tops)


def _heading_shape(text: str, section_id: str) -> tuple[str, str | None]:
    """Split a line into its remainder and any hard disqualifying reason.

    The title-case rule is a *gate*, not a weighted signal. A heading's number is
    essentially always followed by a title -- ``III.13. Forward Capacity
    Market.`` -- whereas wrapped prose reads ``III.14 shall be construed to
    limit...``. Scoring these as merely one weak signal short lets them through.
    Missing an oddly-formatted heading costs one section; admitting a false one
    fabricates a section and corrupts every boundary after it.

    Note that ISO-NE headings legitimately end in a period, so "ends with a
    period" is *not* a prose signal here -- a sentence boundary *inside* the
    remainder is.
    """
    remainder = text.strip()[len(section_id) :].strip(" .:—-")
    if not remainder:
        return "", None
    # ISO-NE repeals subsections in place, leaving
    # "III.13.1.1.2.5.2. [Reserved.]". These are genuine nodes of the section
    # tree -- dropping them manufactures gaps and leaves the tree incomplete --
    # but "[" is not an uppercase letter, so the title-case gate below rejects
    # them unless they are recognised first.
    if _RESERVED.match(remainder):
        return remainder, None
    if not remainder[:1].isupper():
        return remainder, "remainder is lowercase prose, not a title"
    if len(remainder) > MAX_HEADING_REMAINDER:
        return remainder, "remainder is too long to be a title"
    if _MULTI_SENTENCE.search(remainder):
        return remainder, "remainder spans a sentence boundary"
    return remainder, None


def _profile_fonts(lines: list[Line], furniture_tops: set[float]) -> FontProfile:
    body_lines = [line for line in lines if not _in_band(line.top, furniture_tops)]
    counter = Counter((line.fontname, line.size) for line in body_lines)
    if not counter:
        return FontProfile("", 0.0, (), False)

    (body_font, body_size), _ = counter.most_common(1)[0]

    # Distinctness is judged only over lines that pass the prose gate. Judging it
    # over every line starting with a section id lets wrapped body prose
    # outnumber the real headings and wrongly report "not distinct".
    plausible = [
        line
        for line in body_lines
        if (parsed := parse_section_id(line.text))
        and _heading_shape(line.text, parsed.display)[1] is None
    ]
    distinct = bool(plausible) and sum(
        1
        for line in plausible
        if line.is_bold or line.size > body_size + 0.5 or line.fontname != body_font
    ) >= max(1, len(plausible) // 2)

    inventory = tuple(sorted(((f, s, n) for (f, s), n in counter.items()), key=lambda t: -t[2]))
    return FontProfile(body_font, body_size, inventory, distinct)


def _score_candidates(
    lines: list[Line],
    fonts: FontProfile,
    furniture_tops: set[float],
    toc_pages: set[int],
) -> list[HeaderCandidate]:
    """Score each numbered line across independent signal families.

    When the document's headings are not typographically distinct, the typography
    signal is dropped rather than scored as zero -- otherwise every real heading
    in a flat document would be penalised for a property the document does not
    have.
    """
    candidates: list[HeaderCandidate] = []
    use_typography = fonts.headings_typographically_distinct

    for line in lines:
        parsed = parse_section_id(line.text)
        if parsed is None:
            continue

        if line.page_no in toc_pages:
            candidates.append(
                HeaderCandidate(
                    line,
                    parsed.display,
                    parsed.sort_key,
                    parsed.scheme,
                    {},
                    0.0,
                    "on a table-of-contents page",
                )
            )
            continue

        remainder, reason = _heading_shape(line.text, parsed.display)
        if reason is not None:
            candidates.append(
                HeaderCandidate(
                    line, parsed.display, parsed.sort_key, parsed.scheme, {}, 0.0, reason
                )
            )
            continue

        signals: dict[str, float] = {
            "position": 0.0 if _in_band(line.top, furniture_tops) else 1.0,
            "shape": 1.0 if len(remainder) < 90 else 0.0,
        }
        if use_typography:
            signals["typography"] = (
                1.0 if (line.is_bold or line.size > fonts.body_size + 0.5) else 0.0
            )

        score = sum(signals.values()) / len(signals)
        candidates.append(
            HeaderCandidate(
                line, parsed.display, parsed.sort_key, parsed.scheme, signals, round(score, 3)
            )
        )
    return candidates


def _longest_increasing(keys: list[tuple[int, ...]]) -> set[int]:
    """Indices forming a longest strictly-increasing subsequence of ``keys``."""
    tails: list[tuple[int, ...]] = []
    tails_idx: list[int] = []
    prev = [-1] * len(keys)

    for i, key in enumerate(keys):
        pos = bisect.bisect_left(tails, key)
        prev[i] = tails_idx[pos - 1] if pos > 0 else -1
        if pos == len(tails):
            tails.append(key)
            tails_idx.append(i)
        else:
            tails[pos] = key
            tails_idx[pos] = i

    keep: set[int] = set()
    node = tails_idx[-1] if tails_idx else -1
    while node != -1:
        keep.add(node)
        node = prev[node]
    return keep


def _demote_out_of_sequence(candidates: list[HeaderCandidate]) -> tuple[list[HeaderCandidate], int]:
    """Reclassify candidates that break the id sequence as cross-references.

    Sequence consistency is the strongest signal in the cascade, and the point of
    computing it is to *act* on it. Three passes:

    1. A section id occurring more than once keeps only its best-scoring
       occurrence -- the real heading beats a wrapped reference on typography.
    2. Candidates are grouped by numbering scheme. ``Appendix A`` and
       ``III.A.1`` are not mutually comparable, and validating them as one
       sequence lets a stray id from one scheme invalidate the other entirely.
    3. Within each scheme, keep a *longest increasing subsequence* rather than
       walking greedily. A greedy walk lets one bad early acceptance set a high
       watermark that demotes every legitimate heading after it -- which is
       exactly what happened on the real corpus, costing 180 real headings in
       Appendix A alone. An LIS drops the outlier instead of the tail.
    """
    result = list(candidates)
    demoted = 0

    best_by_id: dict[str, int] = {}
    for idx, cand in enumerate(result):
        if not cand.accepted:
            continue
        prior = best_by_id.get(cand.section_id)
        if prior is None:
            best_by_id[cand.section_id] = idx
        elif cand.score > result[prior].score:
            result[prior] = result[prior].demoted("duplicate section id, lower-scoring occurrence")
            demoted += 1
            best_by_id[cand.section_id] = idx
        else:
            result[idx] = cand.demoted("duplicate section id, lower-scoring occurrence")
            demoted += 1

    by_scheme: dict[str, list[int]] = defaultdict(list)
    for idx, cand in enumerate(result):
        if cand.accepted:
            by_scheme[cand.scheme].append(idx)

    for indices in by_scheme.values():
        keep = _longest_increasing([result[i].sort_key for i in indices])
        for position, idx in enumerate(indices):
            if position not in keep:
                result[idx] = result[idx].demoted("breaks the monotonic id sequence")
                demoted += 1

    return result, demoted


def _validate_sequence(candidates: list[HeaderCandidate], demoted: int) -> SequenceReport:
    """Report residual sequence problems, per numbering scheme.

    Schemes are validated independently for the same reason they are demoted
    independently: ``Appendix A`` and ``III.A.1`` have incomparable sort keys, so
    interleaving them manufactures violations that are artefacts of the
    comparison rather than defects in the document.
    """
    accepted = [c for c in candidates if c.accepted]
    violations: list[str] = []
    gaps: list[str] = []

    by_scheme: dict[str, list[HeaderCandidate]] = defaultdict(list)
    for cand in accepted:
        by_scheme[cand.scheme].append(cand)

    for group in by_scheme.values():
        previous: HeaderCandidate | None = None
        for current in group:
            if previous is not None:
                if current.sort_key <= previous.sort_key:
                    violations.append(
                        f"{current.section_id} (p.{current.line.page_no}) "
                        f"does not follow {previous.section_id}"
                    )
                elif len(current.sort_key) > len(previous.sort_key) + 1:
                    gaps.append(
                        f"{previous.section_id} -> {current.section_id} "
                        "(intermediate level missing)"
                    )
                elif (
                    len(current.sort_key) == len(previous.sort_key)
                    and current.sort_key[:-1] == previous.sort_key[:-1]
                    and current.sort_key[-1] > previous.sort_key[-1] + 1
                ):
                    gaps.append(f"{previous.section_id} -> {current.section_id}")
            previous = current

    return SequenceReport(
        accepted_ids=tuple(c.section_id for c in accepted),
        violations=tuple(violations),
        gaps=tuple(gaps),
        demoted=demoted,
    )


def _audit_cross_refs(lines: list[Line], candidates: list[HeaderCandidate]) -> CrossRefAudit:
    inline_seen = 0
    for line in lines:
        stripped = line.text.strip()
        inline_seen += sum(1 for m in _INLINE_REF.finditer(stripped) if m.start() > 0)

    prose_starts = [c for c in candidates if c.rejected_reason is not None]
    misread = sum(1 for c in prose_starts if c.accepted)

    return CrossRefAudit(
        inline_refs_seen=inline_seen,
        prose_lines_starting_with_id=len(prose_starts),
        misread_as_headings=misread,
    )


def spike_document(pdf_path: Path) -> SpikeReport:
    """Run the full diagnostic cascade over one PDF."""
    lines, text_layer, page_height = _read_lines(pdf_path)

    if text_layer.is_reserved_placeholder or not text_layer.has_text_layer:
        note = (
            "Intentionally blank tariff section ([RESERVED]). Excluded from the corpus, "
            "but nothing is lost -- there is no content to recover."
            if text_layer.is_reserved_placeholder
            else "No usable text layer -- this is a scan. Claude's citations require "
            "extractable text, so it cannot be cited and must be dropped from the "
            "corpus (state the exclusion in the README)."
        )
        return SpikeReport(
            path=pdf_path,
            text_layer=text_layer,
            fonts=None,
            repeated_lines=(),
            candidates=(),
            sequence=None,
            cross_refs=CrossRefAudit(0, 0, 0),
            title=_extract_title(lines, [], pdf_path.stem)[0],
            title_source=_extract_title(lines, [], pdf_path.stem)[1],
            notes=[note],
        )

    repeated = _find_repeated_lines(lines, text_layer.page_count, page_height)
    furniture_tops = {r.top for r in repeated}
    toc_pages = _find_toc_pages(lines, furniture_tops)
    fonts = _profile_fonts(lines, furniture_tops)
    candidates = _score_candidates(lines, fonts, furniture_tops, toc_pages)
    candidates, demoted = _demote_out_of_sequence(candidates)
    sequence = _validate_sequence(candidates, demoted)
    cross_refs = _audit_cross_refs(lines, candidates)
    effective_dates = _find_effective_dates(lines)
    title, title_source = _extract_title(
        lines, [c for c in candidates if c.accepted], pdf_path.stem
    )

    notes: list[str] = []
    if not fonts.headings_typographically_distinct:
        notes.append(
            "Headings are not typographically distinct from body text. The typography "
            "signal is disabled for this document; detection relies on numbering, the "
            "prose gate, and sequence consistency alone."
        )
    if not repeated and text_layer.page_count >= 2:
        notes.append(
            "No running header/footer detected. Verify manually -- if furniture is "
            "present but undetected it will pollute every chunk and shift offsets."
        )
    if cross_refs.misread_as_headings:
        notes.append(
            f"{cross_refs.misread_as_headings} wrapped cross-reference(s) were accepted as "
            "headings. This must be zero: a promoted reference fabricates a section and "
            "corrupts every boundary after it."
        )
    if sequence.violations:
        notes.append(
            f"{len(sequence.violations)} sequence violation(s) survived demotion. "
            "Inspect these: they usually mean an unsupported numbering scheme."
        )
    if len(effective_dates) > 1:
        notes.append(
            f"{len(effective_dates)} distinct effective dates in one document -- ISO-NE "
            "versions at finer granularity than the file, so capture these per section."
        )

    return SpikeReport(
        path=pdf_path,
        text_layer=text_layer,
        fonts=fonts,
        repeated_lines=tuple(repeated),
        candidates=tuple(candidates),
        sequence=sequence,
        cross_refs=cross_refs,
        toc_pages=tuple(sorted(toc_pages)),
        effective_dates=tuple(effective_dates),
        title=title,
        title_source=title_source,
        notes=notes,
    )


# --- Rendering ------------------------------------------------------------

_VERDICT_STYLE = {
    Verdict.GO: ("bold green", "structure is recoverable with the full cascade"),
    Verdict.DEGRADED: ("bold yellow", "usable, but a signal is unavailable or noisy"),
    Verdict.EMPTY: ("dim", "intentionally blank tariff section; nothing to recover"),
    Verdict.NO_GO: ("bold red", "drop from the corpus and state the exclusion"),
}


def render_report(report: SpikeReport, console: Console, *, show_candidates: int = 0) -> None:
    """Print a per-document diagnostic report."""
    style, gloss = _VERDICT_STYLE[report.verdict]
    console.print()
    console.rule(f"[{style}]{report.path.name} — {report.verdict.value.upper()}[/{style}]")
    console.print(f"[dim]{gloss}[/dim]\n")

    if report.title:
        console.print(f"[bold]Title[/bold]      {report.title}  [dim]({report.title_source})[/dim]")

    layer = report.text_layer
    console.print(
        f"[bold]Text layer[/bold]  {layer.page_count} pages, "
        f"{layer.total_chars:,} chars, median {layer.median_chars_per_page:,.0f}/page, "
        f"{layer.image_page_ratio:.0%} of pages carry images"
    )

    if report.verdict in (Verdict.NO_GO, Verdict.EMPTY):
        for note in report.notes:
            console.print(f"\n[{'red' if report.verdict is Verdict.NO_GO else 'dim'}]![/] {note}")
        return

    if report.fonts:
        fonts = report.fonts
        mark = (
            "[green]yes[/green]"
            if fonts.headings_typographically_distinct
            else "[yellow]no[/yellow]"
        )
        console.print(
            f"[bold]Fonts[/bold]      body = {fonts.body_font} @ {fonts.body_size}pt; "
            f"headings distinct: {mark}"
        )

    if report.repeated_lines:
        console.print("[bold]Furniture[/bold]  running header/footer positions:")
        for line in report.repeated_lines:
            variants = f" ({line.variants} text variants)" if line.variants > 1 else ""
            console.print(
                f"             top={line.top:6.1f}  {line.page_ratio:.0%}{variants}  {line.text!r}"
            )
    else:
        console.print("[bold]Furniture[/bold]  [yellow]none detected[/yellow]")

    if report.effective_dates:
        console.print("[bold]Versions[/bold]   effective dates stamped on pages:")
        for eff in report.effective_dates[:6]:
            console.print(f"             {eff.date_text}  docket {eff.docket}  ({eff.pages} pages)")

    if report.toc_pages:
        shown = ", ".join(str(p) for p in report.toc_pages[:12])
        more = "" if len(report.toc_pages) <= 12 else f" (+{len(report.toc_pages) - 12} more)"
        console.print(f"[bold]Contents[/bold]   pages excluded as TOC: {shown}{more}")

    cr = report.cross_refs
    misread_style = "green" if cr.misread_as_headings == 0 else "bold red"
    console.print(
        f"[bold]Xrefs[/bold]      {cr.inline_refs_seen} inline; "
        f"{cr.prose_lines_starting_with_id} lines gated; "
        f"[{misread_style}]{cr.misread_as_headings} misread as headings[/{misread_style}]"
    )

    if report.sequence:
        seq = report.sequence
        console.print(
            f"[bold]Sequence[/bold]   {len(seq.accepted_ids)} headings accepted, "
            f"{seq.demoted} demoted, {len(seq.violations)} violations, {len(seq.gaps)} gaps"
        )
        for violation in seq.violations[:10]:
            console.print(f"             [red]violation[/red] {violation}")
        for gap in seq.gaps[:6]:
            console.print(f"             [yellow]gap[/yellow] {gap}")

    if show_candidates:
        table = Table(title="Header candidates", show_lines=False)
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("p", justify="right")
        table.add_column("score", justify="right")
        table.add_column("signals")
        table.add_column("text / rejection")
        for cand in report.candidates[:show_candidates]:
            signals = (
                ",".join(f"{k}={v:g}" for k, v in cand.signals.items())
                if cand.signals
                else "[dim]gated[/dim]"
            )
            detail = cand.rejected_reason or cand.line.text[:52]
            table.add_row(
                cand.section_id,
                str(cand.line.page_no),
                f"{cand.score:.2f}",
                signals,
                detail,
                style=None if cand.accepted else "dim",
            )
        console.print()
        console.print(table)

    for note in report.notes:
        console.print(f"\n[yellow]![/yellow] {note}")
