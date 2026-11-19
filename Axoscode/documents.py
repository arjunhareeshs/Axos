"""Document parser: PDF, Word and image files -> Markdown text.

Text keeps its order, tables become Markdown tables, and each embedded image
is described by a vision model (see llm.describe_image) so the agent can
"see" charts, diagrams and screenshots, and read scanned pages. Images that
cannot be described are still listed, with their size.
"""

import io
import tempfile
from pathlib import Path

from . import config, history

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIN_IMAGE_PX = 48  # smaller images are bullets, rules and logos

IMAGE_PROMPT = (
    "This image comes from a document that a coding agent is reading. "
    "Transcribe any text in it exactly. Describe what it shows: for a chart, "
    "the type, axes and the values or trend; for a diagram, the components "
    "and how they connect; for a screenshot, the interface and its content; "
    "for a table, its rows. Be factual and concise. Do not speculate."
)


# ---------------------------------------------------------------- helpers

def markdown_table(rows):
    """Rows of cells -> a Markdown table (first row is the header)."""
    rows = [
        [" ".join(str(cell or "").split()).replace("|", "\\|") for cell in row]
        for row in rows
        if any(cell for cell in row)
    ]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = [f"| {' | '.join(row)} |" for row in rows]
    lines.insert(1, f"| {' | '.join(['---'] * width)} |")
    return "\n".join(lines)


class Images:
    """Describes the images of one document, within the MAX_DOC_IMAGES budget."""

    def __init__(self):
        self.seen = 0
        self.skipped = 0

    def describe(self, data, label):
        """Markdown for one image: its description, or why there is none."""
        from PIL import Image

        try:
            picture = Image.open(io.BytesIO(data))
            picture.load()
        except Exception:  # noqa: BLE001 - unsupported or corrupt image data
            return f"*[{label}: image could not be decoded]*"

        width, height = picture.size
        if min(width, height) < MIN_IMAGE_PX:
            return ""
        if self.seen >= config.MAX_DOC_IMAGES:
            self.skipped += 1
            return ""
        self.seen += 1

        buffer = io.BytesIO()
        picture.convert("RGB" if picture.mode in ("CMYK", "P") else picture.mode).save(
            buffer, "PNG"
        )
        png = buffer.getvalue()

        # Kept for the turn, so the agent can open it again with read_file.
        handle = tempfile.NamedTemporaryFile(
            prefix="axos-img-", suffix=".png", delete=False
        )
        handle.write(png)
        handle.close()
        history.SPILLS.append(Path(handle.name))

        head = f"{label} ({width}x{height}, saved at {handle.name})"
        try:
            from .llm import describe_image

            return f"*[{head}]*\n{describe_image(png, IMAGE_PROMPT).strip()}"
        except Exception as failure:  # noqa: BLE001 - no key, no vision model, ...
            return f"*[{head}: not described - {type(failure).__name__}: {failure}]*"

    def footer(self):
        if not self.skipped:
            return ""
        return (
            f"\n\n*[{self.skipped} more image(s) not described: limit is "
            f"{config.MAX_DOC_IMAGES} per document (MAX_DOC_IMAGES)]*"
        )


# -------------------------------------------------------------------- PDF

def _page_markdown(page):
    """One pdfplumber page: text and tables, top to bottom."""
    width, height = page.width, page.height
    parts, top = [], 0

    def text_between(upper, lower):
        if lower - upper < 2:
            return
        region = page.crop((0, upper, width, min(lower, height)))
        text = (region.extract_text() or "").strip()
        if text:
            parts.append(text)

    for table in sorted(page.find_tables(), key=lambda t: t.bbox[1]):
        text_between(top, table.bbox[1])
        rendered = markdown_table(table.extract())
        if rendered:
            parts.append(rendered)
        top = table.bbox[3]
    text_between(top, height)
    return "\n\n".join(parts)


def pdf_markdown(path):
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("the PDF is password-protected")

    images, pages, found = Images(), [], False
    with pdfplumber.open(path) as pdf:
        for number, (page, source) in enumerate(zip(pdf.pages, reader.pages), start=1):
            body = _page_markdown(page)
            figures = []
            for index, image in enumerate(source.images, start=1):
                note = images.describe(image.data, f"page {number}, image {index}")
                if note:
                    figures.append(note)
            found = found or bool(body or figures)
            section = [f"--- page {number} ---", body, *figures]
            pages.append("\n\n".join(part for part in section if part))

    if not found:
        raise ValueError("the PDF has no text and no usable images")
    return "\n\n".join(pages) + images.footer()


# ------------------------------------------------------------------- Word

def docx_markdown(path):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(path)
    images, parts = Images(), []

    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            style = paragraph.style.name if paragraph.style is not None else ""
            if text and style.startswith("Heading"):
                level = style.split()[-1]
                text = f"{'#' * (int(level) if level.isdigit() else 1)} {text}"
            elif text and style.startswith("List"):
                text = f"- {text}"
            if text:
                parts.append(text)
            for blip in element.xpath(".//a:blip"):
                related = document.part.related_parts.get(blip.get(qn("r:embed")))
                if related is not None:
                    note = images.describe(related.blob, "image")
                    if note:
                        parts.append(note)
        elif element.tag == qn("w:tbl"):
            table = Table(element, document)
            rendered = markdown_table(
                [[cell.text for cell in row.cells] for row in table.rows]
            )
            if rendered:
                parts.append(rendered)

    if not parts:
        raise ValueError("the document has no readable content")
    return "\n\n".join(parts) + images.footer()


# ------------------------------------------------------------------ image

def image_markdown(path):
    images = Images()
    note = images.describe(Path(path).read_bytes(), Path(path).name)
    return note or f"*[{Path(path).name}: image is too small to describe]*"


READERS = {
    ".pdf": pdf_markdown,
    ".docx": docx_markdown,
    **dict.fromkeys(IMAGE_SUFFIXES, image_markdown),
}


def read(path):
    """File contents as text. PDF, Word and image files are parsed; anything
    else is read as UTF-8."""
    path = Path(path)
    if path.suffix.lower() == ".doc":
        raise ValueError("legacy .doc is not supported; save it as .docx first")
    reader = READERS.get(path.suffix.lower())
    return reader(path) if reader else path.read_text()
