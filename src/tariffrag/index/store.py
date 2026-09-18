"""One SQLite file holding metadata, full-text and vectors together.

Keeping them in a single database is the point: retrieval becomes one SQL join
instead of reconciling ids across a document store and a separate vector store,
and a filtered vector search (``WHERE iso = 'NYISO'``) stays expressible.

The lexical side is FTS5 with one configuration detail that matters more than
any reranker: the tokenizer keeps ``.`` and ``-`` inside tokens, so
``III.13.1.2`` and ``Pay-for-Performance`` survive as single terms. Without it
they shatter and the exact-match queries this corpus is full of stop working.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

from tariffrag.models import Chunk, Document, Section

__all__ = ["connect", "create_schema", "insert_chunks", "insert_documents", "insert_sections"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    iso         TEXT NOT NULL,
    doc_type    TEXT NOT NULL,
    title       TEXT NOT NULL,
    page_count  INTEGER NOT NULL,
    sha256_pdf  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    doc_id      TEXT NOT NULL,
    section_id  TEXT NOT NULL,
    heading     TEXT NOT NULL,
    breadcrumb  TEXT NOT NULL,
    depth       INTEGER NOT NULL,
    parent_id   TEXT,
    page_start  INTEGER NOT NULL,
    page_end    INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    PRIMARY KEY (doc_id, section_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    section_id  TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    breadcrumb  TEXT NOT NULL,
    text        TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    page_start  INTEGER NOT NULL,
    page_end    INTEGER NOT NULL,
    token_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_doc ON chunks (doc_id, section_id);

CREATE TABLE IF NOT EXISTS blocks (
    block_id    TEXT PRIMARY KEY,
    chunk_id    TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_by_chunk ON blocks (chunk_id, ordinal);

-- `tokenchars '.-'` keeps section numbers and hyphenated Defined Terms whole.
-- Breadcrumb is its own column so it can carry a lower bm25 weight: folded into
-- the body it would make every chunk under III.13 match "capacity" and saturate
-- the score.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    breadcrumb,
    section_id,
    chunk_id UNINDEXED,
    tokenize = "unicode61 tokenchars '.-'"
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def insert_documents(connection: sqlite3.Connection, documents: Iterable[Document]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                d.doc_id,
                d.iso.value,
                d.doc_type.value,
                d.title,
                d.page_count,
                d.source.sha256_pdf,
            )
            for d in documents
        ],
    )


def insert_sections(connection: sqlite3.Connection, sections: Iterable[Section]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                s.doc_id,
                s.section_id,
                s.heading,
                s.render_breadcrumb(),
                s.depth,
                s.parent_id,
                s.page_start,
                s.page_end,
                s.char_start,
                s.char_end,
            )
            for s in sections
        ],
    )


def insert_chunks(connection: sqlite3.Connection, chunks: Sequence[Chunk]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                c.chunk_id,
                c.doc_id,
                c.section_id,
                c.ordinal,
                c.breadcrumb,
                c.text,
                c.char_start,
                c.char_end,
                c.page_start,
                c.page_end,
                c.token_count,
            )
            for c in chunks
        ],
    )
    connection.executemany(
        "INSERT OR REPLACE INTO blocks VALUES (?, ?, ?, ?, ?, ?)",
        [
            (b.block_id, b.chunk_id, b.ordinal, b.text, b.char_start, b.char_end)
            for c in chunks
            for b in c.blocks
        ],
    )
    connection.executemany(
        "INSERT INTO chunks_fts (text, breadcrumb, section_id, chunk_id) VALUES (?, ?, ?, ?)",
        [(c.text, c.breadcrumb, c.section_id, c.chunk_id) for c in chunks],
    )
