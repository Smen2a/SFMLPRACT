"""Reading what a query is actually asking for before retrieving.

Some queries are not semantic at all. ``III.13.3.4A`` is a lookup: the user knows
the section and wants it. Leaving that to fusion is unreliable -- a chunk ranked
second lexically but present in the dense list can outrank an exact match that
the dense retriever missed entirely, which is exactly what happened when hybrid
retrieval was first switched on here.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from tariffrag.ingest.structure import parse_section_id

__all__ = ["QueryIntent", "analyse_query"]

_CANDIDATE = re.compile(r"\b(?:[IVXLCDM]+(?:\.(?:\d+[A-Z]?|[A-Z]))+|\d+(?:\.\d+)+)\b")

_TEMPORAL = re.compile(
    r"\b(?:in\s+(?:19|20)\d{2}|as\s+of\s+(?:19|20)\d{2}|used\s+to|previously|"
    r"prior\s+to|formerly|back\s+then|at\s+the\s+time)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class QueryIntent:
    text: str
    section_ids: tuple[str, ...]
    """Section ids named in the query that actually exist in the index."""
    has_temporal_cue: bool
    """The query asks about a past state the corpus snapshot cannot speak to."""

    @property
    def is_lookup(self) -> bool:
        return bool(self.section_ids)


def analyse_query(connection: sqlite3.Connection, query: str) -> QueryIntent:
    found: list[str] = []
    for match in _CANDIDATE.finditer(query):
        parsed = parse_section_id(match.group(0))
        if parsed is None:
            continue
        row = connection.execute(
            "SELECT 1 FROM chunks WHERE section_id = ? LIMIT 1", (parsed.display,)
        ).fetchone()
        if row and parsed.display not in found:
            found.append(parsed.display)

    return QueryIntent(
        text=query,
        section_ids=tuple(found),
        has_temporal_cue=bool(_TEMPORAL.search(query)),
    )
