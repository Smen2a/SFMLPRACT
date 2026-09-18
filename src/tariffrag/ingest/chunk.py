"""Sections into chunks, and chunks into citable blocks.

Three units stay distinct, and the distinction is the point:

*Section* is the parse unit -- the document's own numbered hierarchy.
*Chunk* is the retrieval unit, sized for embedding.
*CitationBlock* is the citation unit, one paragraph or enumerated item.

Block size is the citation-precision knob. Claude cites whole blocks, so one
oversized block yields a citation meaning "somewhere in these 1200 tokens",
which is useless for verifying a claim about a tariff.

There is deliberately **no chunk overlap**. Overlap duplicates text in the
lexical index, double-counts in recall metrics and inflates the corpus; a
neighbour can be pulled in at retrieval time instead, which is cheaper and
explainable.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from tariffrag.ingest.extract import CanonicalText, LineSpan
from tariffrag.models import Chunk, CitationBlock, Section

__all__ = ["Paragraph", "build_chunks", "estimate_tokens", "group_paragraphs"]

CHARS_PER_TOKEN = 4
"""Rough estimator used only for chunk sizing.

Not a token count anyone should quote: it exists to decide where to split, and
being a few percent out moves a boundary rather than breaking anything. Real
counts come from the API when they matter.
"""

PARAGRAPH_GAP_RATIO = 1.4
"""Vertical gap, relative to the page's usual line spacing, that starts a paragraph."""

INDENT_TOLERANCE = 8.0
"""Points of extra left inset, against the previous line, that start a paragraph.

Relative to the previous line rather than to the page margin. Measured on
Sections 13-14: body text sits at x0=72 and indented blocks at 108, but the
continuation lines *inside* an indented block share that 108. Comparing
against the margin made every one of those 850 lines its own paragraph.
"""

MAX_BLOCK_WORDS = 150
MIN_BLOCK_WORDS = 8

_ENUMERATED = re.compile(r"^\((?:[a-z]{1,2}|[ivxlcdm]{1,5}|\d{1,2})\)")
"""ISO-NE enumerates with parenthesised markers: ``(a)``, ``(iii)``, ``(12)``."""

_SENTENCE_END = re.compile(r"(?<=[.;:])\s+")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class Paragraph:
    char_start: int
    char_end: int
    page_start: int
    page_end: int

    def text(self, canonical: CanonicalText) -> str:
        return canonical.slice(self.char_start, self.char_end)


def _typical_gap(lines: Sequence[LineSpan]) -> float:
    gaps = [
        round(b.top - a.top, 1)
        for a, b in pairwise(lines)
        if b.page == a.page and 0 < b.top - a.top < 60
    ]
    return statistics.median(gaps) if gaps else 16.0


def group_paragraphs(canonical: CanonicalText) -> list[Paragraph]:
    """Group canonical lines into paragraphs using the original page geometry.

    Canonical text is one line per visual line, which is faithful but not citable
    prose. Paragraph starts are recovered from the layout that produced it: a
    larger-than-usual vertical gap, a left indent, an enumerated marker, or a
    page break.
    """
    lines = canonical.lines
    if not lines:
        return []

    gap = _typical_gap(lines) * PARAGRAPH_GAP_RATIO

    starts: list[int] = [0]
    for index, (previous, current) in enumerate(pairwise(lines), start=1):
        text = canonical.slice(current.start, current.end)
        new_page = current.page != previous.page
        big_gap = not new_page and (current.top - previous.top) > gap
        indent_step = not new_page and current.x0 > previous.x0 + INDENT_TOLERANCE
        if new_page or big_gap or indent_step or _ENUMERATED.match(text):
            starts.append(index)

    paragraphs: list[Paragraph] = []
    for position, start_index in enumerate(starts):
        end_index = starts[position + 1] if position + 1 < len(starts) else len(lines)
        span = lines[start_index:end_index]
        paragraphs.append(
            Paragraph(
                char_start=span[0].start,
                char_end=span[-1].end,
                page_start=span[0].page,
                page_end=span[-1].page,
            )
        )
    return paragraphs


def _split_long_block(text: str, start: int) -> list[tuple[str, int, int]]:
    """Split an oversized paragraph at sentence boundaries, never mid-sentence."""
    if len(text.split()) <= MAX_BLOCK_WORDS:
        return [(text, start, start + len(text))]

    pieces: list[tuple[str, int, int]] = []
    cursor = 0
    buffer: list[str] = []
    buffer_start = 0
    for sentence in _SENTENCE_END.split(text):
        if not buffer:
            buffer_start = text.find(sentence, cursor)
        buffer.append(sentence)
        cursor = text.find(sentence, cursor) + len(sentence)
        if sum(len(s.split()) for s in buffer) >= MAX_BLOCK_WORDS:
            joined = " ".join(buffer)
            pieces.append((joined, start + buffer_start, start + buffer_start + len(joined)))
            buffer = []
    if buffer:
        joined = " ".join(buffer)
        pieces.append((joined, start + buffer_start, start + buffer_start + len(joined)))
    return pieces


