"""Tests for the corpus manifest."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from tariffrag.ingest.manifest import (
    build_manifest,
    diff_manifest,
    load,
    save,
)
from tariffrag.ingest.spike import _parse_stamp_date
from tariffrag.models import DocStatus, TitleSource, format_page_ranges, parse_page_ranges

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = REPO_ROOT / "corpus" / "isone" / "mr1"
MANIFEST = REPO_ROOT / "corpus" / "manifest.yaml"
FIXTURES = Path(__file__).parent / "fixtures"

needs_corpus = pytest.mark.skipif(not CORPUS.exists(), reason="corpus not present")
needs_manifest = pytest.mark.skipif(not MANIFEST.exists(), reason="manifest not built")


# --- Page ranges ----------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "text"),
    [
        ((1, 2, 3), "1-3"),
        ((1, 2, 3, 7), "1-3,7"),
        ((4, 5, 103, 104, 232), "4-5,103-104,232"),
        ((9,), "9"),
        ((), ""),
    ],
)
def test_page_ranges_round_trip(pages: tuple[int, ...], text: str) -> None:
    """Non-contiguous ranges occur for real: Sections 13-14 has 4-5,103-182,232-235."""
    assert format_page_ranges(pages) == text
    assert parse_page_ranges(text) == pages


# --- Date stamps ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8/27/2021", date(2021, 8, 27)),
        ("3/31/26", date(2026, 3, 31)),
        ("March 31, 2026", date(2026, 3, 31)),
        ("not a date", None),
    ],
)
def test_parses_stamp_dates(text: str, expected: date | None) -> None:
    """Two-digit years resolve by Python's %y rule, so 26 is 2026, not 1926."""
    assert _parse_stamp_date(text) == expected


# --- Round trip and drift -------------------------------------------------


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A miniature corpus laid out the way the real one is."""
    root = tmp_path_factory.mktemp("repo")
    target = root / "corpus" / "isone" / "mr1"
    target.mkdir(parents=True)
    for name in ("tariff_nested", "tariff_flat"):
        shutil.copy(FIXTURES / f"{name}.pdf", target / f"mr1_{name}.pdf")
    return root


def test_build_and_round_trip(fixture_repo: Path, tmp_path: Path) -> None:
    corpus = fixture_repo / "corpus"
    built = build_manifest(corpus, fixture_repo)
    assert len(built.documents) == 2
    assert {d.doc_id for d in built.documents} == {
        "isone:mr1:tariff_nested",
        "isone:mr1:tariff_flat",
    }

    path = tmp_path / "manifest.yaml"
    save(built, path)
    reloaded = load(path)

    assert reloaded.documents == built.documents, "YAML round trip must preserve every field"


def test_rebuild_is_byte_stable(fixture_repo: Path, tmp_path: Path) -> None:
    """A manifest that churns on every build stops being a record of change."""
    corpus = fixture_repo / "corpus"
    first_path, second_path = tmp_path / "a.yaml", tmp_path / "b.yaml"

    first = build_manifest(corpus, fixture_repo)
    save(first, first_path)
    # Rebuild passing the previous manifest, as the CLI does.
    second = build_manifest(corpus, fixture_repo, previous=load(first_path))
    save(second, second_path)

    assert first_path.read_bytes() == second_path.read_bytes()


def test_diff_detects_added_removed_and_changed(fixture_repo: Path, tmp_path: Path) -> None:
    corpus = fixture_repo / "corpus"
    built = build_manifest(corpus, fixture_repo)
    assert diff_manifest(built, corpus, fixture_repo).clean

    # Work on a copy so the module-scoped fixture stays intact.
    scratch = tmp_path / "repo"
    shutil.copytree(fixture_repo, scratch)
    scratch_corpus = scratch / "corpus"
    target = scratch_corpus / "isone" / "mr1"

    shutil.copy(FIXTURES / "tariff_xref_trap.pdf", target / "mr1_added.pdf")
    (target / "mr1_tariff_flat.pdf").unlink()
    with (target / "mr1_tariff_nested.pdf").open("ab") as handle:
        handle.write(b"%% drift")

    delta = diff_manifest(built, scratch_corpus, scratch)
    assert not delta.clean
    assert delta.added == ("isone:mr1:added",)
    assert delta.removed == ("isone:mr1:tariff_flat",)
    assert delta.changed == ("isone:mr1:tariff_nested",)


# --- The real manifest ----------------------------------------------------


@needs_manifest
def test_real_manifest_shape() -> None:
    built = load(MANIFEST)
    assert len(built.documents) == 16
    assert len(built.ingestable) == 12

    reserved = {d.doc_id for d in built.documents if d.status is DocStatus.RESERVED}
    assert reserved == {
        "isone:mr1:append_b",
        "isone:mr1:append_e",
        "isone:mr1:append_h",
        "isone:mr1:append_j",
    }
    assert not [d for d in built.documents if d.status is DocStatus.EXCLUDED], (
        "nothing should be dropped as unreadable"
    )
    assert all(d.title for d in built.documents)


@needs_manifest
def test_no_document_claims_an_unverified_url() -> None:
    """A plausible URL nobody checked would put a false claim under a citation."""
    assert all(d.source.url is None for d in load(MANIFEST).documents)


@needs_manifest
def test_titles_come_from_the_document_not_the_filename() -> None:
    built = load(MANIFEST)
    assert not [d for d in built.documents if d.title_source is TitleSource.FILENAME]
    heading_titled = {
        d.doc_id for d in built.documents if d.title_source is TitleSource.FIRST_HEADING
    }
    assert heading_titled == {
        "isone:mr1:sec_13_14",
        "isone:mr1:sec_14",
        "isone:mr1:sec_15",
    }


@needs_manifest
def test_effective_dates_are_per_section_not_per_document() -> None:
    """The finding that changed the versioning design.

    Sections 13-14 carry four stamps, so a citation must resolve the one
    governing its own page rather than a single document-level date.
    """
    doc = load(MANIFEST).by_id("isone:mr1:sec_13_14")
    assert doc is not None
    assert len(doc.effective_dates) == 4

    primary = doc.primary_effective_date
    assert primary is not None and primary.date == date(2025, 5, 3)

    # A page outside the primary range resolves to its own stamp.
    other = doc.effective_date_for_page(1)
    assert other is not None and other.date == date(2026, 3, 31)
    assert "eff. 2026-03-31" in doc.cite_label(page=1)
    assert "eff. 2025-05-03" in doc.cite_label()


@needs_manifest
def test_documents_without_a_stamp_record_unknown() -> None:
    """Appendices C, D and G carry no effective date. That is a corpus fact."""
    built = load(MANIFEST)
    for doc_id in ("isone:mr1:append_c", "isone:mr1:append_d", "isone:mr1:append_g"):
        doc = built.by_id(doc_id)
        assert doc is not None
        assert doc.effective_dates == ()
        assert doc.primary_effective_date is None
        assert doc.revision_source.value == "unknown"


@needs_corpus
@needs_manifest
def test_manifest_matches_the_corpus_on_disk() -> None:
    built = load(MANIFEST)
    assert diff_manifest(built, REPO_ROOT / "corpus", REPO_ROOT).clean
