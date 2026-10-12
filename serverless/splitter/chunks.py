"""Split a page count into render batches.

Pages are 1-based and page_end is inclusive, matching jobs/{job_id}/pages/{page:04d}.png.
"""

from serverless.common.errors import BadPdfError

PAGES_PER_RENDER = 20  # a multiple of the 10-page task size, so tasks never straddle
MAX_PAGES = 500  # stops one huge PDF from starting hundreds of Lambdas


def make_chunks(page_count: int, size: int = PAGES_PER_RENDER) -> list[tuple[int, int]]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    if page_count < 1:
        raise BadPdfError("PDF has no pages")
    if page_count > MAX_PAGES:
        raise BadPdfError(f"PDF has {page_count} pages, the limit is {MAX_PAGES}")
    return [
        (start, min(start + size - 1, page_count))
        for start in range(1, page_count + 1, size)
    ]
