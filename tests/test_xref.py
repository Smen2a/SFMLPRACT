"""Tests for cross-reference extraction and resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tariffrag.config import settings
from tariffrag.ingest.extract import build_canonical, build_sections
from tariffrag.ingest.structure import parse_document
from tariffrag.ingest.xref import extract_xrefs

FIXTURES = Path(__file__).parent / "fixtures"
TEXT_DIR = settings.text_dir

needs_ingest = pytest.mark.skipif(
    not (TEXT_DIR.exists() and any(TEXT_DIR.glob("*.ingest.json"))),
    reason="corpus not ingested; run `tariffrag ingest`",
)


@pytest.fixture(scope="module")
def trap_report():  # type: ignore[no-untyped-def]
    structure = parse_document(FIXTURES / "tariff_xref_trap.pdf")
    canonical = build_canonical("fix:trap", structure)
    sections = build_sections("fix:trap", structure, canonical)
    return extract_xrefs("fix:trap", canonical, sections)


def test_finds_references_in_prose(trap_report) -> None:  # type: ignore[no-untyped-def]
    assert len(trap_report.references) > 10
    assert any(r.raw_text.startswith("Section") for r in trap_report.references)


def test_resolves_against_the_section_tree(trap_report) -> None:  # type: ignore[no-untyped-def]
    resolved = [r for r in trap_report.references if r.resolved]
    assert resolved
    assert all(r.to_section_id for r in resolved)


def test_reference_lists_are_split_into_separate_targets(trap_report) -> None:  # type: ignore[no-untyped-def]
    """``Sections III.13.1 and III.13.2`` is two references, not one."""
    targets = {r.raw_text for r in trap_report.references}
    assert any("III.13.1" in t for t in targets)
    assert any("III.13.2" in t for t in targets)


def test_bare_letters_are_only_appendix_references() -> None:
    """Allowing a bare letter after "Section" invented matches like "Section S"."""
    structure = parse_document(FIXTURES / "tariff_xref_trap.pdf")
    canonical = build_canonical("fix:trap", structure)
    sections = build_sections("fix:trap", structure, canonical)
    report = extract_xrefs("fix:trap", canonical, sections)
    for ref in report.references:
        kind, _, target = ref.raw_text.partition(" ")
        if len(target) == 1 and target.isalpha():
            assert not kind.lower().startswith("section")


@needs_ingest
def test_corpus_resolution_rate_holds() -> None:
    """Market Rule 1 is split across files whose sections cite each other.

    Resolving within one document alone reports 75%; across the corpus it is
    ~92%. The remainder are genuinely outside the corpus -- Section I of the
    tariff, other manuals -- which is a scope fact, not a parser defect.
    """
    total = resolved = 0
    for record_path in TEXT_DIR.glob("*.ingest.json"):
        record = json.loads(record_path.read_text())
        total += record["xref_count"]
        resolved += record["xref_resolved"]

    assert total > 2000, "expected a large reference sample"
    assert resolved / total >= 0.90
