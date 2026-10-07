from collections.abc import AsyncGenerator

from sqlalchemy import AsyncAdaptedQueuePool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class SessionManager:
    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    def init_db(self, db_url: str) -> None:
        self.engine = create_async_engine(
            db_url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=True,
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
            autoflush=False,
        )

    async def close(self) -> None:
        if self.engine:
            await self.engine.dispose()

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        if not self.session_factory:
            raise RuntimeError("Session factory is not initialized.")
        async with self.session_factory() as session:
            yield session


session_manager = SessionManager()
