"""
Shared authentication dependency for FastAPI.

Provides a reusable `require_auth` dependency that validates Bearer tokens
and returns a typed AuthContext. Every endpoint should use this instead
of manually checking the Authorization header.

Usage:
    from app.core.auth import require_auth, AuthContext

    @router.post("/mapping")
    async def get_mapping(auth: AuthContext = Depends(require_auth)):
        # auth.token is the validated token string
        # auth.auth_header is the full "Bearer xxx" string for forwarding
        pass
"""

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    """Validated authentication context for a request."""
    token: str
    auth_header: str


async def require_auth(request: Request) -> AuthContext:
    """
    FastAPI dependency that validates the Authorization header.

    Validates:
      1. Header is present
      2. Header starts with "Bearer "
      3. Token is non-empty
      4. Token has valid JWT structure (3 dot-separated parts)

    Raises HTTPException(401) on validation failure.
    Never falls back to empty strings.
    """
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        logger.warning(
            f"Missing Authorization header | path={request.url.path}"
        )
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    if not auth_header.startswith("Bearer "):
        logger.warning(
            f"Invalid auth scheme (expected Bearer) | path={request.url.path}"
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization scheme. Expected: Bearer <token>",
        )

    token = auth_header[7:].strip()  # Strip "Bearer "

    if not token:
        logger.warning(
            f"Empty Bearer token | path={request.url.path}"
        )
        raise HTTPException(
            status_code=401,
            detail="Empty Bearer token",
        )

    # Structural JWT validation: must have 3 dot-separated parts
    parts = token.split(".")
    if len(parts) != 3:
        logger.warning(
            f"Malformed JWT (expected 3 parts, got {len(parts)}) | path={request.url.path}"
        )
        raise HTTPException(
            status_code=401,
            detail="Malformed authentication token",
        )

    return AuthContext(token=token, auth_header=auth_header)
