import asyncio

import pytest

from services.web.shared_render import SharedRender


@pytest.mark.asyncio
async def test_viewers_of_one_page_share_a_render():
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return calls

    shared = SharedRender(ttl=5)
    results = await asyncio.gather(*(shared.get("job", render) for _ in range(5)))
    assert results == [1] * 5
    assert await shared.get("job", render) == 1  # still fresh
    assert calls == 1


@pytest.mark.asyncio
async def test_render_runs_again_once_stale():
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return calls

    shared = SharedRender(ttl=0)
    assert [await shared.get("job", render) for _ in range(3)] == [1, 2, 3]


@pytest.mark.asyncio
async def test_pages_are_kept_apart():
    async def render_a():
        return "a"

    async def render_b():
        return "b"

    shared = SharedRender(ttl=5)
    assert await shared.get("a", render_a) == "a"
    assert await shared.get("b", render_b) == "b"


@pytest.mark.asyncio
async def test_failure_reaches_every_viewer():
    async def render():
        await asyncio.sleep(0.01)
        raise ValueError("database down")

    shared = SharedRender(ttl=5)
    results = await asyncio.gather(
        *(shared.get("job", render) for _ in range(3)), return_exceptions=True
    )
    assert all(isinstance(result, ValueError) for result in results)


@pytest.mark.asyncio
async def test_one_viewer_leaving_does_not_cancel_the_render_for_the_others():
    async def render():
        await asyncio.sleep(0.05)
        return "html"

    shared = SharedRender(ttl=5)
    leaving = asyncio.create_task(shared.get("job", render))
    staying = asyncio.create_task(shared.get("job", render))
    await asyncio.sleep(0.01)
    leaving.cancel()
    assert await staying == "html"
