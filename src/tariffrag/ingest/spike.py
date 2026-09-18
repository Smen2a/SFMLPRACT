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
    Recoverable, but the typography signal is unusable (headings are not
    visually distinct), so detection leans on numbering and sequence alone.
``NO_GO``
    No usable text layer. Claude's citations require extractable text, so an
    image-only document is not citable at all and must be dropped from the
    corpus, with the exclusion stated in the README.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pdfplumber
from rich.console import Console
from rich.table import Table

__all__ = [
    "SpikeReport",
    "Verdict",
    "parse_section_id",
    "render_report",
    "spike_document",
]

# --- Thresholds -----------------------------------------------------------

MIN_CHARS_PER_PAGE = 100
"""Below this median, treat the document as having no usable text layer."""

REPEATED_LINE_PAGE_RATIO = 0.6
"""Fraction of pages a line must recur on (at the same height) to be furniture."""

BAND_TOLERANCE = 3.0
"""Vertical tolerance, in points, for "the same height" on another page."""

MARGIN_BAND_RATIO = 0.12
"""Running headers/footers live in the top/bottom 12% of the page.

Without this positional constraint, body text that happens to repeat at the
same height on enough pages is misclassified as furniture -- which the spike
caught on its first run against the fixtures.
"""

HEADING_SCORE_THRESHOLD = 0.6

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

_ROMAN_DOTTED = re.compile(r"^(?P<roman>[IVXLCDM]+)(?P<rest>(?:\.\d+)+)\b")
_PLAIN_DOTTED = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\b")
_LETTERED = re.compile(r"^(?P<kind>Appendix|Attachment|Schedule)\s+(?P<letter>[A-Z])\b")

_INLINE_REF = re.compile(
    r"\b(?:Section|Sections|Appendix|Appendices|Attachment|Schedule)\s+"
    r"(?:[IVXLCDM]+(?:\.\d+)+|\d+(?:\.\d+)*|[A-Z])\b"
)
"""Matches a cross-reference such as ``as defined in Section III.12.2``."""


def _roman_to_int(text: str) -> int | None:
    total, prev = 0, 0
    for char in reversed(text.upper()):
        value = _ROMAN.get(char)
        if value is None:
            return None
        total = total - value if value < prev else total + value
        prev = max(prev, value)
    return total or None


def parse_section_id(text: str) -> tuple[str, tuple[int, ...]] | None:
    """Parse a section id at the start of ``text``.

    Returns the normalized display id and an integer tuple used for ordering,
    or ``None`` if the text does not begin with a recognizable id.

    >>> parse_section_id("III.13.1.2 Qualification")
    ('III.13.1.2', (3, 13, 1, 2))
    >>> parse_section_id("The Market Participant shall")
    """
    stripped = text.strip()

    if match := _ROMAN_DOTTED.match(stripped):
        roman = match.group("roman")
        major = _roman_to_int(roman)
        if major is None:
            return None
        minors = tuple(int(part) for part in match.group("rest").split(".") if part)
        return f"{roman}{match.group('rest')}", (major, *minors)

    if match := _LETTERED.match(stripped):
        letter = match.group("letter")
        kind = match.group("kind")
        return f"{kind} {letter}", (ord(letter),)

    if match := _PLAIN_DOTTED.match(stripped):
        num = match.group("num")
        # A bare integer is far more often a page number or list marker than a
        # section heading, so require at least one dot.
        if "." not in num:
            return None
        return num, tuple(int(part) for part in num.split("."))

    return None


# --- Data model -----------------------------------------------------------


class Verdict(StrEnum):
    GO = "go"
    DEGRADED = "degraded"
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

    @property
    def has_text_layer(self) -> bool:
        return self.median_chars_per_page >= MIN_CHARS_PER_PAGE


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


@dataclass(frozen=True, slots=True)
class HeaderCandidate:
    line: Line
    section_id: str
    sort_key: tuple[int, ...]
    signals: dict[str, float]
    score: float
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None and self.score >= HEADING_SCORE_THRESHOLD


@dataclass(frozen=True, slots=True)
class SequenceReport:
    accepted_ids: tuple[str, ...]
    violations: tuple[str, ...]
    gaps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CrossRefAudit:
    """How the document's cross-references interacted with heading detection."""

    inline_refs_seen: int
    """Mid-line references such as ``as defined in Section III.12.2``."""
    prose_lines_starting_with_id: int
    """Wrapped prose whose line happens to begin with a bare section id.

    This is the dominant hazard: line wrapping puts ``III.14 shall be construed
    ...`` at the start of a line, where it is indistinguishable from a heading by
    numbering alone.
    """
    misread_as_headings: int
    """How many of those were accepted. Must be zero.

    A promoted reference fabricates a section that does not exist and silently
    corrupts every downstream boundary, so this is the metric that matters.
    """


