from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Replica, ReplicaStatus, Task


async def create_replicas(
    session: AsyncSession, tasks: list[Task], replicas_per_task: int
) -> list[Replica]:
    replicas = [
        Replica(task_id=task.id, attempt=1, status=ReplicaStatus.SCHEDULED)
        for task in tasks
        for _ in range(replicas_per_task)
    ]
    session.add_all(replicas)
    await session.flush()
    return replicas
