"""Diagnostic report over a parsed document.

Measures whether a PDF's section structure is recoverable and returns a verdict,
so a document can be judged fit for the corpus before anything is built on it.

The parsing itself lives in ``structure.py``; this module only judges and renders
what that produced. The split matters: a diagnostic that measured a different
parser than the one that ships would read as reassurance while telling you
nothing.

``GO``
    Structure is recoverable using the full signal cascade.
``DEGRADED``
    Recoverable, but a signal is unavailable or the sequence is still noisy.
``EMPTY``
    An intentionally blank tariff section (``[RESERVED]``). Excluded from the
    corpus, but nothing is lost -- distinct from ``NO_GO`` so nobody goes
    looking for an OCR fix for a document that has no content to recover.
``NO_GO``
    Content exists but is not extractable -- a scan. Claude's citations require
    a text layer, so such a document cannot be cited and must be dropped with
    the exclusion stated in the README.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from rich.console import Console
from rich.table import Table

from tariffrag.ingest.structure import (
    CrossRefAudit,
    DocumentStructure,
    FontProfile,
    HeaderCandidate,
    RepeatedLine,
    SequenceReport,
    TextLayerReport,
    parse_document,
    parse_section_id,
)
from tariffrag.models import EffectiveDate, TitleSource

__all__ = ["SpikeReport", "Verdict", "parse_section_id", "render_report", "spike_document"]


class Verdict(StrEnum):
    GO = "go"
    DEGRADED = "degraded"
    EMPTY = "empty"
    NO_GO = "no_go"


@dataclass(slots=True)
class SpikeReport:
    """A parsed document plus a judgement about it.

    Field access is delegated to the structure so callers read one object, but
    the structure remains the single source of truth.
    """

    structure: DocumentStructure
    notes: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.structure.path

    @property
    def text_layer(self) -> TextLayerReport:
        return self.structure.text_layer

    @property
    def fonts(self) -> FontProfile | None:
        return self.structure.fonts

    @property
    def repeated_lines(self) -> tuple[RepeatedLine, ...]:
        return self.structure.repeated_lines

    @property
    def candidates(self) -> tuple[HeaderCandidate, ...]:
        return self.structure.candidates

    @property
    def sequence(self) -> SequenceReport | None:
        return self.structure.sequence

    @property
    def cross_refs(self) -> CrossRefAudit:
        return self.structure.cross_refs

    @property
    def toc_pages(self) -> tuple[int, ...]:
        return self.structure.toc_pages

    @property
    def effective_dates(self) -> tuple[EffectiveDate, ...]:
        return self.structure.effective_dates

    @property
    def title(self) -> str:
        return self.structure.title

    @property
    def title_source(self) -> TitleSource:
        return self.structure.title_source

    @property
    def accepted(self) -> tuple[HeaderCandidate, ...]:
        return self.structure.accepted

    @property
    def verdict(self) -> Verdict:
        layer = self.structure.text_layer
        if layer.is_reserved_placeholder:
            return Verdict.EMPTY
        if not layer.has_text_layer:
            return Verdict.NO_GO
        if self.structure.cross_refs.misread_as_headings:
            return Verdict.DEGRADED
        if self.structure.sequence and self.structure.sequence.violations:
            return Verdict.DEGRADED
        fonts = self.structure.fonts
        if fonts is None or not fonts.headings_typographically_distinct:
            return Verdict.DEGRADED
        return Verdict.GO


def _build_notes(structure: DocumentStructure, verdict: Verdict) -> list[str]:
    if verdict is Verdict.EMPTY:
        return [
            "Intentionally blank tariff section ([RESERVED]). Excluded from the corpus, "
            "but nothing is lost -- there is no content to recover."
        ]
    if verdict is Verdict.NO_GO:
        return [
            "No usable text layer -- this is a scan. Claude's citations require "
            "extractable text, so it cannot be cited and must be dropped from the "
            "corpus (state the exclusion in the README)."
        ]

    notes: list[str] = []
    if structure.fonts and not structure.fonts.headings_typographically_distinct:
        notes.append(
            "Headings are not typographically distinct from body text. The typography "
            "signal is disabled for this document; detection relies on numbering, the "
            "prose gate, and sequence consistency alone."
        )
    if not structure.repeated_lines and structure.text_layer.page_count >= 2:
        notes.append(
            "No running header/footer detected. Verify manually -- if furniture is "
            "present but undetected it will pollute every chunk and shift offsets."
        )
    if structure.cross_refs.misread_as_headings:
        notes.append(
            f"{structure.cross_refs.misread_as_headings} wrapped cross-reference(s) were "
            "accepted as headings. This must be zero: a promoted reference fabricates a "
            "section and corrupts every boundary after it."
        )
    if structure.sequence and structure.sequence.violations:
        notes.append(
            f"{len(structure.sequence.violations)} sequence violation(s) survived demotion. "
            "Inspect these: they usually mean an unsupported numbering scheme."
        )
    if len(structure.effective_dates) > 1:
        notes.append(
            f"{len(structure.effective_dates)} distinct effective dates in one document -- "
            "ISO-NE versions at finer granularity than the file, so capture these per section."
        )
    return notes


def spike_document(pdf_path: Path) -> SpikeReport:
    """Parse a document and judge whether it belongs in the corpus."""
    structure = parse_document(pdf_path)
    report = SpikeReport(structure=structure)
    report.notes = _build_notes(structure, report.verdict)
    return report


# --- Rendering ------------------------------------------------------------

_VERDICT_STYLE = {
    Verdict.GO: ("bold green", "structure is recoverable with the full cascade"),
    Verdict.DEGRADED: ("bold yellow", "usable, but a signal is unavailable or noisy"),
    Verdict.EMPTY: ("dim", "intentionally blank tariff section; nothing to recover"),
    Verdict.NO_GO: ("bold red", "drop from the corpus and state the exclusion"),
}


def render_report(report: SpikeReport, console: Console, *, show_candidates: int = 0) -> None:
    """Print a per-document diagnostic report."""
    style, gloss = _VERDICT_STYLE[report.verdict]
    console.print()
    console.rule(f"[{style}]{report.path.name} — {report.verdict.value.upper()}[/{style}]")
    console.print(f"[dim]{gloss}[/dim]\n")

    if report.title:
        console.print(f"[bold]Title[/bold]      {report.title}  [dim]({report.title_source})[/dim]")

    layer = report.text_layer
    console.print(
        f"[bold]Text layer[/bold]  {layer.page_count} pages, "
        f"{layer.total_chars:,} chars, median {layer.median_chars_per_page:,.0f}/page, "
        f"{layer.image_page_ratio:.0%} of pages carry images"
    )

    if report.verdict in (Verdict.NO_GO, Verdict.EMPTY):
        for note in report.notes:
            console.print(f"\n[{'red' if report.verdict is Verdict.NO_GO else 'dim'}]![/] {note}")
        return

    if report.fonts:
        fonts = report.fonts
        mark = (
            "[green]yes[/green]"
            if fonts.headings_typographically_distinct
            else "[yellow]no[/yellow]"
        )
        console.print(
            f"[bold]Fonts[/bold]      body = {fonts.body_font} @ {fonts.body_size}pt; "
            f"headings distinct: {mark}"
        )

    if report.repeated_lines:
        console.print("[bold]Furniture[/bold]  running header/footer positions:")
        for line in report.repeated_lines:
            variants = f" ({line.variants} text variants)" if line.variants > 1 else ""
            console.print(
                f"             top={line.top:6.1f}  {line.page_ratio:.0%}{variants}  {line.text!r}"
            )
    else:
        console.print("[bold]Furniture[/bold]  [yellow]none detected[/yellow]")

    if report.effective_dates:
        console.print("[bold]Versions[/bold]   effective dates stamped on pages:")
        for eff in report.effective_dates[:6]:
            console.print(f"             {eff.date_text}  docket {eff.docket}  ({eff.pages} pages)")

    if report.toc_pages:
        shown = ", ".join(str(p) for p in report.toc_pages[:12])
        more = "" if len(report.toc_pages) <= 12 else f" (+{len(report.toc_pages) - 12} more)"
        console.print(f"[bold]Contents[/bold]   pages excluded as TOC: {shown}{more}")

    cr = report.cross_refs
    misread_style = "green" if cr.misread_as_headings == 0 else "bold red"
    console.print(
        f"[bold]Xrefs[/bold]      {cr.inline_refs_seen} inline; "
        f"{cr.prose_lines_starting_with_id} lines gated; "
        f"[{misread_style}]{cr.misread_as_headings} misread as headings[/{misread_style}]"
    )

    if report.sequence:
        seq = report.sequence
        console.print(
            f"[bold]Sequence[/bold]   {len(seq.accepted_ids)} headings accepted, "
            f"{seq.demoted} demoted, {len(seq.violations)} violations, {len(seq.gaps)} gaps"
        )
        for violation in seq.violations[:10]:
            console.print(f"             [red]violation[/red] {violation}")
        for gap in seq.gaps[:6]:
            console.print(f"             [yellow]gap[/yellow] {gap}")

    if show_candidates:
        table = Table(title="Header candidates", show_lines=False)
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("p", justify="right")
        table.add_column("score", justify="right")
        table.add_column("signals")
        table.add_column("text / rejection")
        for cand in report.candidates[:show_candidates]:
            signals = (
                ",".join(f"{k}={v:g}" for k, v in cand.signals.items())
                if cand.signals
                else "[dim]gated[/dim]"
            )
            detail = cand.rejected_reason or cand.line.text[:52]
            table.add_row(
                cand.section_id,
                str(cand.line.page_no),
                f"{cand.score:.2f}",
                signals,
                detail,
                style=None if cand.accepted else "dim",
            )
        console.print()
        console.print(table)

    for note in report.notes:
        console.print(f"\n[yellow]![/yellow] {note}")
