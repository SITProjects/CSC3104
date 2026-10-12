"""PDF helpers built on PyMuPDF. No AWS code here, so it is easy to unit test.

PDFs are opened from bytes in memory, not from a file on disk. That avoids
temporary files, which behave differently on Lambda (/tmp only) and on Windows
(an open file cannot be deleted).
"""

from collections.abc import Iterator

import pymupdf

from serverless.common.errors import BadPdfError

RENDER_DPI = 300  # Tesseract works best at around 300 DPI


def _open(pdf: bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
        raise BadPdfError("file is not a readable PDF") from error
    if doc.needs_pass:
        doc.close()
        raise BadPdfError("PDF is password protected")
    return doc


def count_pages(pdf: bytes) -> int:
    with _open(pdf) as doc:
        return doc.page_count


def render_pages(
    pdf: bytes, page_start: int, page_end: int, dpi: int = RENDER_DPI
) -> Iterator[tuple[int, bytes]]:
    """Yield (page_number, png_bytes) for pages page_start..page_end (1-based, inclusive).

    Grayscale keeps the PNGs small. All 3 replicas of a task OCR the same PNG,
    so each page is rendered once and the vote compares like with like.
    """
    with _open(pdf) as doc:
        if not 1 <= page_start <= page_end <= doc.page_count:
            raise ValueError(
                f"bad page range {page_start}-{page_end} for {doc.page_count} pages"
            )
        for number in range(page_start, page_end + 1):
            pix = doc[number - 1].get_pixmap(
                dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False
            )
            yield number, pix.tobytes("png")
