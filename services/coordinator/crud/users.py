import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import SESSION_TTL_SECONDS
from shared.db import User, UserSession


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_user(session: AsyncSession, email: str, password_hash: str) -> User:
    user = User(id=uuid.uuid4(), email=email, password_hash=password_hash)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as e:
        raise HTTPException(409, "an account with this email already exists") from e
    return user


async def delete_expired_sessions(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    await session.execute(delete(UserSession).where(UserSession.expires_at <= now))


async def ensure_user_exists(session: AsyncSession, user_id: uuid.UUID) -> None:
    if await session.get(User, user_id) is None:
        raise HTTPException(422, f"unknown owner_id {user_id}")


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def create_session(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[str, datetime]:
    """
    Starts a login session and drops the user's expired ones.

    args:
        session: The database session for this request.
        user_id: The user logging in.
    returns:
        tuple[str, datetime]: The bearer token, shown to the user only now, and
            when it expires. Only its hash is stored.
    """
    now = datetime.now(UTC)
    await session.execute(
        delete(UserSession).where(
            UserSession.user_id == user_id, UserSession.expires_at <= now
        )
    )
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)
    session.add(
        UserSession(
            token_hash=hash_token(token), user_id=user_id, expires_at=expires_at
        )
    )
    await session.flush()
    return token, expires_at


async def get_session_user(session: AsyncSession, token: str) -> User | None:
    stmt = (
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == hash_token(token),
            UserSession.expires_at > datetime.now(UTC),
        )
    )
    return await session.scalar(stmt)


async def delete_session(session: AsyncSession, token: str) -> None:
    await session.execute(
        delete(UserSession).where(UserSession.token_hash == hash_token(token))
    )
