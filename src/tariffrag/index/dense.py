"""Dense retrieval: embeddings behind a protocol, vectors in the same database.

The embedder is a protocol with a local default so the repo runs with no API key.
Which model is best is an empirical question, and the ablation table answers it
rather than the README asserting it.

Vectors live in the same SQLite file as the chunks, via sqlite-vec, so a filtered
search (``WHERE iso = 'NYISO'``) is a join rather than a second store to keep in
sync. Brute-force KNN over this corpus is a few milliseconds; approximate search
would be complexity with no payoff, and saying so is the honest call.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "DenseHit",
    "Embedder",
    "HashingEmbedder",
    "LocalEmbedder",
    "create_vector_table",
    "search_dense",
    "upsert_vectors",
]


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into vectors.

    Two methods, because retrieval models are often asymmetric: BGE wants an
    instruction prefix on the query and none on the documents, and using the
    same call for both quietly costs recall.
    """

    name: str
    dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class LocalEmbedder:
    """Local sentence-transformers model. No API key, fully offline."""

    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = "BAAI/bge-base-en-v1.5", batch_size: int = 32) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.name = model_name
        self.batch_size = batch_size
        self.dimensions = int(self._model.get_sentence_embedding_dimension() or 768)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = self._model.encode(
            [f"{self.QUERY_PREFIX}{text}"], normalize_embeddings=True, show_progress_bar=False
        )[0]
        return [float(x) for x in vector]


class HashingEmbedder:
    """A deterministic, dependency-free embedder over hashed word features.

    Not a serious retrieval model -- it has no notion of synonymy, which is the
    main thing dense retrieval is for. It exists for two honest reasons: the
    pipeline stays runnable with no model download at all, and the ablation table
    gets a floor to measure real embedding models against. A dense row that
    cannot beat hashed bag-of-words is not earning its dependency.

    Hashing uses blake2b rather than ``hash()``, which is salted per process and
    would give different vectors on every run.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.name = f"hashing-{dimensions}"
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        import hashlib
        import math

        buckets = [0.0] * self.dimensions
        tokens = re.findall(r"[A-Za-z0-9.\-]+", text.lower())
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            # A signed contribution so unrelated tokens can cancel rather than
            # only ever accumulating.
            buckets[value % self.dimensions] += 1.0 if (value >> 8) & 1 else -1.0

        norm = math.sqrt(sum(x * x for x in buckets))
        return [x / norm for x in buckets] if norm else buckets

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _load_extension(connection: sqlite3.Connection) -> None:
    import sqlite_vec

    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)


def create_vector_table(connection: sqlite3.Connection, dimensions: int) -> None:
    _load_extension(connection)
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0("
        f"  chunk_id TEXT PRIMARY KEY, embedding FLOAT[{dimensions}])"
    )


def upsert_vectors(
    connection: sqlite3.Connection, chunk_ids: Sequence[str], vectors: Sequence[Sequence[float]]
) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
        [(chunk_id, _pack(vector)) for chunk_id, vector in zip(chunk_ids, vectors, strict=True)],
    )


@dataclass(frozen=True, slots=True)
class DenseHit:
    chunk_id: str
    score: float
    """Cosine similarity in [-1, 1]; larger is better."""


def search_dense(
    connection: sqlite3.Connection, query_vector: Sequence[float], limit: int = 50
) -> list[DenseHit]:
    _load_extension(connection)
    rows = connection.execute(
        """
        SELECT chunk_id, distance
        FROM chunk_vectors
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (_pack(query_vector), limit),
    ).fetchall()
    # Vectors are stored normalised, so L2 distance d relates to cosine
    # similarity as cos = 1 - d^2 / 2.
    return [
        DenseHit(chunk_id=row["chunk_id"], score=1.0 - (float(row["distance"]) ** 2) / 2.0)
        for row in rows
    ]
