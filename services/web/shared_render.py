"""One render per page per moment, however many browsers are watching it."""

import asyncio
import time
from collections.abc import Callable, Coroutine, Hashable
from typing import Any


class SharedRender:
    """
    Every live stream polls the database (and the coordinator) to render its
    page. Streams that watch the same page ask here instead, so the work is done
    once per ttl seconds per page, not once per viewer.
    """

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._runs: dict[Hashable, tuple[float, asyncio.Task[Any]]] = {}

    async def get[T](
        self, key: Hashable, render: Callable[[], Coroutine[Any, Any, T]]
    ) -> T:
        now = time.monotonic()
        run = self._runs.get(key)
        if run is None or now - run[0] >= self.ttl:
            # Everything older than ttl is stale, which also keeps the dict small
            self._runs = {k: r for k, r in self._runs.items() if now - r[0] < self.ttl}
            task = asyncio.create_task(render())
            # A viewer that disconnects stops waiting; its error is not unhandled
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            run = self._runs[key] = (now, task)
        # shield: one viewer disconnecting must not cancel the render the others wait on
        return await asyncio.shield(run[1])
