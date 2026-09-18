"""Generate synthetic tariff-like PDFs exercising one parser hazard each.

Real ISO documents cannot be fetched in every environment, and they also make
poor regression tests: they are large, they change under you, and they cannot be
committed. These fixtures pin the *detector's* behaviour against hazards we
construct deliberately.

They do not substitute for the real thing. The spike must still be run against
real ISO-NE and NYISO documents before the ingest pipeline is built -- these only
prove the detector reacts correctly when a hazard is present.

Run with::

    python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

FIXTURE_DIR = Path(__file__).parent
PAGE_W, PAGE_H = LETTER

HEADER_Y = PAGE_H - 40
FOOTER_Y = 30
BODY_TOP = PAGE_H - 80
BODY_BOTTOM = 60
LEADING = 16
LEFT_MARGIN = 72

BODY_FONT = "Helvetica"
BODY_SIZE = 10
HEADING_FONT = "Helvetica-Bold"
HEADING_SIZE = 13

Block = tuple[str, str]
"""``(kind, text)`` where kind is ``"heading"`` or ``"body"``."""


def _wrap(text: str, width: int = 88) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _render(
    path: Path,
    blocks: list[Block],
    *,
    running_header: str | None,
    distinct_headings: bool,
) -> None:
    """Lay blocks out across pages with a fixed-position header and footer."""
    pdf = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    page_no = 1
    y = BODY_TOP

    def start_page() -> None:
        if running_header:
            pdf.setFont(BODY_FONT, 9)
            pdf.drawString(LEFT_MARGIN, HEADER_Y, running_header)

    def end_page() -> None:
        pdf.setFont(BODY_FONT, 9)
        pdf.drawString(LEFT_MARGIN, FOOTER_Y, f"Page {page_no}")

    start_page()
    for kind, text in blocks:
        is_heading = kind == "heading"
        font = HEADING_FONT if (is_heading and distinct_headings) else BODY_FONT
        size = HEADING_SIZE if (is_heading and distinct_headings) else BODY_SIZE
        lines = [text] if is_heading else _wrap(text)

        for line in lines:
            if y < BODY_BOTTOM:
                end_page()
                pdf.showPage()
                page_no += 1
                y = BODY_TOP
                start_page()
            pdf.setFont(font, size)
            pdf.drawString(LEFT_MARGIN, y, line)
            y -= LEADING + (4 if is_heading else 0)
        if is_heading:
            y -= 4

    end_page()
    pdf.save()


_SENTENCE = (
    "The Market Participant shall submit the required documentation in accordance "
    "with the schedule established by the ISO, and shall be determined to have "
    "satisfied the applicable requirement upon written confirmation."
)
# Repeated so each fixture spans several pages: the running header/footer
# detector keys off text recurring at the same vertical position across pages,
# which a single-page fixture would satisfy trivially and not actually test.
_BODY = " ".join([_SENTENCE] * 6)

_NESTED: list[Block] = [
    ("heading", "III.13 Forward Capacity Market"),
    ("body", _BODY),
    ("heading", "III.13.1 Qualification Process"),
    ("body", _BODY),
    ("heading", "III.13.1.1 New Capacity Qualification"),
    ("body", _BODY),
    ("heading", "III.13.1.2 Existing Capacity Qualification"),
    ("body", _BODY),
    ("heading", "III.13.2 Forward Capacity Auction"),
    ("body", _BODY),
    ("heading", "III.14 Capacity Supply Obligation"),
    ("body", _BODY),
]

_XREF_BODY = (
    "A Capacity Supply Obligation, as defined in Section III.12.2, shall terminate "
    "upon the occurrence of an event described in Sections III.13.1 and III.13.2. "
    "Nothing in Section III.14 shall be construed to limit the foregoing, and the "
    "provisions of Appendix A shall continue to apply."
)

_LONG_XREF_BODY = " ".join([_XREF_BODY] * 4)

_XREF: list[Block] = [
    ("heading", "III.12 Definitions"),
    ("body", _LONG_XREF_BODY),
    ("heading", "III.12.2 Capacity Supply Obligation"),
    ("body", _LONG_XREF_BODY),
    ("heading", "III.13 Forward Capacity Market"),
    ("body", _LONG_XREF_BODY),
]


def _scanned(path: Path) -> None:
    """An image-only page: no text layer, therefore not citable."""
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(img)
    draw.text((80, 80), "III.13 Forward Capacity Market", fill="black")
    for i in range(12):
        draw.text((80, 140 + i * 30), _SENTENCE[:70], fill="black")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    pdf = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    pdf.drawImage(ImageReader(buf), 0, 0, width=PAGE_W, height=PAGE_H)
    pdf.showPage()
    pdf.save()


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    _render(
        FIXTURE_DIR / "tariff_nested.pdf",
        _NESTED,
        running_header="ISO New England Manual M-20    Revision 27",
        distinct_headings=True,
    )
    _render(
        FIXTURE_DIR / "tariff_xref_trap.pdf",
        _XREF,
        running_header="ISO New England Market Rule 1    Section III",
        distinct_headings=True,
    )
    _render(
        FIXTURE_DIR / "tariff_flat.pdf",
        _NESTED,
        running_header="NYISO Installed Capacity Manual",
        distinct_headings=False,
    )
    _scanned(FIXTURE_DIR / "tariff_scanned.pdf")

    for pdf_path in sorted(FIXTURE_DIR.glob("*.pdf")):
        print(f"wrote {pdf_path.name} ({pdf_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
