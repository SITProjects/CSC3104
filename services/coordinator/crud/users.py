import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import SESSION_TTL_SECONDS
from shared.db import Job, User, UserSession


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


def _users_with_job_count() -> Select:
    """Each user with the number of jobs they own, newest account first."""
    return (
        select(User, func.count(Job.id))
        .outerjoin(Job, Job.owner_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.desc(), User.id)
    )


async def list_users(
    session: AsyncSession, limit: int, offset: int
) -> list[tuple[User, int]]:
    """
    returns:
        list: (user, job count) pairs, newest account first.
    """
    rows = await session.execute(_users_with_job_count().limit(limit).offset(offset))
    return [(user, count) for user, count in rows.all()]


async def get_user_with_job_count(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[User, int] | None:
    row = (
        await session.execute(_users_with_job_count().where(User.id == user_id))
    ).first()
    return (row[0], row[1]) if row else None


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
