from datetime import UTC, datetime, timedelta

import jwt
import pytest

from fundermatch.api.auth import JwtAuthenticator
from fundermatch.workflow.errors import WorkflowAuthorizationError
from fundermatch.workflow.schema import ActorRole

SECRET = "phase-4-test-secret-that-is-longer-than-32-characters"


def token(**overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": "reviewer-42",
        "name": "Signed Reviewer",
        "roles": ["human_reviewer"],
        "iss": "fundermatch",
        "aud": "fundermatch-api",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_jwt_authenticator_accepts_signed_identity_and_roles():
    authenticator = JwtAuthenticator(
        secret=SECRET, issuer="fundermatch", audience="fundermatch-api"
    )
    actor = authenticator.authenticate(token())
    assert actor.actor_id == "reviewer-42"
    assert actor.display_name == "Signed Reviewer"
    assert actor.roles == {ActorRole.HUMAN_REVIEWER}


@pytest.mark.parametrize(
    "bad_token",
    [
        token(exp=datetime.now(UTC) - timedelta(seconds=1)),
        token(aud="another-api"),
        jwt.encode(
            {
                "sub": "attacker",
                "name": "Attacker",
                "roles": ["human_reviewer"],
                "iss": "fundermatch",
                "aud": "fundermatch-api",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            "a-different-secret-that-is-longer-than-thirty-two",
            algorithm="HS256",
        ),
    ],
)
def test_jwt_authenticator_rejects_expired_wrong_audience_and_forged_tokens(bad_token):
    authenticator = JwtAuthenticator(
        secret=SECRET, issuer="fundermatch", audience="fundermatch-api"
    )
    with pytest.raises(WorkflowAuthorizationError):
        authenticator.authenticate(bad_token)


def test_jwt_authenticator_rejects_short_secret():
    with pytest.raises(ValueError):
        JwtAuthenticator(secret="short", issuer="fundermatch", audience="fundermatch-api")
