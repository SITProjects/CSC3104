"""A fake in-memory S3 and a callback recorder, shared by the tests."""

import io

import pymupdf
import pytest

from serverless.common import callback
from serverless.renderer import handler as renderer
from serverless.splitter import handler as splitter


class FakeS3:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.files[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.files[Key])}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    for module in (splitter, renderer):
        monkeypatch.setattr(module, "s3_client", lambda: fake)
    monkeypatch.delenv("RENDER_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("RENDER_DPI", "36")  # low DPI keeps the tests fast
    return fake


@pytest.fixture
def sent(monkeypatch):
    """Every callback body, in order."""
    bodies = []
    monkeypatch.setattr(callback, "post", bodies.append)
    return bodies


def make_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        doc.new_page().insert_text((72, 72), f"Page {n}", fontsize=24)
    data = doc.tobytes()
    doc.close()
    return data


def s3_event(key: str) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}
