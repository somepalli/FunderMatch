"""Run an eligibility-first retrieval smoke against the local precedent corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from qdrant_client import QdrantClient

from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.precedent.corpus import load_cases
from fundermatch.precedent.embedder import BgeM3Config, BgeM3Embedder
from fundermatch.rules.config import load_policies
from fundermatch.rules.schema import BorrowerApplication


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", default="SYN-001")
    parser.add_argument(
        "--corpus", type=Path, default=Path("data/synthetic_decided_loans.jsonl")
    )
    parser.add_argument(
        "--policies", type=Path, default=Path("configs/funder_policies.yaml")
    )
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6999")
    parser.add_argument("--collection", default="fundermatch_precedents")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--min-score", type=float, default=0.45)
    args = parser.parse_args()

    cases = load_cases(args.corpus)
    source = next((case for case in cases if case.case_id == args.case_id), None)
    if source is None:
        raise SystemExit(f"unknown synthetic case ID: {args.case_id}")
    application = BorrowerApplication(
        application_id=f"SMOKE-{source.case_id}",
        borrower_name=f"New synthetic applicant aligned to {source.case_id}",
        industry=source.industry,
        region=source.region,
        profile=source.profile,
        finance_context="Figures reconcile and require independent human review.",
        operations_context="Operating history requires independent human review.",
    )
    retriever = RuleGatedPrecedentRetriever(
        client=QdrantClient(url=args.qdrant_url),
        embedder=BgeM3Embedder(BgeM3Config(snapshot_dir=args.model_dir)),
        config=RetrievalConfig(
            collection=args.collection,
            min_score=args.min_score,
        ),
    )
    result = retriever.retrieve(application, load_policies(args.policies))
    print(result.model_dump_json(indent=2))
