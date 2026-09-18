"""The corpus manifest: what each source document is, and what it hashed to.

``corpus/manifest.yaml`` is the reproducibility contract. It records, per
document, an identity, a title and where that title came from, the pages each
effective-date stamp governs, a content hash, and whether the document takes
part in the corpus at all.

Two properties are deliberate:

*Rebuilds are byte-stable.* ``retrieved_at`` is carried over for any document
whose content hash is unchanged, so rebuilding does not churn the file. A
manifest that produced a diff on every build would stop being a useful record of
change.

*Nothing is invented.* Documents supplied directly carry no URL, and three
appendices carry no effective-date stamp at all. Both are recorded as absent
rather than guessed at, because everything here ends up underneath a citation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pdfplumber
import yaml

from tariffrag.ingest.spike import Verdict, spike_document
from tariffrag.models import (
    ISO,
    DocStatus,
    DocType,
    Document,
    EffectiveDate,
    Origin,
    RevisionSource,
    SourceRef,
    TitleSource,
    format_page_ranges,
    parse_page_ranges,
)

__all__ = ["Manifest", "ManifestDiff", "build_manifest", "diff_manifest", "load", "save"]

MANIFEST_VERSION = 1

_VERDICT_STATUS = {
    Verdict.GO: DocStatus.ACTIVE,
    Verdict.DEGRADED: DocStatus.ACTIVE,
    Verdict.EMPTY: DocStatus.RESERVED,
    Verdict.NO_GO: DocStatus.EXCLUDED,
}

_COLLECTION_DOC_TYPE = {"mr1": DocType.TARIFF}
"""Tariff collections by directory name; anything else is treated as a manual."""


@dataclass(frozen=True, slots=True)
class Manifest:
    documents: tuple[Document, ...]
    version: int = MANIFEST_VERSION

    def by_id(self, doc_id: str) -> Document | None:
        return next((d for d in self.documents if d.doc_id == doc_id), None)

    @property
    def ingestable(self) -> tuple[Document, ...]:
        """Documents Phase 1 should extract; reserved and excluded ones are skipped."""
        return tuple(d for d in self.documents if d.is_ingestable)


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (self.added or self.removed or self.changed)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _identify(pdf_path: Path, repo_root: Path) -> tuple[str, ISO, DocType, str]:
    """Derive ``(doc_id, iso, doc_type, relative_path)`` from the corpus layout.

    ``corpus/isone/mr1/mr1_append_a.pdf`` -> ``isone:mr1:append_a``.
    """
    relative = pdf_path.relative_to(repo_root).as_posix()
    parts = pdf_path.relative_to(repo_root / "corpus").parts
    iso_name, collection = parts[0], parts[1] if len(parts) > 2 else "misc"

    stem = pdf_path.stem
    prefix = f"{collection}_"
    short = stem[len(prefix) :] if stem.startswith(prefix) else stem

    iso = ISO(iso_name.upper())
    doc_type = _COLLECTION_DOC_TYPE.get(collection, DocType.MANUAL)
    return f"{iso_name}:{collection}:{short}", iso, doc_type, relative


def _probe(pdf_path: Path, repo_root: Path, retrieved_at: datetime, sha: str) -> Document:
    """Build one manifest entry by running the spike over the document."""
    report = spike_document(pdf_path)
    doc_id, iso, doc_type, relative = _identify(pdf_path, repo_root)

    return Document(
        doc_id=doc_id,
        iso=iso,
        doc_type=doc_type,
        title=report.title or pdf_path.stem,
        title_source=report.title_source,
        source=SourceRef(
            origin=Origin.SUPPLIED,
            path=relative,
            retrieved_at=retrieved_at,
            sha256_pdf=sha,
        ),
        page_count=report.text_layer.page_count,
        status=_VERDICT_STATUS[report.verdict],
        extractor="pdfplumber",
        extractor_version=str(pdfplumber.__version__),
        effective_dates=report.effective_dates,
        # ISO-NE prints the effective date and docket in a per-page stamp rather
        # than a revision number, so the stamp is the provenance. Documents
        # without one stay UNKNOWN rather than being assigned a guess.
        revision_source=(
            RevisionSource.COVER_PAGE if report.effective_dates else RevisionSource.UNKNOWN
        ),
    )


def build_manifest(corpus_dir: Path, repo_root: Path, previous: Manifest | None = None) -> Manifest:
    """Probe every PDF under ``corpus_dir`` and produce a manifest.

    ``previous`` supplies ``retrieved_at`` for documents whose content hash is
    unchanged, keeping rebuilds byte-stable.
    """
    documents: list[Document] = []
    for pdf_path in sorted(corpus_dir.rglob("*.pdf")):
        sha = sha256_file(pdf_path)
        doc_id, *_ = _identify(pdf_path, repo_root)

        retrieved_at = datetime.fromtimestamp(pdf_path.stat().st_mtime, tz=UTC)
        if previous and (prior := previous.by_id(doc_id)) and prior.source.sha256_pdf == sha:
            retrieved_at = prior.source.retrieved_at

        documents.append(_probe(pdf_path, repo_root, retrieved_at, sha))

    return Manifest(documents=tuple(sorted(documents, key=lambda d: d.doc_id)))


# --- Serialisation --------------------------------------------------------


def _document_to_dict(doc: Document) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "iso": doc.iso.value,
        "doc_type": doc.doc_type.value,
        "title": doc.title,
        "title_source": doc.title_source.value,
        "status": doc.status.value,
        "page_count": doc.page_count,
        "extractor": doc.extractor,
        "extractor_version": doc.extractor_version,
        "revision": doc.revision,
        "revision_source": doc.revision_source.value,
        "source": {
            "origin": doc.source.origin.value,
            "path": doc.source.path,
            "retrieved_at": doc.source.retrieved_at.isoformat(),
            "sha256_pdf": doc.source.sha256_pdf,
            "url": doc.source.url,
        },
        "effective_dates": [
            {
                "date": eff.date.isoformat() if eff.date else None,
                "date_text": eff.date_text,
                "docket": eff.docket,
                "pages": format_page_ranges(eff.pages),
            }
            for eff in doc.effective_dates
        ],
    }


def _document_from_dict(raw: dict[str, Any]) -> Document:
    source = raw["source"]
    return Document(
        doc_id=raw["doc_id"],
        iso=ISO(raw["iso"]),
        doc_type=DocType(raw["doc_type"]),
        title=raw["title"],
        title_source=TitleSource(raw["title_source"]),
        source=SourceRef(
            origin=Origin(source["origin"]),
            path=source["path"],
            retrieved_at=datetime.fromisoformat(source["retrieved_at"]),
            sha256_pdf=source["sha256_pdf"],
            url=source.get("url"),
        ),
        page_count=raw["page_count"],
        status=DocStatus(raw["status"]),
        extractor=raw["extractor"],
        extractor_version=raw["extractor_version"],
        effective_dates=tuple(
            EffectiveDate(
                date_text=eff["date_text"],
                docket=eff["docket"],
                pages=parse_page_ranges(eff["pages"]),
                date=date.fromisoformat(eff["date"]) if eff.get("date") else None,
            )
            for eff in raw.get("effective_dates", [])
        ),
        revision=raw.get("revision"),
        revision_source=RevisionSource(raw.get("revision_source", "unknown")),
    )


def save(manifest: Manifest, path: Path) -> None:
    payload = {
        "version": manifest.version,
        "documents": [_document_to_dict(d) for d in manifest.documents],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by `tariffrag manifest build` -- do not edit by hand.\n"
        "#\n"
        "# Documents were supplied directly rather than fetched, so they carry no\n"
        "# verified URL. Appendices C, D and G carry no effective-date stamp at all;\n"
        "# that is a property of the documents, not a parsing failure.\n"
    )
    # sort_keys=False keeps the field order above, which reads far better in a
    # diff than alphabetical order.
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(header + body)


def load(path: Path) -> Manifest:
    raw = yaml.safe_load(path.read_text())
    return Manifest(
        documents=tuple(_document_from_dict(d) for d in raw.get("documents", [])),
        version=raw.get("version", MANIFEST_VERSION),
    )


def diff_manifest(manifest: Manifest, corpus_dir: Path, repo_root: Path) -> ManifestDiff:
    """Compare the manifest against what is actually on disk, by content hash."""
    on_disk: dict[str, str] = {}
    for pdf_path in sorted(corpus_dir.rglob("*.pdf")):
        doc_id, *_ = _identify(pdf_path, repo_root)
        on_disk[doc_id] = sha256_file(pdf_path)

    recorded = {d.doc_id: d.source.sha256_pdf for d in manifest.documents}

    return ManifestDiff(
        added=tuple(sorted(set(on_disk) - set(recorded))),
        removed=tuple(sorted(set(recorded) - set(on_disk))),
        changed=tuple(sorted(k for k in set(on_disk) & set(recorded) if on_disk[k] != recorded[k])),
    )
