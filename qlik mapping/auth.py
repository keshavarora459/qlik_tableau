import logging
from contextvars import ContextVar
try:
    from fastapi import Request, HTTPException
except ImportError:
    class Request: pass
    class HTTPException(Exception): pass

try:
    from jwt import PyJWKClient, decode as jwt_decode, ExpiredSignatureError, InvalidTokenError
except ImportError:
    PyJWKClient = None
    jwt_decode = None
    class ExpiredSignatureError(Exception): pass
    class InvalidTokenError(Exception): pass
from config import ENABLE_AUTH, JWKS_URL, EXPECTED_AUDIENCE, ISSUER

logger = logging.getLogger(__name__)
request_token_ctx: ContextVar = ContextVar("request_token_ctx", default=None)

_jwk_client = PyJWKClient(JWKS_URL) if JWKS_URL else None

async def validate_request_bearer_token(request: Request) -> dict:
    """Validate incoming Azure AD JWT bearer token."""
    if not ENABLE_AUTH:
        logger.warning("ENABLE_AUTH=false - bypassing bearer token validation")
        request_token_ctx.set(None)
        return {"sub": "local-testing", "auth_bypassed": True}

    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization bearer token")

    token = auth.split(" ", 1)[1].strip()
    request_token_ctx.set(token)

    if not _jwk_client:
        raise HTTPException(status_code=500, detail="JWKS client not initialized (missing TENANT_ID)")

    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token).key
    except Exception as e:
        logger.exception("Failed to fetch signing key: %s", str(e))
        raise HTTPException(status_code=401, detail="Invalid token (no JWKS key)")

    try:
        payload = jwt_decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=EXPECTED_AUDIENCE,
            issuer=ISSUER,
            options={"verify_aud": True, "verify_exp": True}
        )
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except InvalidTokenError as e:
        logger.exception("Invalid token: %s", str(e))
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    request.state.user = payload
    return payload
