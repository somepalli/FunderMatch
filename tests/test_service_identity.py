from pathlib import Path

import pytest

from fundermatch.security.service_identity import (
    load_service_identity_policy,
    provision_shared_secret,
    verify_service_identity,
    verify_service_identity_policies,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "guardrails" / "production.yaml"


def test_provision_writes_identical_high_entropy_secrets_without_exposing_value(tmp_path: Path):
    fundermatch = tmp_path / "fundermatch" / "service_jwt.txt"
    findociq = tmp_path / "findociq" / "service_jwt.txt"

    fingerprint = provision_shared_secret((fundermatch, findociq))

    assert len(fingerprint) == 64
    assert fundermatch.read_text(encoding="utf-8") == findociq.read_text(encoding="utf-8")
    assert len(fundermatch.read_text(encoding="utf-8").strip()) >= 32
    assert fingerprint not in fundermatch.read_text(encoding="utf-8")


def test_provision_refuses_to_overwrite_either_deployment(tmp_path: Path):
    fundermatch = tmp_path / "fundermatch.txt"
    fundermatch.write_text("already-provisioned", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        provision_shared_secret((fundermatch, tmp_path / "findociq.txt"))


def test_verify_accepts_matching_secret_and_policy(tmp_path: Path):
    fundermatch = tmp_path / "fundermatch.txt"
    findociq = tmp_path / "findociq.txt"
    provision_shared_secret((fundermatch, findociq))

    receipt = verify_service_identity(fundermatch, findociq, POLICY, POLICY)

    policy = load_service_identity_policy(POLICY)
    assert receipt.issuer == policy.issuer == "fundermatch"
    assert receipt.audience == policy.audience == "findociq-api"
    assert receipt.policy_hash == policy.policy_hash
    assert len(receipt.secret_fingerprint) == 64


def test_verify_rejects_secret_and_policy_drift(tmp_path: Path):
    fundermatch = tmp_path / "fundermatch.txt"
    findociq = tmp_path / "findociq.txt"
    fundermatch.write_text("a" * 48, encoding="utf-8")
    findociq.write_text("b" * 48, encoding="utf-8")
    with pytest.raises(ValueError, match="secrets differ"):
        verify_service_identity(fundermatch, findociq, POLICY, POLICY)

    findociq.write_text("a" * 48, encoding="utf-8")
    changed_policy = tmp_path / "changed.yaml"
    changed_policy.write_text(
        POLICY.read_text(encoding="utf-8").replace(
            "audience: findociq-api", "audience: wrong-audience"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="policy mismatch"):
        verify_service_identity(fundermatch, findociq, POLICY, changed_policy)


def test_policy_only_verification_rejects_audience_drift(tmp_path: Path):
    changed_policy = tmp_path / "changed.yaml"
    changed_policy.write_text(
        POLICY.read_text(encoding="utf-8").replace(
            "audience: findociq-api", "audience: wrong-audience"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="audience"):
        verify_service_identity_policies(POLICY, changed_policy)
