"""Canonical text: the coordinate system every citation resolves against.

One text file per document, with page furniture removed and normalisation
applied exactly once. Character offsets are assigned *after* that, so a span
recorded at ingest still points at the same words later. Nothing is ever
normalised at query time -- that would shift offsets underneath stored spans.

Alongside the text, a layout file records where each page and each source line
landed, so an offset resolves back to a page (and Phase 2 can regroup lines into
paragraphs using the original geometry).
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from tariffrag.ingest.structure import (
    DocumentStructure,
    HeaderCandidate,
    Line,
    _heading_shape,
)
from tariffrag.models import Section

__all__ = [
    "CanonicalText",
    "LineSpan",
    "PageSpan",
    "build_canonical",
    "build_sections",
    "load_canonical",
    "normalize",
    "save_canonical",
]

LAYOUT_SUFFIX = ".layout.json"

_QUOTES = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
}
"""Curly quotes flattened so lexical search matches what a user types.

Measured on Market Rule 1: 964 right single quotes and 57 double-quote pairs.
En and em dashes are deliberately *not* touched -- they carry meaning in page
ranges and in the effective-date stamps.
"""


def normalize(text: str) -> str:
    """Normalise one line of extracted text.

    Deliberately small, because each step was checked against the corpus rather
    than copied from a checklist. Ligatures, soft hyphens and non-breaking
    spaces do not occur in Market Rule 1 at all, so no step handles them.

    **Line-broken words are not re-joined.** The usual rule -- join when a line
    ends in a hyphen -- is destructive here: across the 147 end-of-line hyphens
    in the two largest documents, the hyphenated form is more common elsewhere
    141 times and the joined form 0 times. ``De-List`` appears 490 times against
    1 for ``DeList``; ``Real-Time`` 374 against 0. These are Defined Terms, and
    de-hyphenating them would break exact-match retrieval on the terms that
    carry the most weight.
    """
    for curly, plain in _QUOTES.items():
        text = text.replace(curly, plain)
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class PageSpan:
    page: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class LineSpan:
    page: int
    top: float
    x0: float
    start: int
    end: int
    source_index: int
    """Index into ``DocumentStructure.lines``.

    Canonical text drops furniture and contents pages, so positions no longer
    line up with the source. This is what lets a detected heading be located in
    the canonical text it will be cited from.
    """


@dataclass(frozen=True, slots=True)
class CanonicalText:
    doc_id: str
    text: str
    pages: tuple[PageSpan, ...]
    lines: tuple[LineSpan, ...]
    excluded_toc_pages: tuple[int, ...]
    sha256: str

    def page_for_offset(self, offset: int) -> int | None:
        """Resolve a character offset back to its source page.

        The newline joining the last line of one page to the first line of the
        next belongs to no page's span, and section boundaries land on exactly
        those characters. So an offset inside a gap resolves to the page that
        precedes it rather than to nothing -- otherwise a section ending at a
        page break reports the wrong page.
        """
        if not self.pages or offset < self.pages[0].start:
            return None
        starts = [p.start for p in self.pages]
        index = bisect_right(starts, offset) - 1
        return self.pages[index].page

    def slice(self, start: int, end: int) -> str:
        return self.text[start:end]

    def source_index_map(self) -> dict[int, LineSpan]:
        """Source line index -> canonical span, for lines that survived.

        Built once by the caller rather than per lookup: a per-call rebuild is
        O(lines) and the tree builder queries it once per heading.
        """
        return {ln.source_index: ln for ln in self.lines}


def _keep(line: Line, furniture_tops: set[float], toc_pages: set[int], tolerance: float) -> bool:
    if line.page_no in toc_pages:
        return False
    return not any(abs(line.top - top) <= tolerance for top in furniture_tops)


def build_canonical(
    doc_id: str, structure: DocumentStructure, *, band_tolerance: float = 3.0
) -> CanonicalText:
    """Assemble canonical text from a parsed document.

    Two kinds of page content are dropped. Running headers and footers are
    furniture that would otherwise land in the middle of every chunk and shift
    every offset. Table-of-contents pages are real content but poor retrieval
    material -- Sections 1-12 open with a genuine 33-page contents listing whose
    entries duplicate every heading in the document -- so they are excluded and
    recorded, rather than silently indexed.
    """
    furniture_tops = structure.furniture_tops
    toc_pages = set(structure.toc_pages)

    parts: list[str] = []
    lines: list[LineSpan] = []
    page_bounds: dict[int, tuple[int, int]] = {}
    cursor = 0

    for source_index, line in enumerate(structure.lines):
        if not _keep(line, furniture_tops, toc_pages, band_tolerance):
            continue
        text = normalize(line.text)
        if not text:
            continue

        start = cursor
        end = start + len(text)
        parts.append(text)
        lines.append(
            LineSpan(
                page=line.page_no,
                top=line.top,
                x0=line.x0,
                start=start,
                end=end,
                source_index=source_index,
            )
        )

        first, _ = page_bounds.get(line.page_no, (start, end))
        page_bounds[line.page_no] = (first, end)
        cursor = end + 1  # the newline joining this line to the next

    text = "\n".join(parts)
    pages = tuple(
        PageSpan(page=page, start=bounds[0], end=bounds[1])
        for page, bounds in sorted(page_bounds.items())
    )

    return CanonicalText(
        doc_id=doc_id,
        text=text,
        pages=pages,
        lines=tuple(lines),
        excluded_toc_pages=tuple(sorted(toc_pages)),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def save_canonical(canonical: CanonicalText, text_dir: Path) -> tuple[Path, Path]:
    text_dir.mkdir(parents=True, exist_ok=True)
    text_path = text_dir / f"{canonical.doc_id}.txt"
    layout_path = text_dir / f"{canonical.doc_id}{LAYOUT_SUFFIX}"

    text_path.write_text(canonical.text, encoding="utf-8")
    layout_path.write_text(
        json.dumps(
            {
                "doc_id": canonical.doc_id,
                "sha256": canonical.sha256,
                "excluded_toc_pages": list(canonical.excluded_toc_pages),
                "pages": [[p.page, p.start, p.end] for p in canonical.pages],
                "lines": [
                    [ln.page, ln.top, ln.x0, ln.start, ln.end, ln.source_index]
                    for ln in canonical.lines
                ],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return text_path, layout_path


def load_canonical(doc_id: str, text_dir: Path) -> CanonicalText:
    text = (text_dir / f"{doc_id}.txt").read_text(encoding="utf-8")
    layout = json.loads((text_dir / f"{doc_id}{LAYOUT_SUFFIX}").read_text(encoding="utf-8"))

    stored = str(layout["sha256"])
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if stored != actual:
        raise ValueError(
            f"{doc_id}: canonical text does not match its recorded hash "
            f"({actual[:12]} != {stored[:12]}). Every stored offset is relative to the "
            "recorded text, so re-run `tariffrag ingest` rather than trusting these spans."
        )

    return CanonicalText(
        doc_id=doc_id,
        text=text,
        pages=tuple(PageSpan(p, s, e) for p, s, e in layout["pages"]),
        lines=tuple(LineSpan(p, t, x, s, e, i) for p, t, x, s, e, i in layout["lines"]),
        excluded_toc_pages=tuple(layout["excluded_toc_pages"]),
        sha256=stored,
    )


# --- Section tree ---------------------------------------------------------
#
# Lives here rather than in structure.py because a section's span is expressed
# in canonical-text coordinates, and structure.py must not depend on this module.


def build_sections(
    doc_id: str, structure: DocumentStructure, canonical: CanonicalText
) -> tuple[Section, ...]:
    """Turn accepted headings into positioned sections.

    A section runs from its own heading to the start of the next heading,
    whatever that one's depth: nested subsections are separate sections, so a
    parent's span holds only its own prose. Text before the first heading --
    cover pages, preambles -- is attached to a synthetic root rather than
    dropped, so "share of text assigned to a section" stays an honest number.
    """
    spans = canonical.source_index_map()
    positions = {id(line): index for index, line in enumerate(structure.lines)}

    anchored: list[tuple[HeaderCandidate, LineSpan]] = []
    for candidate in structure.accepted:
        source_index = positions.get(id(candidate.line))
        if source_index is None:
            continue
        span = spans.get(source_index)
        if span is not None:
            anchored.append((candidate, span))

    anchored.sort(key=lambda pair: pair[1].start)
    sections: list[Section] = []

    if not anchored:
        if not canonical.text:
            return ()
        return (_root_section(doc_id, canonical, 0, len(canonical.text)),)

    first_start = anchored[0][1].start
    if first_start > 0:
        sections.append(_root_section(doc_id, canonical, 0, first_start))

    for position, (candidate, span) in enumerate(anchored):
        end = (
            anchored[position + 1][1].start if position + 1 < len(anchored) else len(canonical.text)
        )
        depth = len(candidate.sort_key)

        # The nearest preceding section that is shallower than this one.
        parent: Section | None = None
        for earlier in reversed(sections):
            if earlier.depth < depth:
                parent = earlier
                break

        breadcrumb: tuple[tuple[str, str], ...] = ()
        if parent is not None and parent.section_id:
            breadcrumb = (*parent.breadcrumb, (parent.section_id, parent.heading))

        remainder, _ = _heading_shape(candidate.line.text, candidate.section_id)
        sections.append(
            Section(
                doc_id=doc_id,
                section_id=candidate.section_id,
                heading=remainder or candidate.section_id,
                breadcrumb=breadcrumb,
                depth=depth,
                parent_id=parent.section_id if parent else None,
                page_start=span.page,
                page_end=canonical.page_for_offset(max(span.start, end - 1)) or span.page,
                char_start=span.start,
                char_end=end,
            )
        )
    return tuple(sections)


def _root_section(doc_id: str, canonical: CanonicalText, start: int, end: int) -> Section:
    """Holds front matter so it is accounted for rather than silently lost."""
    return Section(
        doc_id=doc_id,
        section_id="",
        heading="(front matter)",
        breadcrumb=(),
        depth=0,
        parent_id=None,
        page_start=canonical.page_for_offset(start) or 1,
        page_end=canonical.page_for_offset(max(start, end - 1)) or 1,
        char_start=start,
        char_end=end,
    )
