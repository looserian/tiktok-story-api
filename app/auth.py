"""
auth.py - Bearer token authentication dependency for FastAPI.
"""

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

# FastAPI security scheme — expects "Authorization: Bearer <token>"
_bearer_scheme = HTTPBearer(auto_error=True)


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_bearer_scheme),
) -> str:
    """
    FastAPI dependency that validates the Bearer token against the
    configured API_KEY environment variable.

    Raises HTTP 401 if the token is missing or invalid.
    Returns the validated token string on success.
    """
    if credentials.credentials != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
