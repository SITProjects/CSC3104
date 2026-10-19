from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Node


async def ensure_node(session: AsyncSession, node_name: str) -> None:
    """
    Ensures that a node exists in the database.

    args:
        session: The database session for this request.
        node_name: The name of the node to ensure exists.
    """
    await session.execute(
        insert(Node)
        .values(name=node_name)
        .on_conflict_do_nothing(index_elements=[Node.name])
    )
