import pytest

from serverless.common.errors import BadPdfError
from serverless.splitter.chunks import MAX_PAGES, make_chunks


def test_exact_multiple():
    assert make_chunks(40) == [(1, 20), (21, 40)]


def test_last_chunk_is_short():
    assert make_chunks(57) == [(1, 20), (21, 40), (41, 57)]


def test_single_page():
    assert make_chunks(1) == [(1, 1)]


def test_chunks_cover_every_page_once():
    pages = [p for start, end in make_chunks(199) for p in range(start, end + 1)]
    assert pages == list(range(1, 200))


def test_chunks_line_up_with_10_page_tasks():
    # A batch never splits a task, so a task can start once its batch arrives
    assert all((start - 1) % 10 == 0 for start, _ in make_chunks(200))


@pytest.mark.parametrize("bad", [0, MAX_PAGES + 1])
def test_rejects_bad_counts(bad):
    with pytest.raises(BadPdfError):
        make_chunks(bad)
