"""Tests for indexing, fusion and retrieval."""

from __future__ import annotations

import sqlite3

import pytest

from tariffrag.config import settings
from tariffrag.index import store
from tariffrag.index.dense import DenseHit, HashingEmbedder
from tariffrag.index.lexical import LexicalHit, build_match_query, search_lexical
from tariffrag.retrieve.fuse import reciprocal_rank_fusion
from tariffrag.retrieve.pipeline import retrieve
from tariffrag.retrieve.query import analyse_query

needs_index = pytest.mark.skipif(not settings.index_path.exists(), reason="index not built")


@pytest.fixture(scope="module")
def connection() -> sqlite3.Connection:
    return store.connect(settings.index_path)


# --- Lexical --------------------------------------------------------------


def test_match_query_keeps_section_numbers_whole() -> None:
    """The tokenizer keeps '.' and '-' inside tokens; the query must match that."""
    assert '"III.13.1.2"' in build_match_query("see III.13.1.2 please")
    assert '"Pay-for-Performance"' in build_match_query("Pay-for-Performance rules")


def test_match_query_is_empty_for_punctuation_only() -> None:
    assert build_match_query("...") == ""
    assert build_match_query("") == ""


@needs_index
def test_exact_section_number_is_retrievable(connection: sqlite3.Connection) -> None:
    """The case dense retrieval cannot serve: a section number has no semantics."""
    hits = search_lexical(connection, "III.13.3.4A", limit=5)
    assert hits
    rows = {
        connection.execute(
            "SELECT section_id FROM chunks WHERE chunk_id = ?", (h.chunk_id,)
        ).fetchone()["section_id"]
        for h in hits
    }
    assert "III.13.3.4A" in rows


@needs_index
def test_hyphenated_defined_term_survives_indexing(connection: sqlite3.Connection) -> None:
    hits = search_lexical(connection, "De-List Bid", limit=5)
    assert hits


# --- Fusion ---------------------------------------------------------------


def test_fusion_rewards_agreement_between_retrievers() -> None:
    result = reciprocal_rank_fusion(
        [LexicalHit("a", 9.0), LexicalHit("b", 8.0)],
        [DenseHit("b", 0.9), DenseHit("c", 0.8)],
    )
    assert result.hits[0].chunk_id == "b"
    assert result.hits[0].lexical_rank == 2
    assert result.hits[0].dense_rank == 1


def test_fusion_keeps_raw_scores_for_abstention() -> None:
    """Fused ranks are always 1..k, so they cannot signal "nothing matched"."""
    result = reciprocal_rank_fusion([LexicalHit("a", 0.4)], [DenseHit("a", 0.05)])
    assert result.top_lexical_score == 0.4
    assert result.top_dense_score == 0.05


def test_fusion_handles_one_empty_retriever() -> None:
    result = reciprocal_rank_fusion([LexicalHit("a", 1.0)], [])
    assert [h.chunk_id for h in result.hits] == ["a"]
    assert result.top_dense_score is None


def test_weights_shift_the_ranking() -> None:
    lexical = [LexicalHit("a", 9.0)]
    dense = [DenseHit("b", 0.9)]
    lexical_first = reciprocal_rank_fusion(lexical, dense, weight_lexical=5.0)
    dense_first = reciprocal_rank_fusion(lexical, dense, weight_dense=5.0)
    assert lexical_first.hits[0].chunk_id == "a"
    assert dense_first.hits[0].chunk_id == "b"


# --- Embedder -------------------------------------------------------------


def test_hashing_embedder_is_deterministic() -> None:
    """Uses blake2b, not hash(), which is salted per process."""
    a, b = HashingEmbedder(), HashingEmbedder()
    assert a.embed_query("Capacity Supply Obligation") == b.embed_query(
        "Capacity Supply Obligation"
    )


def test_hashing_embedder_returns_unit_vectors() -> None:
    vector = HashingEmbedder().embed_query("Forward Capacity Auction")
    assert abs(sum(x * x for x in vector) - 1.0) < 1e-6


# --- Query intent ---------------------------------------------------------


@needs_index
def test_named_section_is_detected_as_a_lookup(connection: sqlite3.Connection) -> None:
    intent = analyse_query(connection, "summarise III.13.3.4A for me")
    assert intent.is_lookup
    assert "III.13.3.4A" in intent.section_ids


@needs_index
def test_unknown_section_is_not_a_lookup(connection: sqlite3.Connection) -> None:
    assert not analyse_query(connection, "what about III.99.99.99").is_lookup


@needs_index
def test_temporal_cue_is_flagged(connection: sqlite3.Connection) -> None:
    """The corpus is one snapshot, so a question about 2019 cannot be answered."""
    assert analyse_query(connection, "what were the rules in 2019").has_temporal_cue
    assert not analyse_query(connection, "what are the rules now").has_temporal_cue


# --- End to end -----------------------------------------------------------


@needs_index
def test_named_section_is_pinned_to_the_top(connection: sqlite3.Connection) -> None:
    """A typed section id is a lookup, not a ranking problem.

    Without this, a chunk ranked second lexically but present in the dense list
    outranked the exact match, which is what happened when hybrid retrieval was
    first switched on.
    """
    result = retrieve(connection, "III.13.3.4A", embedder=HashingEmbedder(), limit=3)
    assert result.chunks
    assert result.chunks[0].section_id == "III.13.3.4A"


@needs_index
def test_retrieval_runs_without_an_embedder(connection: sqlite3.Connection) -> None:
    """Lexical-only must stay usable, so the repo runs with no model download."""
    result = retrieve(connection, "Capacity Supply Obligation", embedder=None, limit=5)
    assert result.chunks
    assert not result.dense_available
    assert result.top_dense_score is None


@needs_index
def test_every_retrieved_chunk_explains_its_provenance(
    connection: sqlite3.Connection,
) -> None:
    result = retrieve(connection, "forward capacity auction", embedder=HashingEmbedder())
    for chunk in result.chunks:
        assert chunk.breadcrumb
        assert chunk.pages.startswith("p")
        assert chunk.hit.explain()
