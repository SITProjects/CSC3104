import asyncio
import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import BATCH_SIZE, SCHEDULER_INTERVAL
from shared.db.models import ReplicaStatus

from .crud import fetch_scheduled_replicas, mark_job_running, mark_task_running


async def launch_batch(
    session_factory: async_sessionmaker[AsyncSession], coordinator_url
) -> int:
    async with session_factory.begin() as session:
        scheduled_replicas = await fetch_scheduled_replicas(session, limit=BATCH_SIZE)
        for replica, job_id in scheduled_replicas:
            token = secrets.token_urlsafe(32)
            hashlib.sha256(token.encode()).hexdigest()
            # TODO: K8s jobs in idlecloud-job
            # Create K8 jobs if successful, update replica status to RUNNING and store token in db

            replica.status = ReplicaStatus.RUNNING
            replica.token_hash = token
            replica.progress = 0
            replica.attempt += 1
            await session.flush()
            await mark_task_running(session, replica.task_id)
            await mark_job_running(session, job_id)
            print(f"launched replica {replica.id} attempt {replica.attempt}")
        return len(scheduled_replicas)


async def scheduler_loop(
    session_factory: async_sessionmaker[AsyncSession], coordinator_url: str
) -> None:
    """
    Main scheduler loop that periodically checks for sheduled replicas, creates K8s jobs for them, and updates their status in the database.

    args:
        session_factory: async_sessionmaker[AsyncSession]: SQLAlchemy async session factory
        coordinator_url: str: URL of the coordinator service
    """
    while True:
        try:
            launched_replicas = await launch_batch(session_factory, coordinator_url)
            if launched_replicas == BATCH_SIZE:
                # immediately check for more scheduled replicas if we launched a full batch
                continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            print("Scheduler transaction failed")
        # wait for x seconds before checking for new scheduled replicas
        await asyncio.sleep(SCHEDULER_INTERVAL)
