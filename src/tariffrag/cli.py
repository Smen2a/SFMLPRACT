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

from tariffrag import __version__
from tariffrag.config import settings
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


if __name__ == "__main__":  # pragma: no cover
    app()
