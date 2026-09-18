"""The single retrieval entry point.

The CLI, the evaluation harness and the answering path all call this. Nothing
re-implements retrieval elsewhere: an eval that runs its own retrieval is
measuring a different system, which is the most common way a RAG evaluation
quietly lies about itself.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from tariffrag.index.dense import Embedder, search_dense
from tariffrag.index.lexical import search_lexical
from tariffrag.retrieve.fuse import FusedHit, reciprocal_rank_fusion
from tariffrag.retrieve.query import QueryIntent, analyse_query

__all__ = ["Retrieval", "RetrievedChunk", "retrieve"]


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    section_id: str
    breadcrumb: str
    text: str
    page_start: int
    page_end: int
    hit: FusedHit

    @property
    def pages(self) -> str:
        return (
            f"p{self.page_start}"
            if self.page_start == self.page_end
            else f"p{self.page_start}-{self.page_end}"
        )


@dataclass(frozen=True, slots=True)
class Retrieval:
    query: str
    chunks: list[RetrievedChunk]
    top_lexical_score: float | None
    top_dense_score: float | None
    dense_available: bool
    intent: QueryIntent | None = None

    def looks_unanswerable(self, *, min_bm25: float = 2.0, min_cosine: float = 0.35) -> bool:
        """Whether nothing in the corpus matched well enough to answer from.

        Judged on the *raw* retriever scores, never the fused ones: RRF ranks run
        1..k whatever the match quality, so a fused list looks identical for a
        question the corpus answers well and one it cannot answer at all.
        """
        lexical_ok = self.top_lexical_score is not None and self.top_lexical_score >= min_bm25
        dense_ok = self.top_dense_score is not None and self.top_dense_score >= min_cosine
        return not (lexical_ok or dense_ok)


def retrieve(
    connection: sqlite3.Connection,
    query: str,
    *,
    embedder: Embedder | None = None,
    candidates: int = 50,
    limit: int = 8,
    weight_lexical: float = 1.0,
    weight_dense: float = 1.0,
) -> Retrieval:
    """Run both retrievers and fuse them.

    Falls back to lexical alone when no embedder is supplied, so the system stays
    usable without the optional model dependency -- degraded, and honest about it
    via ``dense_available`` rather than silently worse.
    """
    intent = analyse_query(connection, query)
    lexical = search_lexical(connection, query, limit=candidates)

    dense = []
    if embedder is not None:
        dense = search_dense(connection, embedder.embed_query(query), limit=candidates)

    fused = reciprocal_rank_fusion(
        lexical,
        dense,
        weight_lexical=weight_lexical,
        weight_dense=weight_dense,
        limit=limit,
    )

    ordered_ids = [hit.chunk_id for hit in fused.hits]
    pinned = _chunks_for_sections(connection, intent.section_ids)

    # A named section is a lookup, not a ranking problem: put it first and say so.
    hit_by_id = {hit.chunk_id: hit for hit in fused.hits}
    ordered_ids = pinned + [cid for cid in ordered_ids if cid not in pinned]

    chunks: list[RetrievedChunk] = []
    for chunk_id in ordered_ids[:limit]:
        hit = hit_by_id.get(chunk_id) or FusedHit(chunk_id=chunk_id, score=0.0)
        row = connection.execute(
            "SELECT doc_id, section_id, breadcrumb, text, page_start, page_end "
            "FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            continue
        chunks.append(
            RetrievedChunk(
                chunk_id=chunk_id,
                doc_id=row["doc_id"],
                section_id=row["section_id"],
                breadcrumb=row["breadcrumb"],
                text=row["text"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                hit=hit,
            )
        )

    return Retrieval(
        query=query,
        chunks=chunks,
        top_lexical_score=fused.top_lexical_score,
        top_dense_score=fused.top_dense_score,
        dense_available=embedder is not None,
        intent=intent,
    )


def _chunks_for_sections(connection: sqlite3.Connection, section_ids: Sequence[str]) -> list[str]:
    """Chunks belonging to sections the query named explicitly."""
    pinned: list[str] = []
    for section_id in section_ids:
        rows = connection.execute(
            "SELECT chunk_id FROM chunks WHERE section_id = ? ORDER BY ordinal",
            (section_id,),
        ).fetchall()
        pinned.extend(row["chunk_id"] for row in rows)
    return pinned
