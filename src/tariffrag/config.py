"""Runtime configuration.

Values are overridable by environment variable with the ``TARIFFRAG_`` prefix,
e.g. ``TARIFFRAG_RRF_K=40``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "settings"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TARIFFRAG_", env_file=".env", extra="ignore")

    # --- Paths -------------------------------------------------------------
    repo_root: Path = _REPO_ROOT
    data_dir: Path = _REPO_ROOT / "data"
    corpus_dir: Path = _REPO_ROOT / "corpus"
    manifest_path: Path = _REPO_ROOT / "corpus" / "manifest.yaml"
    gold_dir: Path = _REPO_ROOT / "evals" / "gold"
    runs_dir: Path = _REPO_ROOT / "evals" / "runs"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @property
    def text_dir(self) -> Path:
        """Canonical extracted text; all char offsets are relative to these."""
        return self.data_dir / "text"

    @property
    def index_path(self) -> Path:
        """Single SQLite file holding metadata, FTS5, and vectors together."""
        return self.data_dir / "index.db"

    # --- Models ------------------------------------------------------------
    answer_model: str = "claude-opus-5"
    judge_model: str = "claude-opus-5"
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # --- Chunking ----------------------------------------------------------
    chunk_target_tokens: int = 700
    chunk_min_tokens: int = 400
    chunk_max_tokens: int = 1200

    # --- Retrieval ---------------------------------------------------------
    rrf_k: int = 60
    """Reciprocal Rank Fusion smoothing constant."""
    weight_lexical: float = 1.0
    weight_dense: float = 1.0
    candidates_per_retriever: int = 50
    final_k: int = 8
    use_reranker: bool = True

    # Absolute-score floors for abstention. Fused RRF ranks are always 1..k
    # regardless of match quality, so they can never signal "nothing relevant
    # exists" -- these thresholds are calibrated against the unanswerable gold
    # items and read off the *raw* retriever scores.
    min_bm25_score: float = 2.0
    min_cosine_score: float = 0.35

    # --- Cross-reference expansion (gated: tariffs cite everything) ---------
    xref_expand_from_top_n: int = 3
    xref_max_expanded: int = 3
    enable_xref_expansion: bool = True
    enable_glossary_expansion: bool = True

    # --- Fetching ----------------------------------------------------------
    user_agent: str = Field(
        default="tariffrag/0.1 (research project; contact via repository)",
        description="Identifying UA -- these are public filings, but be polite.",
    )
    fetch_delay_seconds: float = 2.0


settings = Settings()