def _blocks_for(
    chunk_id: str, canonical: CanonicalText, paragraphs: Sequence[Paragraph]
) -> list[CitationBlock]:
    blocks: list[CitationBlock] = []
    for paragraph in paragraphs:
        text = paragraph.text(canonical)
        for piece, start, end in _split_long_block(text, paragraph.char_start):
            if len(piece.split()) < MIN_BLOCK_WORDS and blocks:
                # Fold a stub onto the previous block rather than emitting a
                # citation target too small to support a claim.
                previous = blocks[-1]
                blocks[-1] = CitationBlock(
                    block_id=previous.block_id,
                    chunk_id=chunk_id,
                    ordinal=previous.ordinal,
                    text=f"{previous.text} {piece}",
                    char_start=previous.char_start,
                    char_end=end,
                )
                continue
            blocks.append(
                CitationBlock(
                    block_id=f"{chunk_id}:b{len(blocks)}",
                    chunk_id=chunk_id,
                    ordinal=len(blocks),
                    text=piece,
                    char_start=start,
                    char_end=end,
                )
            )
    return blocks


def _paragraphs_in(paragraphs: Sequence[Paragraph], start: int, end: int) -> list[Paragraph]:
    return [p for p in paragraphs if p.char_start >= start and p.char_end <= end]


def _emit(
    section: Section,
    canonical: CanonicalText,
    group: Sequence[Paragraph],
    ordinal: int,
) -> Chunk:
    char_start = group[0].char_start
    char_end = group[-1].char_end
    text = canonical.slice(char_start, char_end)
    chunk_id = f"{section.doc_id}:{section.section_id or 'root'}:c{ordinal}"

    chunk = Chunk(
        chunk_id=chunk_id,
        doc_id=section.doc_id,
        section_id=section.section_id,
        ordinal=ordinal,
        breadcrumb=section.render_breadcrumb(),
        text=text,
        char_start=char_start,
        char_end=char_end,
        page_start=group[0].page_start,
        page_end=group[-1].page_end,
        token_count=estimate_tokens(text),
    )
    chunk.blocks = _blocks_for(chunk_id, canonical, group)
    return chunk


def _group_sections(
    sections: Sequence[Section], canonical: CanonicalText, min_tokens: int
) -> list[list[Section]]:
    """Attach sections too small to stand alone to the one that follows them.

    A parent whose span holds only its heading -- because a subsection starts on
    the next line -- is common in a deep hierarchy and makes a useless chunk on
    its own. Leading the chunk that holds its first child's prose is better for
    retrieval too: the parent heading rides along as context.

    Merging never crosses a parent boundary. A chunk spanning two parents has a
    breadcrumb that is a lie about half its content.
    """
    groups: list[list[Section]] = []
    carried: list[Section] = []

    for section in sections:
        size = estimate_tokens(canonical.slice(section.char_start, section.char_end))
        if carried:
            leader = carried[-1]
            related = section.parent_id == leader.section_id or (
                section.parent_id == leader.parent_id and leader.parent_id is not None
            )
            if not related:
                groups.append(carried)
                carried = []

        if size < min_tokens:
            carried.append(section)
            if (
                sum(estimate_tokens(canonical.slice(s.char_start, s.char_end)) for s in carried)
                >= min_tokens
            ):
                groups.append(carried)
                carried = []
            continue

        groups.append([*carried, section])
        carried = []

    if carried:
        if groups:
            groups[-1].extend(carried)
        else:
            groups.append(carried)
    return groups


def build_chunks(
    sections: Sequence[Section],
    canonical: CanonicalText,
    paragraphs: Sequence[Paragraph],
    *,
    target_tokens: int = 700,
    max_tokens: int = 1200,
    min_tokens: int = 400,
) -> list[Chunk]:
    """Pack sections into retrieval-sized chunks.

    A group that fits becomes one chunk. An oversized one splits at paragraph
    boundaries -- never mid-sentence -- with ``continued_from`` /
    ``continues_in`` linking the pieces.
    """
    chunks: list[Chunk] = []

    for group in _group_sections(sections, canonical, min_tokens):
        own: list[Paragraph] = []
        for member in group:
            own.extend(_paragraphs_in(paragraphs, member.char_start, member.char_end))
        if not own:
            continue

        leader = group[0]
        tokens = estimate_tokens(canonical.slice(own[0].char_start, own[-1].char_end))
        if tokens <= max_tokens:
            chunks.append(_emit(leader, canonical, own, 0))
            continue

        pieces: list[list[Paragraph]] = []
        buffer: list[Paragraph] = []
        running = 0
        for paragraph in own:
            size = estimate_tokens(paragraph.text(canonical))
            if buffer and running + size > target_tokens:
                pieces.append(buffer)
                buffer, running = [], 0
            buffer.append(paragraph)
            running += size
        if buffer:
            pieces.append(buffer)

        emitted = [_emit(leader, canonical, piece, index) for index, piece in enumerate(pieces)]
        for index, chunk in enumerate(emitted):
            if index:
                chunk.continued_from = emitted[index - 1].chunk_id
            if index + 1 < len(emitted):
                chunk.continues_in = emitted[index + 1].chunk_id
        chunks.extend(emitted)

    return chunks
