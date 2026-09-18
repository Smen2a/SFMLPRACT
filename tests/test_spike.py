"""Tests for the parser spike.

The fixtures are committed so these run without reportlab; regenerate them with
``python tests/fixtures/make_fixtures.py`` (output is byte-deterministic).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tariffrag.ingest.spike import (
    MARGIN_BAND_RATIO,
    SpikeReport,
    Verdict,
    parse_section_id,
    spike_document,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def nested() -> SpikeReport:
    return spike_document(FIXTURES / "tariff_nested.pdf")


@pytest.fixture(scope="module")
def flat() -> SpikeReport:
    return spike_document(FIXTURES / "tariff_flat.pdf")


@pytest.fixture(scope="module")
def xref_trap() -> SpikeReport:
    return spike_document(FIXTURES / "tariff_xref_trap.pdf")


@pytest.fixture(scope="module")
def scanned() -> SpikeReport:
    return spike_document(FIXTURES / "tariff_scanned.pdf")


# --- Section id parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("III.13.1.2 Qualification Process", ("III.13.1.2", (3, 13, 1, 2))),
        ("III.13 Forward Capacity Market", ("III.13", (3, 13))),
        ("2.1.3 Scheduling", ("2.1.3", (2, 1, 3))),
        ("Appendix A Auction Rules", ("Appendix A", (65,))),
        ("Attachment H Rate Schedule", ("Attachment H", (72,))),
    ],
)
def test_parses_section_ids(text: str, expected: tuple[str, tuple[int, ...]]) -> None:
    assert parse_section_id(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The Market Participant shall submit documentation.",
        "Section III.12.2 shall apply",  # leading keyword: a reference, not an id
        "412",  # a bare integer is a page number far more often than a heading
        "",
    ],
)
def test_rejects_non_section_ids(text: str) -> None:
    assert parse_section_id(text) is None


def test_bare_integer_requires_a_dot() -> None:
    """``13`` is a page number; ``13.1`` is plausibly a section."""
    assert parse_section_id("13 Something") is None
    assert parse_section_id("13.1 Something") == ("13.1", (13, 1))


# --- Verdicts -------------------------------------------------------------


def test_well_formed_document_is_go(nested: SpikeReport) -> None:
    assert nested.verdict is Verdict.GO
    assert [c.section_id for c in nested.accepted] == [
        "III.13",
        "III.13.1",
        "III.13.1.1",
        "III.13.1.2",
        "III.13.2",
        "III.14",
    ]
    assert nested.sequence is not None
    assert nested.sequence.violations == ()


def test_scanned_document_is_no_go(scanned: SpikeReport) -> None:
    """No text layer means Claude cannot cite it at all."""
    assert scanned.verdict is Verdict.NO_GO
    assert not scanned.text_layer.has_text_layer
    assert scanned.accepted == ()
    assert any("dropped from the corpus" in note for note in scanned.notes)


def test_flat_document_is_degraded_but_still_parses(flat: SpikeReport) -> None:
    """Typography is a bonus signal, not a requirement.

    This is the load-bearing claim of the whole cascade: with headings set in
    body type, numbering plus the prose gate plus sequence consistency still
    recover every heading.
    """
    assert flat.verdict is Verdict.DEGRADED
    assert flat.fonts is not None
    assert not flat.fonts.headings_typographically_distinct
    assert len(flat.accepted) == 6
    assert flat.sequence is not None
    assert flat.sequence.violations == ()


# --- The cross-reference trap ---------------------------------------------


def test_wrapped_cross_references_are_never_promoted(xref_trap: SpikeReport) -> None:
    """The metric that matters.

    Line wrapping puts ``III.14 shall be construed ...`` at the start of a line,
    where numbering alone cannot distinguish it from a heading. A promoted
    reference fabricates a section and corrupts every boundary after it, so this
    must be exactly zero.
    """
    audit = xref_trap.cross_refs
    assert audit.inline_refs_seen > 0, "fixture should contain the hazard"
    assert audit.prose_lines_starting_with_id > 0, "fixture should contain wrapped refs"
    assert audit.misread_as_headings == 0


def test_only_real_headings_accepted_in_trap(xref_trap: SpikeReport) -> None:
    assert [c.section_id for c in xref_trap.accepted] == ["III.12", "III.12.2", "III.13"]


def test_gated_candidates_carry_a_reason(xref_trap: SpikeReport) -> None:
    gated = [c for c in xref_trap.candidates if c.rejected_reason]
    assert gated, "wrapped prose should be gated, not merely low-scored"
    assert all(not c.accepted for c in gated)
    assert all("prose" in c.rejected_reason or "sentence" in c.rejected_reason for c in gated)  # type: ignore[operator]


# --- Page furniture -------------------------------------------------------


def test_furniture_detected_in_margins_only(nested: SpikeReport) -> None:
    """Repeating body text must not be mistaken for a running header.

    The first run of this spike flagged repeated body sentences as furniture,
    because the detector keyed on recurrence alone. Running headers and footers
    are positionally constrained, so the margin band is part of the definition.
    """
    assert len(nested.repeated_lines) == 2
    texts = [line.text for line in nested.repeated_lines]
    assert any("ISO New England" in t for t in texts)
    assert any("Page" in t for t in texts)
    assert all("Market Participant" not in t for t in texts)


def test_margin_band_is_a_minority_of_the_page() -> None:
    assert 0 < MARGIN_BAND_RATIO < 0.25