@dataclass(slots=True)
class SpikeReport:
    path: Path
    text_layer: TextLayerReport
    fonts: FontProfile | None
    repeated_lines: tuple[RepeatedLine, ...]
    candidates: tuple[HeaderCandidate, ...]
    sequence: SequenceReport | None
    cross_refs: CrossRefAudit
    notes: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> tuple[HeaderCandidate, ...]:
        return tuple(c for c in self.candidates if c.accepted)

    @property
    def verdict(self) -> Verdict:
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
        (str(c.get("fontname", "")), round(float(c.get("size", 0)), 1)) for c in chars
    )
    (font, size), _ = counter.most_common(1)[0]
    return (font, size)


def _read_lines(pdf_path: Path) -> tuple[list[Line], TextLayerReport, float]:
    lines: list[Line] = []
    chars_per_page: list[int] = []
    page_height = 0.0

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            page_height = max(page_height, float(page.height))
            chars_per_page.append(len(page.chars))
            for raw in page.extract_text_lines():
                text = str(raw.get("text", "")).strip()
                if not text:
                    continue
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
    )
    return lines, report, page_height


def _find_repeated_lines(
    lines: list[Line], page_count: int, page_height: float
) -> list[RepeatedLine]:
    """Find running headers and footers: same text, same height, most pages.

    These must be stripped before char offsets are assigned, or they pollute
    every chunk and shift every citation span.
    """
    if page_count < 2 or page_height <= 0:
        return []

    top_band = page_height * MARGIN_BAND_RATIO
    bottom_band = page_height * (1.0 - MARGIN_BAND_RATIO)

    buckets: dict[tuple[str, float], set[int]] = defaultdict(set)
    for line in lines:
        if top_band < line.top < bottom_band:
            continue  # body text, not page furniture
        # Digits are normalized away so "Page 3" and "Page 4" collapse together.
        normalized = re.sub(r"\d+", "#", line.text)
        buckets[(normalized, line.top)].add(line.page_no)

    repeated = [
        RepeatedLine(text=text, top=top, page_ratio=len(pages) / page_count)
        for (text, top), pages in buckets.items()
        if len(pages) / page_count >= REPEATED_LINE_PAGE_RATIO
    ]
    return sorted(repeated, key=lambda r: r.top)


# --- Detection -----------------------------------------------------------


def _in_band(top: float, furniture_tops: set[float]) -> bool:
    return any(abs(top - t) <= BAND_TOLERANCE for t in furniture_tops)


