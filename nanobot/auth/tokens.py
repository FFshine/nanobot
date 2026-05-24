"""JWT token creation and validation."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt

_jwt_secret: str | None = None
_jwt_algorithm = "HS256"


def get_jwt_secret() -> str:
    global _jwt_secret
    if _jwt_secret is None:
        _jwt_secret = secrets.token_urlsafe(32)
    return _jwt_secret


def set_jwt_secret(secret: str) -> None:
    global _jwt_secret
    _jwt_secret = secret


@dataclass
class TokenPayload:
    user_id: str
    username: str
    role: str


def create_token(
    user_id: str,
    username: str,
    role: str,
    *,
    expires_in: int = 86400,
) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + expires_in,
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=_jwt_algorithm)


def verify_token(token: str) -> TokenPayload | None:
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[_jwt_algorithm])
        return TokenPayload(
            user_id=payload["sub"],
            username=payload["username"],
            role=payload["role"],
        )
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
