"""Combining lexical and dense results with Reciprocal Rank Fusion.

RRF uses ranks rather than scores because the two scales are not comparable:
BM25 is unbounded and query-length dependent, while cosine on normalised vectors
sits in a tight, query-dependent band. Per-query min-max normalisation would
force the top hit to 1.0 in both, destroying exactly the signal that says
*whether anything matched at all*.

That last point is why the raw scores are carried through rather than discarded.
Fused ranks are always 1..k regardless of match quality, so RRF alone can never
say "nothing relevant exists" -- which is precisely what abstention needs. The
raw top BM25 and max cosine are kept on the result for thresholds calibrated
against the unanswerable items in the gold set.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from tariffrag.index.dense import DenseHit
from tariffrag.index.lexical import LexicalHit

__all__ = ["FusedHit", "FusionResult", "reciprocal_rank_fusion"]

RRF_K = 60
"""Smoothing constant. Standard value; damps the influence of a single top rank."""


@dataclass(frozen=True, slots=True)
class FusedHit:
    chunk_id: str
    score: float
    lexical_rank: int | None = None
    dense_rank: int | None = None
    lexical_score: float | None = None
    dense_score: float | None = None

    def explain(self) -> str:
        parts = []
        if self.lexical_rank is not None:
            parts.append(f"bm25 #{self.lexical_rank} ({self.lexical_score:.2f})")
        if self.dense_rank is not None:
            parts.append(f"dense #{self.dense_rank} ({self.dense_score:.3f})")
        return ", ".join(parts) or "no match"


@dataclass(frozen=True, slots=True)
class FusionResult:
    hits: list[FusedHit] = field(default_factory=list)
    top_lexical_score: float | None = None
    top_dense_score: float | None = None
    """Raw, unfused bests -- the only signal that can say nothing matched."""


def reciprocal_rank_fusion(
    lexical: Sequence[LexicalHit],
    dense: Sequence[DenseHit],
    *,
    weight_lexical: float = 1.0,
    weight_dense: float = 1.0,
    k: int = RRF_K,
    limit: int = 20,
) -> FusionResult:
    scores: dict[str, float] = {}
    lexical_rank: dict[str, int] = {}
    dense_rank: dict[str, int] = {}
    lexical_score: dict[str, float] = {}
    dense_score: dict[str, float] = {}

    for rank, hit in enumerate(lexical, start=1):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight_lexical / (k + rank)
        lexical_rank[hit.chunk_id] = rank
        lexical_score[hit.chunk_id] = hit.score

    for rank, dense_hit in enumerate(dense, start=1):
        scores[dense_hit.chunk_id] = scores.get(dense_hit.chunk_id, 0.0) + weight_dense / (k + rank)
        dense_rank[dense_hit.chunk_id] = rank
        dense_score[dense_hit.chunk_id] = dense_hit.score

    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    hits = [
        FusedHit(
            chunk_id=chunk_id,
            score=score,
            lexical_rank=lexical_rank.get(chunk_id),
            dense_rank=dense_rank.get(chunk_id),
            lexical_score=lexical_score.get(chunk_id),
            dense_score=dense_score.get(chunk_id),
        )
        for chunk_id, score in ordered
    ]
    return FusionResult(
        hits=hits,
        top_lexical_score=lexical[0].score if lexical else None,
        top_dense_score=dense[0].score if dense else None,
    )
