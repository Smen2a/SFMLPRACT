"""Tests for the parser spike.

Two layers. The fixtures (committed, byte-deterministic; regenerate with
``python tests/fixtures/make_fixtures.py``) each encode one hazard in isolation.
The real-corpus tests pin behaviour against actual ISO-NE Market Rule 1 text,
using only the small appendices so the suite stays fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tariffrag.ingest.spike import (
    _EFFECTIVE_DATE,
    MARGIN_BAND_RATIO,
    MIN_FURNITURE_PAGES,
    SpikeReport,
    Verdict,
    _heading_shape,
    _longest_increasing,
    parse_section_id,
    spike_document,
)

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "isone" / "mr1"


def _fixture(name: str) -> SpikeReport:
    return spike_document(FIXTURES / f"{name}.pdf")


@pytest.fixture(scope="module")
def nested() -> SpikeReport:
    return _fixture("tariff_nested")


@pytest.fixture(scope="module")
def flat() -> SpikeReport:
    return _fixture("tariff_flat")


@pytest.fixture(scope="module")
def xref_trap() -> SpikeReport:
    return _fixture("tariff_xref_trap")


@pytest.fixture(scope="module")
def scanned() -> SpikeReport:
    return _fixture("tariff_scanned")


# --- Section id parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "display", "sort_key", "scheme"),
    [
        ("III.13.1.2 Qualification Process", "III.13.1.2", (3, 13, 1, 2), "roman_dotted"),
        ("III.13. Forward Capacity Market.", "III.13", (3, 13), "roman_dotted"),
        ("III.A.1.1 Mission Statement", "III.A.1.1", (3, 1065, 1, 1), "roman_letter"),
        ("III.A.1. Introduction", "III.A.1", (3, 1065, 1), "roman_letter"),
        ("2.1.3 Scheduling", "2.1.3", (2, 1, 3), "plain"),
        ("Appendix A Auction Rules", "Appendix A", (65,), "lettered"),
    ],
)
def test_parses_section_ids(
    text: str, display: str, sort_key: tuple[int, ...], scheme: str
) -> None:
    parsed = parse_section_id(text)
    assert parsed is not None
    assert (parsed.display, parsed.sort_key, parsed.scheme) == (display, sort_key, scheme)


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
    assert parse_section_id("13 Something") is None
    parsed = parse_section_id("13.1 Something")
    assert parsed is not None and parsed.display == "13.1"


def test_appendix_ids_sort_above_numbered_sections() -> None:
    """``III.A.1`` must not interleave with ``III.15.x`` in the same sequence."""
    appendix = parse_section_id("III.A.1 Introduction")
    section = parse_section_id("III.15.1 Something")
    assert appendix is not None and section is not None
    assert appendix.sort_key > section.sort_key
    assert appendix.scheme != section.scheme


# --- Longest increasing subsequence ---------------------------------------


def test_lis_drops_the_outlier_not_the_tail() -> None:
    """The property that protects against cascade poisoning.

    A greedy monotonic walk would accept 5, then reject 2, 3 and 4 for failing to
    exceed it. On the real corpus that cost 180 genuine headings in Appendix A,
    where a stray id set an unreachable watermark.
    """
    keys: list[tuple[int, ...]] = [(1,), (5,), (2,), (3,), (4,)]
    assert sorted(_longest_increasing(keys)) == [0, 2, 3, 4]


def test_lis_handles_empty_input() -> None:
    assert _longest_increasing([]) == set()


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

    This is the load-bearing claim of the cascade: with headings set in body
    type, numbering plus the prose gate plus sequence consistency still recover
    every heading.
    """
    assert flat.verdict is Verdict.DEGRADED
    assert flat.fonts is not None
    assert not flat.fonts.headings_typographically_distinct
    assert len(flat.accepted) == 6


# --- The cross-reference trap ---------------------------------------------


def test_wrapped_cross_references_are_never_promoted(xref_trap: SpikeReport) -> None:
    """The metric that matters.

    A promoted reference fabricates a section that does not exist and corrupts
    every boundary after it, so this must be exactly zero.
    """
    audit = xref_trap.cross_refs
    assert audit.inline_refs_seen > 0, "fixture should contain the hazard"
    assert audit.prose_lines_starting_with_id > 0, "fixture should contain wrapped refs"
    assert audit.misread_as_headings == 0


def test_only_real_headings_accepted_in_trap(xref_trap: SpikeReport) -> None:
    assert [c.section_id for c in xref_trap.accepted] == ["III.12", "III.12.2", "III.13"]


# --- Page furniture -------------------------------------------------------


def test_furniture_is_header_and_footer_only(nested: SpikeReport) -> None:
    """Body text must not be mistaken for a running header.

    Furniture is identified by position, low text variance, and recurrence across
    pages together. Position alone flags the first body line of every page; text
    matching alone misses ISO-NE footers, which vary by effective date.
    """
    texts = [line.text for line in nested.repeated_lines]
    assert len(texts) == 2
    assert any("ISO New England" in t for t in texts)
    assert any("Page" in t for t in texts)
    assert all("Market Participant" not in t for t in texts)


