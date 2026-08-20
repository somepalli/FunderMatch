from datetime import timedelta

from fastapi.testclient import TestClient

from fundermatch.api.app import create_app
from fundermatch.api.auth import JwtAuthenticator
from fundermatch.workflow.demo import issue_token
from fundermatch.workflow.repository import InMemoryWorkflowRepository
from fundermatch.workflow.schema import ActorClaims, ActorRole


class ConsoleAuthenticator:
    def authenticate(self, token: str) -> ActorClaims:
        return ActorClaims(
            actor_id="console-test",
            display_name="Console Test",
            roles={ActorRole.HUMAN_REVIEWER},
        )


def test_review_console_serves_three_panels_with_security_headers() -> None:
    client = TestClient(create_app(InMemoryWorkflowRepository(), ConsoleAuthenticator()))
    response = client.get("/")

    assert response.status_code == 200
    assert "Borrower evidence" in response.text
    assert "Funder shortlist" in response.text
    assert "Precedent &amp; decision" in response.text
    assert "chat" not in response.text.lower()
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_console_assets_are_local_and_use_session_scoped_tokens() -> None:
    client = TestClient(create_app(InMemoryWorkflowRepository(), ConsoleAuthenticator()))
    styles = client.get("/assets/styles.css")
    script = client.get("/assets/app.js")

    assert styles.status_code == 200
    assert ".workspace-grid" in styles.text
    assert script.status_code == 200
    assert "sessionStorage" in script.text
    assert "localStorage" not in script.text
    assert "expected_version" in script.text
    assert "/decision" in script.text
    assert "/precedent" in script.text


def test_demo_tokens_are_short_lived_signed_role_claims() -> None:
    secret = "phase-six-demo-secret-longer-than-thirty-two-characters"
    encoded = issue_token(
        secret=secret,
        issuer="fundermatch",
        audience="fundermatch-api",
        subject="demo-reviewer",
        name="Demo Reviewer",
        role=ActorRole.HUMAN_REVIEWER,
        lifetime=timedelta(minutes=10),
    )
    actor = JwtAuthenticator(
        secret=secret,
        issuer="fundermatch",
        audience="fundermatch-api",
    ).authenticate(encoded)

    assert actor.actor_id == "demo-reviewer"
    assert actor.roles == {ActorRole.HUMAN_REVIEWER}
