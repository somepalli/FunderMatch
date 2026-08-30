import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from fundermatch.api.app import _mask_workflow
from fundermatch.orchestration.workspace import ApplicationWorkspace
from fundermatch.precedent.schema import PrecedentStatus
from fundermatch.security.policy import ProductionGuardrailPolicy
from fundermatch.security.receipts import ReceiptSigner
from fundermatch.workflow.schema import WorkflowRecord

ROOT = Path(__file__).parents[1]


def test_normal_api_masking_covers_cited_legal_name() -> None:
    record = WorkflowRecord(
        application_id="APP-MASK-1",
        suggestion={
            "application": {
                "borrower_name": "Borrower Private Limited",
                "finance_context": "finance",
                "operations_context": "operations",
                "evidence": [
                    {"name": "borrower_name", "value": "Borrower Private Limited"},
                    {"name": "dscr", "value": "1.4"},
                ],
            }
        },
    )

    application = _mask_workflow(record).suggestion["application"]
    assert application["borrower_name"] == "[MASKED]"
    assert application["evidence"][0]["value"] == "[MASKED]"
    assert application["evidence"][1]["value"] == "1.4"


class _SensitiveArtifact(BaseModel):
    value: str


def test_production_policy_loads_worker_limits_and_stable_hash() -> None:
    policy = ProductionGuardrailPolicy.from_yaml(
        ROOT / "configs/guardrails/production.yaml"
    )
    assert len(policy.policy_hash) == 64
    assert policy.workers["financial_analysis"].max_calls == 13
    assert policy.workers["document_processing"].permitted_tools == (
        "findociq_ingest",
    )


def test_worker_receipt_signature_detects_tampering() -> None:
    signer = ReceiptSigner("r" * 32)
    payload = {
        "worker": "financial_analysis",
        "attempt": 1,
        "tool_calls": ["findociq_extract"],
        "policy_hash": "a" * 64,
    }
    signature = signer.sign(payload)
    assert signer.verify(payload, signature)
    assert not signer.verify({**payload, "attempt": 2}, signature)


def test_precedent_lifecycle_status_is_fail_closed(synthetic_cases) -> None:  # type: ignore[no-untyped-def]
    precedent = synthetic_cases[0]
    assert precedent.is_retrievable()
    assert not precedent.model_copy(
        update={"lifecycle_status": PrecedentStatus.REVOKED}
    ).is_retrievable()
    assert not precedent.model_copy(
        update={"valid_until": datetime.now(UTC) - timedelta(seconds=1)}
    ).is_retrievable()


def test_production_workspace_encrypts_revealable_artifacts(tmp_path: Path) -> None:
    workspace = ApplicationWorkspace(tmp_path, master_key=b"k" * 32)
    artifact = _SensitiveArtifact(value="borrower-secret@example.test")
    workspace.save("APP-SECURE", "request", artifact)

    stored = (tmp_path / "APP-SECURE" / "agent" / "request.json").read_bytes()
    assert artifact.value.encode() not in stored
    assert workspace.load("APP-SECURE", "request", _SensitiveArtifact) == artifact


def test_workspace_key_rotation_rewraps_without_rewriting_ciphertext(
    tmp_path: Path,
) -> None:
    artifact = _SensitiveArtifact(value="protected")
    workspace = ApplicationWorkspace(tmp_path, master_key=b"o" * 32, key_version="v1")
    workspace.save("APP-ROTATE", "request", artifact)
    target = tmp_path / "APP-ROTATE" / "agent" / "request.json"
    before = json.loads(target.read_text(encoding="utf-8"))

    assert workspace.rewrap_all(b"n" * 32, "v2") == 1
    after = json.loads(target.read_text(encoding="utf-8"))
    assert before["ciphertext"] == after["ciphertext"]
    assert before["wrapped_key"] != after["wrapped_key"]
    assert workspace.load("APP-ROTATE", "request", _SensitiveArtifact) == artifact
