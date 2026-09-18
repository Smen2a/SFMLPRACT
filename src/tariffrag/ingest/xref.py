"""Cross-references, used here as a measure of parser quality.

Tariff prose cites itself constantly -- Sections 13-14 alone carry 836 inline
references. Resolving them against the section tree grades that tree against the
document's own internal claims rather than against our expectations of it: if a
large share of references point at sections we never detected, the tree is
wrong, and no amount of eyeballing headings would have revealed it.

Query-time expansion (pulling a referenced section in as extra context) is a
later concern. What matters now is the resolution rate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tariffrag.ingest.extract import CanonicalText
from tariffrag.models import CrossReference, Section

__all__ = ["XrefReport", "extract_xrefs", "resolution_rate"]

_DOTTED_ID = r"(?:[IVXLCDM]+(?:\.[A-Z0-9]+)+|\d+(?:\.\d+)+)"
_BARE_LETTER = r"[A-Z]"
"""Only Appendix/Attachment/Schedule take a bare letter.

Allowing it after "Section" matched things like "Section S" mid-sentence,
inventing references that do not exist.
"""

_REFERENCE = re.compile(
    rf"\b(?:"
    rf"(?P<skind>Sections?)\s+(?P<sbody>{_DOTTED_ID}(?:\s*(?:,|and|through|to)\s*{_DOTTED_ID})*)"
    rf"|"
    rf"(?P<akind>Appendix|Appendices|Attachment|Schedule)\s+"
    rf"(?P<abody>(?:{_DOTTED_ID}|{_BARE_LETTER})"
    rf"(?:\s*(?:,|and|through|to)\s*(?:{_DOTTED_ID}|{_BARE_LETTER}))*)"
    rf")"
)
"""Matches a citation and any list that follows it.

``Sections III.13.1 and III.13.2`` is one match carrying two ids, so the list
form is not silently counted as a single reference.
"""

_CONNECTOR = re.compile(r"\s*(?:,|and|through|to)\s*")
_APPENDIX_LETTER = re.compile(r"^[A-Z]$")


@dataclass(frozen=True, slots=True)
class XrefReport:
    doc_id: str
    references: tuple[CrossReference, ...]

    @property
    def resolved(self) -> int:
        return sum(1 for ref in self.references if ref.resolved)

    @property
    def rate(self) -> float:
        return self.resolved / len(self.references) if self.references else 1.0

    def unresolved_targets(self, limit: int = 10) -> list[str]:
        seen: list[str] = []
        for ref in self.references:
            if not ref.resolved and ref.raw_text not in seen:
                seen.append(ref.raw_text)
            if len(seen) >= limit:
                break
        return seen


def _section_at(sections: Sequence[Section], offset: int) -> Section | None:
    for section in sections:
        if section.char_start <= offset < section.char_end:
            return section
    return None


def extract_xrefs(
    doc_id: str,
    canonical: CanonicalText,
    sections: Sequence[Section],
    appendix_docs: Mapping[str, str] | None = None,
    corpus_sections: Mapping[str, set[str]] | None = None,
) -> XrefReport:
    """Find every cross-reference and resolve it against the section tree.

    Resolution is scoped to the containing document first -- a bare
    ``Section 4.2`` inside one manual means that manual's 4.2 -- then widened to
    the rest of the corpus, because Market Rule 1 is split across files and its
    sections cite each other freely. ``Appendix X`` references resolve when
    ``appendix_docs`` maps the letter to a doc id.
    """
    known = {s.section_id for s in sections if s.section_id}
    corpus_sections = corpus_sections or {}
    appendix_docs = appendix_docs or {}
    ordered = sorted(sections, key=lambda s: s.char_start)

    references: list[CrossReference] = []
    for match in _REFERENCE.finditer(canonical.text):
        kind_raw = match.group("skind") or match.group("akind")
        body = match.group("sbody") or match.group("abody")
        kind = kind_raw.lower()
        cursor = match.start("sbody" if match.group("sbody") else "abody")

        for piece in _CONNECTOR.split(body):
            target = piece.strip()
            if not target:
                continue
            start = canonical.text.find(target, cursor)
            if start < 0:
                start = match.start()
            cursor = start + len(target)

            owner = _section_at(ordered, match.start())
            to_doc: str | None = None
            to_section: str | None = None

            if target in known:
                to_doc, to_section = doc_id, target
            elif found := _find_in_corpus(target, corpus_sections, doc_id):
                # Market Rule 1 is split across files, so a great many references
                # legitimately point at another document in the same corpus.
                to_doc, to_section = found, target
            elif kind.startswith("appendi") and _APPENDIX_LETTER.match(target):
                mapped = appendix_docs.get(target)
                if mapped:
                    to_doc, to_section = mapped, ""

            references.append(
                CrossReference(
                    from_doc_id=doc_id,
                    from_section_id=owner.section_id if owner else "",
                    raw_text=f"{kind_raw} {target}",
                    char_start=start,
                    char_end=start + len(target),
                    to_doc_id=to_doc,
                    to_section_id=to_section,
                )
            )
    return XrefReport(doc_id=doc_id, references=tuple(references))


def _find_in_corpus(
    target: str, corpus_sections: Mapping[str, set[str]], exclude: str
) -> str | None:
    """Locate a section id in another document of the corpus."""
    for other_id, ids in corpus_sections.items():
        if other_id != exclude and target in ids:
            return other_id
    return None


def resolution_rate(reports: Sequence[XrefReport]) -> float:
    total = sum(len(r.references) for r in reports)
    resolved = sum(r.resolved for r in reports)
    return resolved / total if total else 1.0
