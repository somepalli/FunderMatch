"""Exercise Gemma narrative generation without loading BGE on the same GPU."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from fundermatch.matching.schema import PrecedentMatch, RuleGatedRetrievalResult
from fundermatch.precedent.corpus import load_cases
from fundermatch.rules.config import load_policies
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.suggest.narrative import GemmaNarrativeClient, GemmaNarrativeConfig


async def _run(args: argparse.Namespace) -> None:
    cases = load_cases(args.corpus)
    source = next((case for case in cases if case.case_id == args.case_id), None)
    if source is None:
        raise SystemExit(f"unknown synthetic case ID: {args.case_id}")
    application = BorrowerApplication(
        application_id=f"NARRATIVE-{source.case_id}",
        borrower_name=f"New synthetic applicant aligned to {source.case_id}",
        industry=source.industry,
        region=source.region,
        profile=source.profile,
        evidence=source.evidence,
        finance_context="Figures reconcile and require independent human review.",
        operations_context="Operating history requires independent human review.",
    )
    policies = load_policies(args.policies)
    retrieval = RuleGatedRetrievalResult(
        application_id=application.application_id,
        eligibility=EligibilityEngine().evaluate_all(application, policies),
        matches=(
            PrecedentMatch(
                precedent=source,
                score=1.0,
                profile_score=1.0,
                comments_score=1.0,
            ),
        ),
    )
    bundle = SuggestionAssembler().assemble(application, policies, retrieval)
    candidate = next(
        item for item in bundle.candidates if item.funder_id == source.decision.funder_id
    )
    run = await GemmaNarrativeClient(
        GemmaNarrativeConfig(
            base_url=args.base_url,
            prompt_path=args.prompt,
        )
    ).explain(candidate)
    print(run.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", default="SYN-001")
    parser.add_argument(
        "--corpus", type=Path, default=Path("data/synthetic_decided_loans.jsonl")
    )
    parser.add_argument(
        "--policies", type=Path, default=Path("configs/funder_policies.yaml")
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8900/v1")
    parser.add_argument(
        "--prompt",
        type=Path,
        default=Path("prompts/suggestion_narrative_system.txt"),
    )
    asyncio.run(_run(parser.parse_args()))
