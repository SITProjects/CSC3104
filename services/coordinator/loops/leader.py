import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

from kubernetes.client import (
    ApiException,
    BatchV1Api,
    CoordinationV1Api,
    CoreV1Api,
    V1Lease,
    V1LeaseSpec,
    V1ObjectMeta,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import (
    LEASE_NAME,
    LEASE_NAMESPACE,
    LEASE_SECONDS,
    RENEW_DEADLINE,
    RENEW_INTERVAL,
)

from .housekeeping import housekeeping_loop
from .pod_watcher import watch_pods

log = logging.getLogger("coordinator.leader")


class LeaseLock:
    def __init__(self, api: CoordinationV1Api, identity: str) -> None:
        self.api = api
        self.identity = identity
        # Expiry is timed on this Pod's monotonic clock from when the Lease last
        # changed, not from renew_time, so clock skew between nodes can't cause
        # an early takeover (same approach as client-go).
        self.observed: tuple[str | None, datetime | None] | None = None
        self.observed_at = 0.0

    async def try_acquire_or_renew(self) -> bool:
        now = datetime.now(UTC)
        try:
            lease = cast(
                V1Lease,
                await asyncio.to_thread(
                    self.api.read_namespaced_lease, LEASE_NAME, LEASE_NAMESPACE
                ),
            )
        except ApiException as e:
            if e.status != 404:
                raise
            return await self._create_lease(now)
        spec = lease.spec or V1LeaseSpec()
        record = (spec.holder_identity, spec.renew_time)
        if record != self.observed:
            self.observed = record
            self.observed_at = time.monotonic()
        duration = spec.lease_duration_seconds or LEASE_SECONDS
        expired = time.monotonic() - self.observed_at > duration
        if spec.holder_identity != self.identity:
            if spec.holder_identity and not expired:
                return False
            spec.holder_identity = self.identity
            spec.acquire_time = now
            spec.lease_transitions = (spec.lease_transitions or 0) + 1
        spec.renew_time = now
        spec.lease_duration_seconds = LEASE_SECONDS
        lease.spec = spec
        try:
            await asyncio.to_thread(
                self.api.replace_namespaced_lease, LEASE_NAME, LEASE_NAMESPACE, lease
            )
        except ApiException as e:
            if e.status == 409:
                return False
            raise
        return True

    async def _create_lease(self, now: datetime) -> bool:
        lease = V1Lease(
            metadata=V1ObjectMeta(name=LEASE_NAME, namespace=LEASE_NAMESPACE),
            spec=V1LeaseSpec(
                holder_identity=self.identity,
                acquire_time=now,
                renew_time=now,
                lease_duration_seconds=LEASE_SECONDS,
                lease_transitions=0,
            ),
        )
        try:
            await asyncio.to_thread(
                self.api.create_namespaced_lease, LEASE_NAMESPACE, lease
            )
        except ApiException as e:
            if e.status == 409:
                return False
            raise
        return True

    async def release(self) -> None:
        """Gives the Lease up on shutdown, so the other Pod takes over at once."""
        try:
            lease = cast(
                V1Lease,
                await asyncio.to_thread(
                    self.api.read_namespaced_lease, LEASE_NAME, LEASE_NAMESPACE
                ),
            )
            if not lease.spec or lease.spec.holder_identity != self.identity:
                return
            lease.spec.holder_identity = None
            await asyncio.to_thread(
                self.api.replace_namespaced_lease, LEASE_NAME, LEASE_NAMESPACE, lease
            )
        except ApiException:
            log.warning("could not release the leader Lease", exc_info=True)


class LeaderLoop:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        batch_api: BatchV1Api,
        core_api: CoreV1Api,
    ) -> None:
        self.session_factory = session_factory
        self.batch_api = batch_api
        self.core_api = core_api
        self.housekeeping: asyncio.Task | None = None
        self.watcher: threading.Thread | None = None
        self.stop_watcher = threading.Event()

    async def start(self) -> None:
        self.stop_watcher = threading.Event()
        self.watcher = threading.Thread(
            target=watch_after,
            args=(
                self.watcher,
                asyncio.get_running_loop(),
                self.session_factory,
                self.core_api,
                self.stop_watcher,
            ),
            name="pod_watcher",
            daemon=True,
        )
        self.watcher.start()
        self.housekeeping = asyncio.create_task(
            housekeeping_loop(self.session_factory, self.batch_api, self.core_api)
        )

    async def stop(self) -> None:
        self.stop_watcher.set()
        if self.housekeeping:
            self.housekeeping.cancel()
            try:
                await self.housekeeping
            except asyncio.CancelledError:
                pass
            self.housekeeping = None
        if self.watcher:
            # The watch stream only checks the stop flag between events, so the
            # thread can outlive this join; start() waits for it via watch_after.
            await asyncio.to_thread(self.watcher.join, 2)


def watch_after(previous: threading.Thread | None, *args) -> None:
    """Runs the pod watcher once the previous leader term's watcher has exited."""
    if previous:
        previous.join()
    watch_pods(*args)


async def leader_loop(
    lock: LeaseLock,
    on_start: Callable[[], Awaitable[None]],
    on_stop: Callable[[], Awaitable[None]],
) -> None:
    leader = False
    last_renewed = 0.0
    try:
        while True:
            try:
                holding = await asyncio.wait_for(
                    lock.try_acquire_or_renew(), timeout=RENEW_DEADLINE
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("failed to acquire/renew leader Lease", exc_info=True)
                holding = None
            now = time.monotonic()
            if holding:
                last_renewed = now
                if not leader:
                    try:
                        await on_start()
                    except Exception:
                        # Give the Lease up so the other replica can lead.
                        log.exception("failed to start leader work")
                        await stop_quietly(on_stop)
                        await lock.release()
                    else:
                        leader = True
                        log.info("acquired leader Lease")
            elif leader and (holding is False or now - last_renewed > RENEW_DEADLINE):
                leader = False
                log.info("lost leader Lease")
                await stop_quietly(on_stop)
            await asyncio.sleep(RENEW_INTERVAL)
    finally:
        if leader:
            await stop_quietly(on_stop)
            await lock.release()


async def stop_quietly(on_stop: Callable[[], Awaitable[None]]) -> None:
    try:
        await on_stop()
    except Exception:
        log.exception("failed to stop leader work")
