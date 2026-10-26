import asyncio
import logging
import os
import signal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.session import SessionManager

from .adaptive import adaptive_loop
from .controller import verify_next_task
from .k8s import trust_sync_loop
from .output import output_loop

log = logging.getLogger("verifier")
VERIFIER_INTERVAL = 5  # seconds between checks when idle or after an error


async def verifier_loop(
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = VERIFIER_INTERVAL,
) -> None:
    """
    Repeatedly processes tasks ready for voting until the service is stopped.

    Completed transactions are followed by another check immediately. When no
    task is ready, or a transaction fails, wait before trying again. Cancellation
    is allowed through so shutdown can close the database connection pool.

    args:
        session_factory: The verifier's async database session factory.
        interval: Seconds to wait when idle or recovering from an error.
    """
    while True:
        try:
            task_id = await verify_next_task(session_factory)
            if task_id is not None:
                log.info("Processed voting task %s", task_id)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Voting transaction failed")
        await asyncio.sleep(interval)


async def run_verifier() -> None:
    """
    Runs voting, output assembly, adaptive checks and trust sync, and closes the
    pool on shutdown.

    Reads DATABASE_URL using the coordinator's database URL convention. The
    coordinator owns migrations; this service uses the existing tables. SIGTERM
    cancels the running task so normal container shutdown also closes the pool.
    Set ADAPTIVE_REPLICATION_ENABLED=false to collect all copies for evaluation.

    raises:
        RuntimeError: If DATABASE_URL has not been configured.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set to run the verifier")

    manager = SessionManager()
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Verifier must be run in an asyncio task")
    try:
        manager.init_db(database_url)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            log.info("Verifier started")
            background_tasks = [
                asyncio.create_task(trust_sync_loop(manager.session_factory)),
                asyncio.create_task(output_loop(manager.session_factory)),
            ]
            if os.environ.get(
                "ADAPTIVE_REPLICATION_ENABLED", "true"
            ).strip().lower() not in {"false", "0", "no", "off"}:
                background_tasks.append(
                    asyncio.create_task(adaptive_loop(manager.session_factory))
                )
            try:
                await verifier_loop(manager.session_factory)
            finally:
                for background_task in background_tasks:
                    background_task.cancel()
                await asyncio.gather(*background_tasks, return_exceptions=True)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    finally:
        await manager.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_verifier())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Verifier stopped")
