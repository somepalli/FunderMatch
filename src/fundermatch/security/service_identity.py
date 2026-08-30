"""Provision and verify the shared FinDocIQ service identity."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

MINIMUM_SECRET_LENGTH = 32


@dataclass(frozen=True)
class ServiceIdentityPolicy:
    issuer: str
    audience: str
    maximum_token_age_seconds: int
    required_correlation_header: str
    policy_hash: str


@dataclass(frozen=True)
class ServiceIdentityReceipt:
    issuer: str
    audience: str
    maximum_token_age_seconds: int
    required_correlation_header: str
    policy_hash: str
    secret_fingerprint: str
    verified_at: str


def load_service_identity_policy(path: Path) -> ServiceIdentityPolicy:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("service_auth"), dict):
        raise ValueError(f"service_auth policy is missing from {path}")
    supplied_hash = payload.pop("policy_hash", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    calculated_hash = sha256(canonical.encode()).hexdigest()
    if supplied_hash and supplied_hash != calculated_hash:
        raise ValueError(f"guardrail policy hash is invalid: {path}")
    auth = payload["service_auth"]
    return ServiceIdentityPolicy(
        issuer=str(auth["issuer"]),
        audience=str(auth["audience"]),
        maximum_token_age_seconds=int(auth["maximum_token_age_seconds"]),
        required_correlation_header=str(auth["required_correlation_header"]),
        policy_hash=calculated_hash,
    )


def provision_shared_secret(destinations: tuple[Path, ...]) -> str:
    if len(destinations) < 2:
        raise ValueError("at least two deployment secret destinations are required")
    resolved = tuple(path.resolve() for path in destinations)
    if len(set(resolved)) != len(resolved):
        raise ValueError("deployment secret destinations must be distinct")
    existing = [path for path in resolved if path.exists()]
    if existing:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing service secrets: {names}")

    value = secrets.token_urlsafe(48)
    staged: list[tuple[Path, Path]] = []
    try:
        for destination in resolved:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                delete=False,
                prefix=f".{destination.name}.",
            ) as handle:
                handle.write(value + "\n")
                temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            temporary.replace(destination)
            os.chmod(destination, 0o600)
        return sha256(value.encode()).hexdigest()
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def verify_service_identity_policies(
    fundermatch_policy: Path, findociq_policy: Path
) -> ServiceIdentityPolicy:
    consumer = load_service_identity_policy(fundermatch_policy)
    producer = load_service_identity_policy(findociq_policy)
    if consumer != producer:
        mismatches = [
            field
            for field in asdict(consumer)
            if getattr(consumer, field) != getattr(producer, field)
        ]
        raise ValueError(f"FinDocIQ service policy mismatch: {', '.join(mismatches)}")
    return consumer


def verify_service_identity(
    fundermatch_secret: Path,
    findociq_secret: Path,
    fundermatch_policy: Path,
    findociq_policy: Path,
) -> ServiceIdentityReceipt:
    consumer = verify_service_identity_policies(fundermatch_policy, findociq_policy)

    consumer_secret = fundermatch_secret.read_text(encoding="utf-8").strip()
    producer_secret = findociq_secret.read_text(encoding="utf-8").strip()
    if len(consumer_secret) < MINIMUM_SECRET_LENGTH or len(producer_secret) < MINIMUM_SECRET_LENGTH:
        raise ValueError("service JWT secret must contain at least 32 characters")
    if not secrets.compare_digest(consumer_secret, producer_secret):
        raise ValueError("FunderMatch and FinDocIQ service JWT secrets differ")

    return ServiceIdentityReceipt(
        issuer=consumer.issuer,
        audience=consumer.audience,
        maximum_token_age_seconds=consumer.maximum_token_age_seconds,
        required_correlation_header=consumer.required_correlation_header,
        policy_hash=consumer.policy_hash,
        secret_fingerprint=sha256(consumer_secret.encode()).hexdigest(),
        verified_at=datetime.now(UTC).isoformat(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision = subparsers.add_parser("provision", help="create one secret for two deployments")
    provision.add_argument("--fundermatch-secret", type=Path, required=True)
    provision.add_argument("--findociq-secret", type=Path, required=True)

    policy = subparsers.add_parser("verify-policy", help="verify producer/consumer policy")
    policy.add_argument(
        "--fundermatch-policy",
        type=Path,
        default=Path("configs/guardrails/production.yaml"),
    )
    policy.add_argument("--findociq-policy", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify deployed identity without printing it")
    verify.add_argument("--fundermatch-secret", type=Path, required=True)
    verify.add_argument("--findociq-secret", type=Path, required=True)
    verify.add_argument(
        "--fundermatch-policy",
        type=Path,
        default=Path("configs/guardrails/production.yaml"),
    )
    verify.add_argument("--findociq-policy", type=Path, required=True)
    verify.add_argument("--receipt", type=Path)

    args = parser.parse_args()
    if args.command == "provision":
        fingerprint = provision_shared_secret(
            (args.fundermatch_secret, args.findociq_secret)
        )
        print(json.dumps({"status": "provisioned", "secret_fingerprint": fingerprint}))
        return

    if args.command == "verify-policy":
        identity = verify_service_identity_policies(
            args.fundermatch_policy, args.findociq_policy
        )
        print(json.dumps(asdict(identity), indent=2, sort_keys=True))
        return

    receipt = verify_service_identity(
        args.fundermatch_secret,
        args.findociq_secret,
        args.fundermatch_policy,
        args.findociq_policy,
    )
    rendered = json.dumps(asdict(receipt), indent=2, sort_keys=True)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
