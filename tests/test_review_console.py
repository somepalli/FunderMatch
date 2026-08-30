from datetime import timedelta

from fastapi.testclient import TestClient

from fundermatch.api.app import create_app
from fundermatch.api.auth import JwtAuthenticator
from fundermatch.intake import IntakeDocument, IntakeResult
from fundermatch.workflow.demo import issue_token
from fundermatch.workflow.repository import InMemoryWorkflowRepository
from fundermatch.workflow.schema import ActorClaims, ActorRole, WorkflowRecord, WorkflowState


class ConsoleAuthenticator:
    def authenticate(self, token: str) -> ActorClaims:
        return ActorClaims(
            actor_id="console-test",
            display_name="Console Test",
            roles={ActorRole.HUMAN_REVIEWER},
        )


class PipelineAuthenticator:
    def authenticate(self, token: str) -> ActorClaims:
        return ActorClaims(
            actor_id="intake-test",
            display_name="Intake Test",
            roles={ActorRole.PIPELINE},
        )


class FakeIntakeService:
    def __init__(self) -> None:
        self.calls = []

    async def process(self, metadata, files, actor):  # type: ignore[no-untyped-def]
        self.calls.append((metadata, files, actor))
        return IntakeResult(
            workflow=WorkflowRecord(
                application_id=metadata.application_id,
                state=WorkflowState.AWAITING_HUMAN,
                version=4,
            ),
            documents=(
                IntakeDocument(
                    filename="borrower.pdf",
                    sha256="a" * 64,
                    document_id="b" * 64,
                    page_count=1,
                    chunk_count=2,
                ),
            ),
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
    assert "/v1/intake-jobs" in script.text
    assert "activity-events" in styles.text
    assert "fundermatch.activeIntakeJob" in script.text


def test_pipeline_can_upload_real_pdf_boundary() -> None:
    intake = FakeIntakeService()
    client = TestClient(
        create_app(
            InMemoryWorkflowRepository(),
            PipelineAuthenticator(),
            intake_service=intake,  # type: ignore[arg-type]
        )
    )
    metadata = {
        "requested_amount_crore": "25",
        "loan_type": "term_loan",
    }
    response = client.post(
        "/v1/intake",
        data={"metadata": __import__("json").dumps(metadata)},
        files=[("files", ("borrower.pdf", b"%PDF-1.7 test", "application/pdf"))],
        headers={"Authorization": "Bearer pipeline-token"},
    )
    assert response.status_code == 201
    assert response.json()["workflow"]["state"] == "AWAITING_HUMAN"
    assert intake.calls[0][1][0][0] == "borrower.pdf"


def test_intake_rejects_manually_supplied_document_facts() -> None:
    intake = FakeIntakeService()
    client = TestClient(
        create_app(
            InMemoryWorkflowRepository(),
            PipelineAuthenticator(),
            intake_service=intake,  # type: ignore[arg-type]
        )
    )
    response = client.post(
        "/v1/intake",
        data={
            "metadata": __import__("json").dumps(
                {
                    "requested_amount_crore": "25",
                    "loan_type": "term_loan",
                    "industry": "Manually supplied industry",
                }
            )
        },
        files=[("files", ("borrower.pdf", b"%PDF-1.7 test", "application/pdf"))],
        headers={"Authorization": "Bearer pipeline-token"},
    )
    assert response.status_code == 422
    assert intake.calls == []


def test_upload_boundary_accepts_more_than_ten_pdfs() -> None:
    intake = FakeIntakeService()
    client = TestClient(
        create_app(
            InMemoryWorkflowRepository(),
            PipelineAuthenticator(),
            intake_service=intake,  # type: ignore[arg-type]
        )
    )
    metadata = {
        "requested_amount_crore": "25",
        "loan_type": "term_loan",
    }
    response = client.post(
        "/v1/intake",
        data={"metadata": __import__("json").dumps(metadata)},
        files=[
            ("files", (f"borrower-{index}.pdf", b"%PDF-1.7 test", "application/pdf"))
            for index in range(12)
        ],
        headers={"Authorization": "Bearer pipeline-token"},
    )
    assert response.status_code == 201
    assert len(intake.calls[0][1]) == 12


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
