from collections.abc import AsyncGenerator

from sqlalchemy import AsyncAdaptedQueuePool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.settings import DEBUG_ECHO, MAX_OVERFLOW, POOL_SIZE


class SessionManager:
    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def init_db(self, db_url: str) -> None:
        self.engine = create_async_engine(
            db_url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=DEBUG_ECHO,
        )
        self._session_factory = async_sessionmaker(
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

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if not self._session_factory:
            raise RuntimeError("Session factory is not initialized.")
        return self._session_factory


session_manager = SessionManager()
