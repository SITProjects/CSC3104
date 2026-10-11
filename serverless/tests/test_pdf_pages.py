import pytest

from serverless.common.errors import BadPdfError
from serverless.common.pdf_pages import count_pages, render_pages
from serverless.tests.conftest import make_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def sample_pdf():
    return make_pdf(5)


def test_count_pages(sample_pdf):
    assert count_pages(sample_pdf) == 5


def test_render_range_gives_pngs_in_order(sample_pdf):
    pages = list(render_pages(sample_pdf, 2, 4, dpi=72))
    assert [n for n, _ in pages] == [2, 3, 4]
    assert all(png.startswith(PNG_MAGIC) for _, png in pages)


def test_render_is_deterministic(sample_pdf):
    # Voting relies on every replica seeing the exact same image
    first = dict(render_pages(sample_pdf, 1, 1, dpi=72))
    assert first == dict(render_pages(sample_pdf, 1, 1, dpi=72))


def test_bad_range_raises(sample_pdf):
    with pytest.raises(ValueError):
        list(render_pages(sample_pdf, 4, 9, dpi=72))


def test_not_a_pdf():
    with pytest.raises(BadPdfError):
        count_pages(b"hello")
