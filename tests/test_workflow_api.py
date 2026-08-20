from fastapi.testclient import TestClient

from fundermatch.api.app import create_app
from fundermatch.workflow.repository import InMemoryWorkflowRepository
from fundermatch.workflow.schema import ActorClaims, ActorRole


class FakeAuthenticator:
    def authenticate(self, token: str) -> ActorClaims:
        if token == "pipeline-token":
            return ActorClaims(
                actor_id="pipeline-api",
                display_name="Pipeline API",
                roles={ActorRole.PIPELINE},
            )
        return ActorClaims(
            actor_id="reviewer-api",
            display_name="API Reviewer",
            roles={ActorRole.HUMAN_REVIEWER},
        )


def test_api_requires_authentication_and_signed_role():
    client = TestClient(create_app(InMemoryWorkflowRepository(), FakeAuthenticator()))
    assert client.post("/v1/workflows", json={"application_id": "api-1"}).status_code == 401
    response = client.post(
        "/v1/workflows",
        json={"application_id": "api-1"},
        headers={"Authorization": "Bearer reviewer-token"},
    )
    assert response.status_code == 403


def test_api_human_actor_is_derived_from_token():
    client = TestClient(create_app(InMemoryWorkflowRepository(), FakeAuthenticator()))
    pipeline_headers = {"Authorization": "Bearer pipeline-token"}
    reviewer_headers = {"Authorization": "Bearer reviewer-token"}
    response = client.post(
        "/v1/workflows", json={"application_id": "api-2"}, headers=pipeline_headers
    )
    assert response.status_code == 201
    for version, state in enumerate(
        ["EXTRACTED", "RULE_GATED", "AI_SUGGESTED", "AWAITING_HUMAN"]
    ):
        payload = {
            "expected_version": version,
            "target_state": state,
            "reason": f"entered {state}",
        }
        if state == "AI_SUGGESTED":
            payload["suggestion"] = {"recommendation": "review"}
        response = client.post(
            "/v1/workflows/api-2/pipeline", json=payload, headers=pipeline_headers
        )
        assert response.status_code == 200, response.text

    response = client.post(
        "/v1/workflows/api-2/decision",
        json={
            "expected_version": 4,
            "action": "approve_with_conditions",
            "reason": "Approved after human review",
            "conditions": ["Quarterly monitoring"],
            "actor_id": "forged-user",
        },
        headers=reviewer_headers,
    )
    assert response.status_code == 200, response.text
    decision = response.json()["workflow"]["decision"]
    assert decision["actor_id"] == "reviewer-api"
    assert decision["actor_display_name"] == "API Reviewer"

    audit = client.get("/v1/workflows/api-2/audit", headers=reviewer_headers).json()["events"]
    assert len(audit) == 6
    assert audit[-1]["actor_id"] == "reviewer-api"
