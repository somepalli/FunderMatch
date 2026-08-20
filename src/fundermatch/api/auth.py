"""JWT validation that derives reviewer identity from signed claims."""

from typing import Protocol

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fundermatch.workflow.errors import WorkflowAuthorizationError
from fundermatch.workflow.schema import ActorClaims, ActorRole


class TokenAuthenticator(Protocol):
    def authenticate(self, token: str) -> ActorClaims: ...


class _JwtClaims(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sub: str = Field(min_length=1)
    name: str = Field(min_length=1)
    roles: frozenset[ActorRole]


class JwtAuthenticator:
    def __init__(self, *, secret: str, issuer: str, audience: str) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must contain at least 32 characters")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def authenticate(self, token: str) -> ActorClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            claims = _JwtClaims.model_validate(payload)
        except (jwt.PyJWTError, ValidationError) as exc:
            raise WorkflowAuthorizationError("invalid bearer token") from exc
        return ActorClaims(
            actor_id=claims.sub,
            display_name=claims.name,
            roles=claims.roles,
        )
