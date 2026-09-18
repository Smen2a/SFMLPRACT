"""Command-line entry point.

Subcommands are added phase by phase; the pipeline they drive lives in
``retrieve.pipeline`` and ``answer``, which are the single implementations the
evaluation harness also consumes. If the eval ever re-implements retrieval it is
measuring a different system -- the most common way a RAG eval lies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from tariffrag import __version__
from tariffrag.config import settings
from tariffrag.ingest import manifest as manifest_mod
from tariffrag.ingest import pipeline as pipeline_mod
from tariffrag.ingest.spike import Verdict, render_report, spike_document

app = typer.Typer(
    name="tariffrag",
    help="Grounded Q&A over ISO-NE and NYISO tariffs and market manuals.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"tariffrag {__version__}")


@app.command()
def config() -> None:
    """Show resolved paths and model selections."""
    console.print(f"[bold]index[/bold]      {settings.index_path}")
    console.print(f"[bold]manifest[/bold]   {settings.manifest_path}")
    console.print(f"[bold]answer[/bold]     {settings.answer_model}")
    console.print(f"[bold]embeddings[/bold] {settings.embedding_model}")


@app.command()
def spike(
    pdfs: Annotated[list[Path], typer.Argument(help="PDF files to diagnose.")],
    show_candidates: Annotated[
        int, typer.Option("--show-candidates", "-c", help="Print the first N header candidates.")
    ] = 0,
) -> None:
    """Measure whether a PDF's section structure is recoverable.

    Run this before building the ingest pipeline. It reports, per document, a
    GO / DEGRADED / NO_GO verdict: whether there is a usable text layer, whether
    running furniture is strippable, whether headings are typographically
    distinct, whether section ids form a monotone sequence, and -- the number
    that matters -- how many wrapped cross-references were misread as headings.

    Exits non-zero if any document is NO_GO, so it can gate a pipeline run.
    """
    worst_is_blocking = False
    for pdf_path in pdfs:
        if not pdf_path.exists():
            console.print(f"[red]not found:[/red] {pdf_path}")
            raise typer.Exit(code=2)
        report = spike_document(pdf_path)
        render_report(report, console, show_candidates=show_candidates)
        worst_is_blocking |= report.verdict is Verdict.NO_GO

    console.print()
    if worst_is_blocking:
        console.print(
            "[bold red]At least one document is NO_GO.[/bold red] Drop it from the corpus."
        )
        raise typer.Exit(code=1)


manifest_app = typer.Typer(
    help="Build and inspect the corpus manifest.",
    no_args_is_help=True,
)
app.add_typer(manifest_app, name="manifest")


@manifest_app.command("build")
def manifest_build() -> None:
    """Probe every PDF under corpus/ and write corpus/manifest.yaml.

    Carries over `retrieved_at` for documents whose content hash is unchanged, so
    rebuilding does not churn the file.
    """
    previous = (
        manifest_mod.load(settings.manifest_path) if settings.manifest_path.exists() else None
    )
    built = manifest_mod.build_manifest(settings.corpus_dir, settings.repo_root, previous)
    manifest_mod.save(built, settings.manifest_path)

    active = sum(1 for d in built.documents if d.is_ingestable)
    console.print(
        f"Wrote [bold]{settings.manifest_path.relative_to(settings.repo_root)}[/bold]: "
        f"{len(built.documents)} documents, {active} ingestable."
    )


@manifest_app.command("show")
def manifest_show() -> None:
    """Print the manifest as a table."""
    if not settings.manifest_path.exists():
        console.print("[red]No manifest.[/red] Run `tariffrag manifest build` first.")
        raise typer.Exit(code=2)

    built = manifest_mod.load(settings.manifest_path)
    table = Table(title=f"Corpus manifest ({len(built.documents)} documents)")
    table.add_column("doc id", style="cyan", no_wrap=True)
    table.add_column("title", max_width=42)
    table.add_column("status")
    table.add_column("pp", justify="right")
    table.add_column("effective", max_width=30)
    table.add_column("sha", style="dim", no_wrap=True)

    status_style = {"active": "green", "reserved": "dim", "excluded": "red"}
    for doc in built.documents:
        eff = doc.primary_effective_date
        if eff:
            shown = eff.date.isoformat() if eff.date else eff.date_text
            effective = f"{shown}  {eff.docket}"
        else:
            effective = "[dim]none recorded[/dim]"
        style = status_style.get(doc.status.value, "")
        table.add_row(
            doc.doc_id,
            doc.title,
            f"[{style}]{doc.status.value}[/{style}]" if style else doc.status.value,
            str(doc.page_count),
            effective,
            doc.source.sha256_pdf[:10],
            style=None if doc.is_ingestable else "dim",
        )
    console.print(table)


@manifest_app.command("diff")
def manifest_diff() -> None:
    """Compare the corpus on disk against the manifest, by content hash.

    Exits non-zero on drift, so CI catches a corpus that no longer matches its
    record.
    """
    if not settings.manifest_path.exists():
        console.print("[red]No manifest.[/red] Run `tariffrag manifest build` first.")
        raise typer.Exit(code=2)

    built = manifest_mod.load(settings.manifest_path)
    delta = manifest_mod.diff_manifest(built, settings.corpus_dir, settings.repo_root)

    if delta.clean:
        console.print(f"[green]Clean[/green] — {len(built.documents)} documents match.")
        return

    for doc_id in delta.added:
        console.print(f"  [green]added[/green]    {doc_id}")
    for doc_id in delta.removed:
        console.print(f"  [red]removed[/red]  {doc_id}")
    for doc_id in delta.changed:
        console.print(f"  [yellow]changed[/yellow]  {doc_id}")
    console.print("\nRun `tariffrag manifest build` to update the record.")
    raise typer.Exit(code=1)


@app.command()
def ingest() -> None:
    """Extract canonical text, section trees and cross-references.

    Skips reserved and excluded documents by reading their status from the
    manifest rather than any hard-coded list.
    """
    if not settings.manifest_path.exists():
        console.print("[red]No manifest.[/red] Run `tariffrag manifest build` first.")
        raise typer.Exit(code=2)

    built = manifest_mod.load(settings.manifest_path)
    records = pipeline_mod.ingest_corpus(built, settings.repo_root, settings.text_dir)

    table = Table(title=f"Ingested {len(records)} documents")
    table.add_column("doc id", style="cyan", no_wrap=True)
    table.add_column("pages", justify="right")
    table.add_column("sections", justify="right")
    table.add_column("coverage", justify="right")
    table.add_column("xrefs", justify="right")
    table.add_column("resolved", justify="right")

    for record in records:
        pages = f"{record.pages_kept}/{record.page_count}"
        coverage = f"{record.coverage:.1%}"
        table.add_row(
            record.doc_id,
            pages,
            str(record.section_count),
            f"[green]{coverage}[/green]"
            if record.coverage >= 0.95
            else f"[yellow]{coverage}[/yellow]",
            str(record.xref_count),
            f"{record.xref_rate:.0%}",
        )
    console.print(table)

    total_refs = sum(r.xref_count for r in records)
    resolved = sum(r.xref_resolved for r in records)
    worst = min((r.coverage for r in records), default=1.0)
    console.print(
        f"\nCross-references resolved corpus-wide: [bold]{resolved}/{total_refs}"
        f"[/bold] ({resolved / total_refs:.1%}); lowest coverage {worst:.1%}."
    )


@app.command()
def outline(
    doc_id: Annotated[str, typer.Argument(help="Document id, e.g. isone:mr1:sec_13_14")],
    max_depth: Annotated[int, typer.Option("--max-depth", "-d")] = 4,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 60,
) -> None:
    """Print a document's section tree with page spans."""
    try:
        sections = pipeline_mod.load_sections(doc_id, settings.text_dir)
    except FileNotFoundError:
        console.print(f"[red]Not ingested:[/red] {doc_id}. Run `tariffrag ingest` first.")
        raise typer.Exit(code=2) from None

    shown = 0
    for section in sections:
        if section.depth > max_depth:
            continue
        if shown >= limit:
            console.print(f"[dim]... {len(sections) - shown} more sections[/dim]")
            break
        indent = "  " * max(0, section.depth - 1)
        pages = (
            f"p{section.page_start}"
            if section.page_start == section.page_end
            else f"p{section.page_start}-{section.page_end}"
        )
        label = section.section_id or "(front matter)"
        console.print(f"{indent}[cyan]{label}[/cyan]  {section.heading}  [dim]{pages}[/dim]")
        shown += 1


if __name__ == "__main__":  # pragma: no cover
    app()
