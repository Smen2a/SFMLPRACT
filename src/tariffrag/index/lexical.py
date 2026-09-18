"""Lexical retrieval over FTS5.

Lexical matching is not a checkbox here, it is half the system. Section numbers
carry almost no semantic signal -- ``III.13.1.1.2.2`` embeds near every other
section number -- so dense retrieval cannot find them and BM25 can. Defined
Terms are exact and load-bearing: "Capacity Supply Obligation" is not "capacity
obligation", and conflating near-synonyms is precisely what embeddings do well.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

__all__ = ["LexicalHit", "search_lexical"]

WEIGHT_TEXT = 1.0
WEIGHT_BREADCRUMB = 0.3
WEIGHT_SECTION_ID = 2.0
"""Breadcrumb is damped and an exact section-id match is boosted.

An unweighted breadcrumb makes every chunk under III.13 match "capacity".
"""

_SAFE = re.compile(r"[A-Za-z0-9.\-]+")


def build_match_query(query: str) -> str:
    """Turn free text into an FTS5 MATCH expression.

    Terms are OR-ed so a partial match still ranks, and each is quoted because
    ``III.13.1`` contains characters FTS5 would otherwise read as syntax.
    """
    terms = [t for t in _SAFE.findall(query) if t.strip(".-")]
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms)


@dataclass(frozen=True, slots=True)
class LexicalHit:
    chunk_id: str
    score: float
    """Positive and larger is better: FTS5's bm25() returns negatives."""


def search_lexical(connection: sqlite3.Connection, query: str, limit: int = 50) -> list[LexicalHit]:
    match = build_match_query(query)
    if not match:
        return []

    rows = connection.execute(
        """
        SELECT chunk_id, bm25(chunks_fts, ?, ?, ?) AS score
        FROM chunks_fts
        WHERE chunks_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (WEIGHT_TEXT, WEIGHT_BREADCRUMB, WEIGHT_SECTION_ID, match, limit),
    ).fetchall()
    return [LexicalHit(chunk_id=row["chunk_id"], score=-float(row["score"])) for row in rows]
