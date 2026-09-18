"""Tests for paragraph grouping and chunking."""

from __future__ import annotations

from itertools import pairwise

import pytest

from tariffrag.config import settings
from tariffrag.ingest.chunk import build_chunks, estimate_tokens, group_paragraphs
from tariffrag.ingest.extract import load_canonical
from tariffrag.ingest.pipeline import load_sections

TEXT_DIR = settings.text_dir
needs_ingest = pytest.mark.skipif(
    not (TEXT_DIR.exists() and any(TEXT_DIR.glob("*.ingest.json"))),
    reason="corpus not ingested",
)

DOC = "isone:mr1:sec_13_14"


@pytest.fixture(scope="module")
def chunked():  # type: ignore[no-untyped-def]
    canonical = load_canonical(DOC, TEXT_DIR)
    sections = load_sections(DOC, TEXT_DIR)
    return canonical, build_chunks(sections, canonical, group_paragraphs(canonical))


@needs_ingest
def test_chunks_respect_the_ceiling(chunked) -> None:  # type: ignore[no-untyped-def]
    _, chunks = chunked
    assert chunks
    assert max(c.token_count for c in chunks) <= 1200


@needs_ingest
def test_chunk_text_matches_its_offsets(chunked) -> None:  # type: ignore[no-untyped-def]
    """A chunk's stored text must be exactly what its span says it is."""
    canonical, chunks = chunked
    for chunk in chunks:
        assert chunk.text == canonical.slice(chunk.char_start, chunk.char_end)


@needs_ingest
def test_blocks_tile_their_chunk_in_order(chunked) -> None:  # type: ignore[no-untyped-def]
    _, chunks = chunked
    for chunk in chunks:
        assert chunk.blocks, "every chunk needs at least one citation target"
        assert [b.ordinal for b in chunk.blocks] == list(range(len(chunk.blocks)))
        for block in chunk.blocks:
            assert chunk.char_start <= block.char_start <= chunk.char_end


@needs_ingest
def test_chunks_do_not_overlap(chunked) -> None:  # type: ignore[no-untyped-def]
    """Overlap duplicates text in the lexical index and double-counts in recall."""
    _, chunks = chunked
    spans = sorted((c.char_start, c.char_end) for c in chunks)
    for (_, end), (next_start, _) in pairwise(spans):
        assert next_start >= end


@needs_ingest
def test_split_chunks_link_to_their_neighbours(chunked) -> None:  # type: ignore[no-untyped-def]
    _, chunks = chunked
    by_id = {c.chunk_id: c for c in chunks}
    split = [c for c in chunks if c.continues_in]
    assert split, "an oversized section should split"
    for chunk in split:
        assert chunk.continues_in is not None
        assert by_id[chunk.continues_in].continued_from == chunk.chunk_id


@needs_ingest
def test_source_uri_is_stable_and_carries_no_url(chunked) -> None:  # type: ignore[no-untyped-def]
    _, chunks = chunked
    for chunk in chunks[:50]:
        assert "http" not in chunk.source_uri()
        assert chunk.doc_id in chunk.source_uri()


def test_token_estimate_is_monotonic() -> None:
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 100)
    assert estimate_tokens("") >= 1
