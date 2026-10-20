from uuid import uuid4

import pytest

from services.web import local_split
from services.web.coordinator import CoordinatorError


class FakeSessionFactory:
    """Stands in for the session factory; pages_ready is faked, so no session is used."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def reported(monkeypatch):
    payloads = []

    async def pages_ready(session, coordinator, payload):
        payloads.append(payload)

    monkeypatch.setattr(local_split, "pages_ready", pages_ready)
    return payloads


@pytest.mark.asyncio
async def test_a_crash_fails_the_upload(monkeypatch, reported):
    def boom(pdf):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(local_split, "count_pages", boom)
    job_id = uuid4()
    await local_split.split_locally(FakeSessionFactory(), None, job_id, b"%PDF-")
    assert [p["status"] for p in reported] == ["failed"]
    assert reported[0]["job_id"] == str(job_id)


@pytest.mark.asyncio
async def test_a_failing_pages_ready_does_not_escape_or_hang_the_job(monkeypatch):
    calls = []

    async def pages_ready(session, coordinator, payload):
        calls.append(payload["status"])
        raise CoordinatorError(503, "coordinator unreachable")

    monkeypatch.setattr(local_split, "pages_ready", pages_ready)
    monkeypatch.setattr(local_split, "count_pages", lambda pdf: 5)
    monkeypatch.setattr(local_split, "_render_batch", lambda *args: None)
    # Does not raise, and tries to mark the upload failed after the batch report fails
    await local_split.split_locally(FakeSessionFactory(), None, uuid4(), b"%PDF-")
    assert calls == ["ok", "failed"]


@pytest.mark.asyncio
async def test_a_normal_split_reports_every_batch(monkeypatch, reported):
    monkeypatch.setattr(local_split, "count_pages", lambda pdf: 25)
    monkeypatch.setattr(
        local_split, "make_chunks", lambda count: [(1, 10), (11, 20), (21, 25)]
    )
    monkeypatch.setattr(local_split, "_render_batch", lambda *args: None)
    await local_split.split_locally(FakeSessionFactory(), None, uuid4(), b"%PDF-")
    assert [(p["page_start"], p["page_end"]) for p in reported] == [
        (1, 10),
        (11, 20),
        (21, 25),
    ]
    assert {p["status"] for p in reported} == {"ok"}
