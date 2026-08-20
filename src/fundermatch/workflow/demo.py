"""Create one invented Phase 6 review case and short-lived local JWTs."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt

from fundermatch.matching.schema import PrecedentMatch, RuleGatedRetrievalResult
from fundermatch.precedent.corpus import load_cases
from fundermatch.rules.config import load_policies
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.workflow.postgres import PostgresWorkflowRepository
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    PipelineAdvanceCommand,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService


def issue_token(
    *,
    secret: str,
    issuer: str,
    audience: str,
    subject: str,
    name: str,
    role: ActorRole,
    lifetime: timedelta,
) -> str:
    if len(secret) < 32:
        raise ValueError("JWT secret must contain at least 32 characters")
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "name": name,
            "roles": [role.value],
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + lifetime,
        },
        secret,
        algorithm="HS256",
    )


async def run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[3]
    for migration in sorted((root / "migrations").glob("*.sql")):
        await PostgresWorkflowRepository.migrate(args.database_url, migration)
    cases = load_cases(root / "data" / "synthetic_decided_loans.jsonl")
    source = cases[0]
    application_id = args.application_id or f"APP-PHASE6-{uuid4().hex[:10].upper()}"
    application = BorrowerApplication(
        application_id=application_id,
        borrower_name="Aster Precision Components",
        industry=source.industry,
        region=source.region,
        profile=source.profile.model_copy(
            update={"requested_amount_crore": source.profile.requested_amount_crore + 1}
        ),
        evidence=source.evidence,
        finance_context=(
            "Synthetic review notes stable cash generation; working-capital concentration "
            "needs quarterly monitoring."
        ),
        operations_context=(
            "Synthetic review confirms established customers and available plant capacity."
        ),
    )
    policies = load_policies(root / "configs" / "funder_policies.yaml")
    eligibility = EligibilityEngine().evaluate_all(application, policies)
    case_by_funder = {case.decision.funder_id: case for case in cases}
    matches = tuple(
        PrecedentMatch(
            precedent=case_by_funder[item.funder_id],
            score=0.88,
            profile_score=0.94,
            comments_score=0.70,
        )
        for item in eligibility
        if item.eligible and item.funder_id in case_by_funder
    )
    suggestion = SuggestionAssembler().assemble(
        application,
        policies,
        RuleGatedRetrievalResult(
            application_id=application_id,
            eligibility=eligibility,
            matches=matches,
        ),
    )
    pipeline = ActorClaims(
        actor_id="phase6-demo-pipeline",
        display_name="Phase 6 Demo Pipeline",
        roles={ActorRole.PIPELINE},
    )
    async with PostgresWorkflowRepository.connect(args.database_url) as repository:
        workflow = WorkflowService(repository)
        await workflow.create(application_id, pipeline)
        for version, target in enumerate(
            (
                WorkflowState.EXTRACTED,
                WorkflowState.RULE_GATED,
                WorkflowState.AI_SUGGESTED,
                WorkflowState.AWAITING_HUMAN,
            )
        ):
            await workflow.advance_pipeline(
                application_id,
                PipelineAdvanceCommand(
                    expected_version=version,
                    target_state=target,
                    reason=f"Synthetic demo entered {target.value}",
                    suggestion=suggestion.model_dump(mode="json")
                    if target == WorkflowState.AI_SUGGESTED
                    else None,
                ),
                pipeline,
            )
    lifetime = timedelta(hours=args.token_hours)
    common = {
        "secret": args.jwt_secret,
        "issuer": args.jwt_issuer,
        "audience": args.jwt_audience,
        "lifetime": lifetime,
    }
    output = {
        "application_id": application_id,
        "review_url": f"{args.base_url}/?application={application_id}",
        "reviewer_token": issue_token(
            **common,
            subject="phase6-demo-reviewer",
            name="Phase 6 Demo Reviewer",
            role=ActorRole.HUMAN_REVIEWER,
        ),
        "pipeline_token": issue_token(
            **common,
            subject="phase6-demo-pipeline",
            name="Phase 6 Demo Pipeline",
            role=ActorRole.PIPELINE,
        ),
        "expires_in_hours": args.token_hours,
        "data_classification": "invented_synthetic_demo",
    }
    print(json.dumps(output, indent=2))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--database-url",
        default=os.getenv("FUNDERMATCH_DATABASE_URL"),
        required=os.getenv("FUNDERMATCH_DATABASE_URL") is None,
    )
    value.add_argument(
        "--jwt-secret",
        default=os.getenv("FUNDERMATCH_JWT_SECRET"),
        required=os.getenv("FUNDERMATCH_JWT_SECRET") is None,
    )
    value.add_argument("--jwt-issuer", default="fundermatch")
    value.add_argument("--jwt-audience", default="fundermatch-api")
    value.add_argument("--application-id")
    value.add_argument("--token-hours", type=int, default=8, choices=range(1, 25))
    value.add_argument("--base-url", default="http://127.0.0.1:8977")
    return value


def main() -> None:
    asyncio.run(run(parser().parse_args()))
