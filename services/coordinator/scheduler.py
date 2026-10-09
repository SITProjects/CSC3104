import asyncio
import hashlib
import secrets
import traceback

from kubernetes.client import BatchV1Api
from kubernetes.client.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import BATCH_SIZE, JOB_NAMESPACE, SCHEDULER_INTERVAL
from shared.db.models import ReplicaStatus

from .crud import fetch_scheduled_replicas, mark_job_running, mark_task_running
from .k8s import build_replica_job_manifest


async def launch_batch(
    session_factory: async_sessionmaker[AsyncSession],
    coordinator_url: str,
    k3_api: BatchV1Api,
) -> int:
    launched = 0
    async with session_factory.begin() as session:
        scheduled_replicas = await fetch_scheduled_replicas(session, limit=BATCH_SIZE)
        for replica, task, job_type in scheduled_replicas:
            print(f"launching replica {replica.id} attempt {replica.attempt}")
            # the pod gets the plain token; only its hash is stored
            token = secrets.token_urlsafe(32)
            job_manifest = build_replica_job_manifest(
                replica, task, job_type, token, coordinator_url
            )
            try:
                # BatchV1Api is blocking, so keep it off the event loop
                await asyncio.to_thread(
                    k3_api.create_namespaced_job, JOB_NAMESPACE, job_manifest
                )
            except ApiException as e:
                if e.status == 409:
                    # left over from a launch whose commit failed; its token is lost,
                    # so delete it and relaunch on the next loop
                    await asyncio.to_thread(
                        k3_api.delete_namespaced_job,
                        job_manifest["metadata"]["name"],
                        JOB_NAMESPACE,
                        propagation_policy="Background",
                    )
                print(
                    f"Failed to create K8s job for replica {replica.id}: {e.status} {e.reason}"
                )
                continue
            replica.status = ReplicaStatus.RUNNING
            replica.token_hash = hashlib.sha256(token.encode()).hexdigest()
            replica.progress = 0
            await session.flush()
            await mark_task_running(session, task.id)
            await mark_job_running(session, task.job_id)
            launched += 1
            print(f"launched replica {replica.id} attempt {replica.attempt}")
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
            print("Scheduler transaction failed")
            traceback.print_exc()
        # wait for x seconds before checking for new scheduled replicas
        await asyncio.sleep(SCHEDULER_INTERVAL)
