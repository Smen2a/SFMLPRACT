"""Grounded question answering over ISO-NE and NYISO tariffs and market manuals."""

from __future__ import annotations

__version__ = "0.1.0"

CHUNKER_VERSION = "0"
"""Bumped whenever chunk boundaries change.

Stored in the manifest alongside the canonical-text hash; an index whose hashes
disagree with the chunks built from them is refused rather than served, so a
library upgrade that silently changes extraction becomes a hard failure instead
of a subtle citation bug.
"""
