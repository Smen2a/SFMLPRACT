"""Tests for canonical text, offsets and the section tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tariffrag.config import settings
from tariffrag.ingest.extract import build_canonical, build_sections, load_canonical, normalize
from tariffrag.ingest.pipeline import load_sections
from tariffrag.ingest.structure import parse_document

FIXTURES = Path(__file__).parent / "fixtures"
TEXT_DIR = settings.text_dir

needs_ingest = pytest.mark.skipif(
    not (TEXT_DIR.exists() and any(TEXT_DIR.glob("*.ingest.json"))),
    reason="corpus not ingested; run `tariffrag ingest`",
)


# --- Normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    "term",
    ["De-List", "Real-Time", "Rest-of-Pool", "Non-Spinning", "Import-Constrained"],
)
def test_hyphenated_defined_terms_survive(term: str) -> None:
    """The regression that matters most.

    De-hyphenating line-broken words is the textbook normalisation step and is
    destructive here: these are Defined Terms, and joining them would break
    exact-match retrieval on the terms carrying the most weight.
    """
    assert normalize(f"the {term} Bid") == f"the {term} Bid"


def test_curly_quotes_are_flattened() -> None:
    assert normalize("Participant’s “bid”") == 'Participant\'s "bid"'


def test_whitespace_is_collapsed_but_dashes_are_kept() -> None:
    """En dashes carry meaning in page ranges and effective-date stamps."""
    assert normalize("a   b\tc") == "a b c"
    assert "–" in normalize("2028 – 2029")


def test_normalize_is_idempotent() -> None:
    once = normalize("Participant’s  De-List   Bid")
    assert normalize(once) == once


# --- Canonical text -------------------------------------------------------


@pytest.fixture(scope="module")
def nested_canonical():  # type: ignore[no-untyped-def]
    structure = parse_document(FIXTURES / "tariff_nested.pdf")
    return structure, build_canonical("fix:nested", structure)


def test_furniture_is_excluded_from_canonical_text(nested_canonical) -> None:  # type: ignore[no-untyped-def]
    _, canonical = nested_canonical
    assert "ISO New England Manual" not in canonical.text
    assert "Forward Capacity Market" in canonical.text


def test_every_line_offset_resolves_to_its_own_page(nested_canonical) -> None:  # type: ignore[no-untyped-def]
    _, canonical = nested_canonical
    for span in canonical.lines:
        assert canonical.page_for_offset(span.start) == span.page
        assert canonical.slice(span.start, span.end)


def test_sections_span_their_own_heading(nested_canonical) -> None:  # type: ignore[no-untyped-def]
    structure, canonical = nested_canonical
    sections = build_sections("fix:nested", structure, canonical)
    assert sections
    for section in sections:
        if section.section_id:
            assert canonical.slice(section.char_start, section.char_end).startswith(
                section.section_id
            )


def test_section_tree_nests_and_covers_everything(nested_canonical) -> None:  # type: ignore[no-untyped-def]
    structure, canonical = nested_canonical
    sections = build_sections("fix:nested", structure, canonical)

    covered = sum(s.char_end - s.char_start for s in sections)
    assert covered == len(canonical.text), "sections must tile the text with no gaps"

    by_id = {s.section_id: s for s in sections}
    child = by_id["III.13.1.1"]
    assert child.parent_id == "III.13.1"
    assert child.depth == 4
    assert by_id["III.13"].parent_id is None


# --- The real corpus ------------------------------------------------------


@needs_ingest
def test_offsets_round_trip_across_the_corpus() -> None:
    """The check that stops citations pointing at the wrong words.

    Nothing downstream can detect a bad offset, so it is asserted directly: each
    section's span must start with its own id, and its recorded page must agree
    with resolving its start offset.
    """
    checked = 0
    for record_path in sorted(TEXT_DIR.glob("*.ingest.json")):
        doc_id = json.loads(record_path.read_text())["doc_id"]
        canonical = load_canonical(doc_id, TEXT_DIR)  # also verifies the stored hash
        for section in load_sections(doc_id, TEXT_DIR):
            if section.section_id:
                assert canonical.slice(section.char_start, section.char_end).startswith(
                    section.section_id
                )
            assert canonical.page_for_offset(section.char_start) == section.page_start
            checked += 1
    assert checked > 1000, "expected the whole corpus to be covered"


@needs_ingest
def test_reserved_documents_are_not_ingested() -> None:
    ingested = {json.loads(p.read_text())["doc_id"] for p in TEXT_DIR.glob("*.ingest.json")}
    for reserved in ("append_b", "append_e", "append_h", "append_j"):
        assert f"isone:mr1:{reserved}" not in ingested


@needs_ingest
def test_canonical_text_hash_is_enforced(tmp_path: Path) -> None:
    """A mismatched hash must fail loudly: every offset is relative to the text."""
    doc_id = "isone:mr1:sec_14"
    for suffix in (".txt", ".layout.json"):
        (tmp_path / f"{doc_id}{suffix}").write_text(
            (TEXT_DIR / f"{doc_id}{suffix}").read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tmp_path / f"{doc_id}.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match its recorded hash"):
        load_canonical(doc_id, tmp_path)