def test_margin_band_is_a_minority_of_the_page() -> None:
    assert 0 < MARGIN_BAND_RATIO < 0.25


def test_furniture_requires_recurrence() -> None:
    assert MIN_FURNITURE_PAGES >= 2


# --- Real ISO-NE Market Rule 1 --------------------------------------------

pytestmark_corpus = pytest.mark.skipif(
    not CORPUS.exists(), reason="Market Rule 1 corpus not present"
)


@pytestmark_corpus
def test_reserved_appendix_is_empty_not_no_go() -> None:
    """Appendix H is an intentionally blank tariff section.

    Reporting it as NO_GO would send someone looking for an OCR fix for a
    document that has no content to recover.
    """
    report = spike_document(CORPUS / "mr1_append_h.pdf")
    assert report.verdict is Verdict.EMPTY
    assert report.text_layer.is_reserved_placeholder
    assert any("nothing is lost" in note for note in report.notes)


@pytestmark_corpus
def test_reserved_appendix_with_cover_boilerplate_is_empty() -> None:
    """Appendix E clears the text-layer threshold but is still `[RESERVED]`."""
    report = spike_document(CORPUS / "mr1_append_e.pdf")
    assert report.text_layer.has_text_layer
    assert report.verdict is Verdict.EMPTY


@pytestmark_corpus
def test_substantive_one_page_appendix_is_not_empty() -> None:
    """Appendix L is one page of real content; the RESERVED rule must not eat it."""
    report = spike_document(CORPUS / "mr1_append_l.pdf")
    assert report.verdict is not Verdict.EMPTY
    assert report.text_layer.total_chars > 1000


@pytestmark_corpus
def test_real_appendix_parses_cleanly() -> None:
    report = spike_document(CORPUS / "mr1_append_g.pdf")
    assert report.verdict is Verdict.GO
    assert report.accepted
    assert report.sequence is not None
    assert report.sequence.violations == ()
    assert report.cross_refs.misread_as_headings == 0


@pytestmark_corpus
def test_effective_dates_are_captured_from_page_stamps() -> None:
    """ISO-NE versions at finer granularity than the file.

    Every page carries an effective date and a FERC docket number, and the dates
    differ within a single document, so this is versioning metadata rather than
    furniture to discard.
    """
    report = spike_document(CORPUS / "mr1_append_a.pdf")
    assert report.effective_dates
    assert all(eff.docket.startswith("ER") for eff in report.effective_dates)


@pytest.mark.parametrize(
    ("stamp", "date", "docket"),
    [
        ("Effective Date: 3/31/26 – Docket No. ER26-925-000", "3/31/26", "ER26-925-000"),
        ("Effective Date: 6/1/2018 - Docket # ER17-2164-000", "6/1/2018", "ER17-2164-000"),
        ("Effective Date: 8/27/2021 - Docket #: ER21-2220-000", "8/27/2021", "ER21-2220-000"),
        (
            "Effective Date: March 31, 2026 – Docket No. ER26-925-000",
            "March 31, 2026",
            "ER26-925-000",
        ),
    ],
)
def test_effective_date_stamp_variants(stamp: str, date: str, docket: str) -> None:
    """All four forms occur in Market Rule 1.

    The separator is an en dash in some documents and a hyphen in others, and the
    docket label appears as "Docket No.", "Docket #", or "Docket #:".
    """
    match = _EFFECTIVE_DATE.search(stamp)
    assert match is not None
    assert match.group("date") == date
    assert match.group("docket") == docket


@pytest.mark.parametrize(
    "text",
    [
        "III.13.1.1.2.5.2. [Reserved]",
        "III.13.2.7.3. [Reserved.]",
        "III.1.2 [Reserved.]",
    ],
)
def test_reserved_subsections_are_headings(text: str) -> None:
    """ISO-NE repeals subsections in place rather than renumbering.

    These are real nodes of the section tree. Dropping them manufactures gaps --
    on Market Rule 1 it produced seven false ones -- and leaves the tree
    incomplete. "[" is not uppercase, so the title-case gate rejects them unless
    they are recognised first.
    """
    parsed = parse_section_id(text)
    assert parsed is not None
    _, reason = _heading_shape(text, parsed.display)
    assert reason is None


def test_prose_continuation_is_still_gated() -> None:
    """The reserved carve-out must not widen the prose gate."""
    parsed = parse_section_id("III.14 shall be construed to limit the foregoing")
    assert parsed is not None
    _, reason = _heading_shape("III.14 shall be construed to limit the foregoing", parsed.display)
    assert reason is not None


@pytest.mark.parametrize(
    ("text", "display", "scheme"),
    [
        ("III.13.A.1. [Reserved.]", "III.13.A.1", "roman_dotted"),
        ("III.A.1. Introduction", "III.A.1", "roman_letter"),
    ],
)
def test_lettered_segments(text: str, display: str, scheme: str) -> None:
    """A letter directly after the roman major is the appendix scheme.

    A lettered segment deeper in the tree belongs to the body sequence and must
    be validated alongside it, not split into a separate scheme.
    """
    parsed = parse_section_id(text)
    assert parsed is not None
    assert (parsed.display, parsed.scheme) == (display, scheme)