def _heading_shape(text: str, section_id: str) -> tuple[str, str | None]:
    """Split a line into its remainder and any hard disqualifying reason.

    The title-case rule is a *gate*, not a weighted signal. A heading's number is
    essentially always followed by a title -- ``III.13.1 Qualification Process``
    -- whereas wrapped prose reads ``III.14 shall be construed to limit...``.
    Scoring these as merely "one weak signal short" lets them through, which is
    what the first run against the fixtures showed. Missing an oddly-formatted
    heading costs one section; admitting a false one corrupts every boundary
    after it, so the asymmetry justifies a hard rule.
    """
    remainder = text.strip()[len(section_id) :].strip(" .:—-")
    if not remainder:
        return "", None
    if not remainder[:1].isupper():
        return remainder, "remainder is lowercase prose, not a title"
    if remainder.endswith(".") and len(remainder) > 60:
        return remainder, "line is a terminated sentence, not a heading"
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
        and _heading_shape(line.text, parsed[0])[1] is None
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
) -> list[HeaderCandidate]:
    """Score each numbered line across independent signal families.

    A single regex cannot separate a heading from a wrapped cross-reference, so
    the prose gate runs first and the surviving lines are scored on the remaining
    signals. When the document's headings are not typographically distinct, the
    typography signal is dropped rather than scored as zero -- otherwise every
    real heading in a flat document would be penalised for a property the
    document simply does not have.
    """
    candidates: list[HeaderCandidate] = []
    use_typography = fonts.headings_typographically_distinct

    for line in lines:
        parsed = parse_section_id(line.text)
        if parsed is None:
            continue
        section_id, sort_key = parsed
        remainder, reason = _heading_shape(line.text, section_id)

        if reason is not None:
            candidates.append(
                HeaderCandidate(line, section_id, sort_key, {}, 0.0, rejected_reason=reason)
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
        candidates.append(HeaderCandidate(line, section_id, sort_key, signals, round(score, 3)))
    return candidates


def _validate_sequence(candidates: list[HeaderCandidate]) -> SequenceReport:
    """Check that accepted ids form a monotone, depth-consistent sequence.

    This is the strongest signal and the one naive parsers omit: a candidate that
    breaks monotonicity is almost always a cross-reference, and a gap means a
    real heading was missed.
    """
    accepted = [c for c in candidates if c.accepted]
    violations: list[str] = []
    gaps: list[str] = []

    previous: HeaderCandidate | None = None
    for current in accepted:
        if previous is not None:
            if current.sort_key <= previous.sort_key:
                violations.append(
                    f"{current.section_id} (p.{current.line.page_no}) "
                    f"does not follow {previous.section_id}"
                )
            elif len(current.sort_key) > len(previous.sort_key) + 1:
                violations.append(
                    f"{current.section_id} jumps more than one level below {previous.section_id}"
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

    if not text_layer.has_text_layer:
        return SpikeReport(
            path=pdf_path,
            text_layer=text_layer,
            fonts=None,
            repeated_lines=(),
            candidates=(),
            sequence=None,
            cross_refs=CrossRefAudit(0, 0, 0),
            notes=[
                "No usable text layer -- this document is almost certainly a scan. "
                "Claude's citations require extractable text, so it cannot be cited "
                "and must be dropped from the corpus (state the exclusion in the README)."
            ],
        )

    repeated = _find_repeated_lines(lines, text_layer.page_count, page_height)
    furniture_tops = {r.top for r in repeated}
    fonts = _profile_fonts(lines, furniture_tops)
    candidates = _score_candidates(lines, fonts, furniture_tops)
    sequence = _validate_sequence(candidates)
    cross_refs = _audit_cross_refs(lines, candidates)

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
            f"{len(sequence.violations)} sequence violation(s). Ids should increase "
            "monotonically; violations usually mean false headings were admitted."
        )

    return SpikeReport(
        path=pdf_path,
        text_layer=text_layer,
        fonts=fonts,
        repeated_lines=tuple(repeated),
        candidates=tuple(candidates),
        sequence=sequence,
        cross_refs=cross_refs,
        notes=notes,
    )


# --- Rendering ------------------------------------------------------------

_VERDICT_STYLE = {
    Verdict.GO: ("bold green", "structure is recoverable with the full cascade"),
    Verdict.DEGRADED: ("bold yellow", "usable, but a signal is unavailable or noisy"),
    Verdict.NO_GO: ("bold red", "drop from the corpus and state the exclusion"),
}


def render_report(report: SpikeReport, console: Console, *, show_candidates: int = 0) -> None:
    """Print a per-document diagnostic report."""
    style, gloss = _VERDICT_STYLE[report.verdict]
    console.print()
    console.rule(f"[{style}]{report.path.name} — {report.verdict.value.upper()}[/{style}]")
    console.print(f"[dim]{gloss}[/dim]\n")

    layer = report.text_layer
    console.print(
        f"[bold]Text layer[/bold]  {layer.page_count} pages, "
        f"{layer.total_chars:,} chars, median {layer.median_chars_per_page:,.0f}/page, "
        f"{layer.empty_page_ratio:.0%} near-empty"
    )

    if report.verdict is Verdict.NO_GO:
        for note in report.notes:
            console.print(f"\n[red]![/red] {note}")
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
        console.print("[bold]Furniture[/bold]  running header/footer detected:")
        for line in report.repeated_lines:
            console.print(f"             top={line.top:6.1f}  {line.page_ratio:.0%}  {line.text!r}")
    else:
        console.print("[bold]Furniture[/bold]  [yellow]none detected[/yellow]")

    cr = report.cross_refs
    misread_style = "green" if cr.misread_as_headings == 0 else "bold red"
    console.print(
        f"[bold]Xrefs[/bold]      {cr.inline_refs_seen} inline; "
        f"{cr.prose_lines_starting_with_id} wrapped prose lines start with an id; "
        f"[{misread_style}]{cr.misread_as_headings} misread as headings[/{misread_style}]"
    )

    if report.sequence:
        seq = report.sequence
        console.print(
            f"[bold]Sequence[/bold]   {len(seq.accepted_ids)} headings accepted, "
            f"{len(seq.violations)} violations, {len(seq.gaps)} gaps"
        )
        for violation in seq.violations[:10]:
            console.print(f"             [red]violation[/red] {violation}")
        for gap in seq.gaps[:10]:
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
            row_style = None if cand.accepted else "dim"
            table.add_row(
                cand.section_id,
                str(cand.line.page_no),
                f"{cand.score:.2f}",
                signals,
                detail,
                style=row_style,
            )
        console.print()
        console.print(table)

    for note in report.notes:
        console.print(f"\n[yellow]![/yellow] {note}")
