import asyncio
import logging
import secrets
from datetime import UTC, datetime

from kubernetes.client import BatchV1Api
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import BATCH_SIZE, SCHEDULER_INTERVAL
from services.coordinator.crud import (
    fetch_scheduled_replicas,
    mark_job_running,
    mark_task_running,
    out_of_retries,
    recover_replicas,
    schedule_replica,
    set_voting,
    sibling_nodes,
)
from services.coordinator.k8s import build_replica_job_manifest, create_job
from shared.db.models import ReplicaStatus

log = logging.getLogger("coordinator.scheduler")


async def launch_batch(
    session_factory: async_sessionmaker[AsyncSession],
    coordinator_url: str,
    k3_api: BatchV1Api,
) -> int:
    launched = 0
    async with session_factory.begin() as session:
        await recover_replicas(session)
    for _ in range(BATCH_SIZE):
        async with session_factory.begin() as session:
            scheduled_replicas = await fetch_scheduled_replicas(session, limit=1)
            if not scheduled_replicas:
                break
            replica, task, job_type = scheduled_replicas[0]
            log.info(f"launching replica {replica.id} attempt {replica.attempt}")
            # the pod gets the plain token; only its hash is stored
            token = secrets.token_urlsafe(32)
            job_manifest = build_replica_job_manifest(
                replica,
                task,
                job_type,
                token,
                coordinator_url,
                # other copies' nodes, so the vote is between different PCs,
                # and nodes where this copy found no working GPU
                avoid_nodes=sorted(
                    {*await sibling_nodes(session, replica), *replica.bad_nodes}
                ),
            )
            try:
                # BatchV1Api is blocking, so keep it off the event loop
                await create_job(job_manifest, k3_api)
            except Exception:  # noqa: BLE001
                log.error(f"Failed to create K8s job for replica {replica.id}")
                replica.failures += 1
                if out_of_retries(replica):
                    replica.status = ReplicaStatus.FAILED
                    replica.token_hash = None
                    replica.ended_at = datetime.now(UTC)
                    await session.flush()
                    await mark_task_running(session, task.id)
                    await mark_job_running(session, task.job_id)
                    await set_voting(session, task)
                else:
                    replica.attempt += 1
                break
            await schedule_replica(session, replica, token)
            await session.flush()
            await mark_task_running(session, task.id)
            await mark_job_running(session, task.job_id)
        launched += 1
        log.info(f"launched replica {replica.id} attempt {replica.attempt}")
    # only count successes, so repeated failures still wait SCHEDULER_INTERVAL
    return launched


async def scheduler_loop(
    session_factory: async_sessionmaker[AsyncSession],
    coordinator_url: str,
    k3_api: BatchV1Api,
) -> None:
    """
    Main scheduler loop that periodically checks for sheduled replicas, creates K8s jobs for them, and updates their status in the database.

    args:
        session_factory: async_sessionmaker[AsyncSession]: SQLAlchemy async session factory
        coordinator_url: str: URL of the coordinator service
    """
    while True:
        try:
            launched_replicas = await launch_batch(
                session_factory, coordinator_url, k3_api
            )
            if launched_replicas == BATCH_SIZE:
                # immediately check for more scheduled replicas if we launched a full batch
                continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.error("Scheduler transaction failed")
        # wait for x seconds before checking for new scheduled replicas
        await asyncio.sleep(SCHEDULER_INTERVAL)
