"""Command-line entry point.

Subcommands are added phase by phase; the pipeline they drive lives in
``retrieve.pipeline`` and ``answer``, which are the single implementations the
evaluation harness also consumes. If the eval ever re-implements retrieval it is
measuring a different system -- the most common way a RAG eval lies.
"""

from __future__ import annotations

import typer
from rich.console import Console

from tariffrag import __version__
from tariffrag.config import settings

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


if __name__ == "__main__":  # pragma: no cover
    app()
