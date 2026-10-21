import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.crud import get_session_user
from shared.db import User, UserRole, session_manager

# scrypt cost: 2**14 * 8 * 128 bytes = 16 MiB and about 50 ms per hash
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32
    )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = base64.b64encode
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


# checked against when the email is unknown, so a login takes as long either way
# and doesn't reveal which emails have accounts
_DUMMY_HASH = _hash_password(secrets.token_urlsafe(16))


async def hash_password(password: str) -> str:
    """
    Hashes a password with scrypt off the event loop.

    returns:
        str: "scrypt$n$r$p$salt$hash", salt and hash in base64.
    """
    return await asyncio.to_thread(_hash_password, password)


async def verify_password(password: str, stored: str | None) -> bool:
    """
    Checks a password against a stored hash off the event loop.

    args:
        password: The password the user typed.
        stored: The user's password_hash, or None when there is no such user.
    returns:
        bool: True only if a user exists and the password matches.
    """
    ok = await asyncio.to_thread(_verify_password, password, stored or _DUMMY_HASH)
    return ok and stored is not None


def bearer_token(authorization: str | None) -> str:
    """
    raises:
        HTTPException: 401 if the Authorization header has no bearer token.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            401, "not signed in", headers={"WWW-Authenticate": "Bearer"}
        )
    return token.strip()


async def current_user(
    session: Annotated[AsyncSession, Depends(session_manager.get_session)],
    authorization: str | None = Header(None),
) -> User:
    """
    Finds the signed-in user from the request's bearer token.

    Shares the request's database session with the route, and commits the
    lookup so the route can still start its own transaction.

    returns:
        User: The user the token belongs to.
    raises:
        HTTPException: 401 if the token is missing, unknown or expired.
    """
    user = await get_session_user(session, bearer_token(authorization))
    await session.commit()
    if user is None:
        raise HTTPException(
            401, "not signed in", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    """
    returns:
        User: The signed-in user, who is an admin.
    raises:
        HTTPException: 401 if not signed in, 403 if not an admin.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "admins only")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
