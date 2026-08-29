"""Run the held-out release cases against real BGE-M3 and Qdrant services."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from qdrant_client import QdrantClient

from fundermatch.clients.findociq_contract import BoundingBox, SourceCitation
from fundermatch.evals.metrics import (
    PerformanceObservation,
    RetrievalObservation,
    performance_metrics,
    retrieval_metrics,
)
from fundermatch.evals.schema import AgentReleaseCase, InventedApplication
from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.precedent.corpus import load_cases
from fundermatch.precedent.embedder import BgeM3Config, BgeM3Embedder
from fundermatch.precedent.schema import EvidenceMetric, FinancialProfile
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore
from fundermatch.rules.config import load_policies
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication


def _load_release_cases(path: Path) -> tuple[AgentReleaseCase, ...]:
    return tuple(
        AgentReleaseCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _application(source: InventedApplication) -> BorrowerApplication:
    document_id = f"synthetic-eval-{source.application_id.lower()}"
    citation = SourceCitation(
        document_id=document_id,
        page_number=1,
        bbox=BoundingBox(x0=10, y0=10, x1=100, y1=30),
    )
    profile = FinancialProfile(
        annual_revenue_crore=source.annual_revenue_crore,
        requested_amount_crore=source.requested_amount_crore,
        ebitda_margin_pct=source.ebitda_margin_pct,
        dscr=source.dscr,
        debt_to_ebitda=source.debt_to_ebitda,
        collateral_cover=source.collateral_cover,
        years_operating=source.years_operating,
        employee_count=source.employee_count,
    )
    evidence = tuple(
        EvidenceMetric(
            name=name,
            value=getattr(source, name),
            unit=unit,
            period="FY2026",
            citation=citation,
        )
        for name, unit in (
            ("annual_revenue_crore", "INR crore"),
            ("ebitda_margin_pct", "percent"),
            ("dscr", "ratio"),
        )
    )
    return BorrowerApplication(
        application_id=source.application_id,
        borrower_name=f"Synthetic held-out borrower {source.application_id}",
        industry=source.industry,
        region=source.region,
        profile=profile,
        evidence=evidence,
        finance_context="Held-out finance evidence for deterministic release evaluation.",
        operations_context="Held-out operations evidence for deterministic release evaluation.",
    )


def _environment() -> str:
    try:
        import torch

        return "gpu" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("evals/datasets/agent_release_cases.jsonl")
    )
    parser.add_argument("--corpus", type=Path, default=Path("data/synthetic_decided_loans.jsonl"))
    parser.add_argument("--policies", type=Path, default=Path("configs/funder_policies.yaml"))
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6999")
    parser.add_argument("--collection", default="fundermatch_phase8_release")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-score", type=float, default=0.45)
    parser.add_argument("--skip-seed", action="store_true")
    args = parser.parse_args()

    release_cases = _load_release_cases(args.dataset)
    if len(release_cases) != 24:
        raise SystemExit(f"release dataset must contain n=24, found {len(release_cases)}")
    corpus = load_cases(args.corpus)
    corpus_ids = {item.case_id for item in corpus}
    unresolved = sorted(
        expected
        for case in release_cases
        for expected in case.expected_precedent_ids
        if expected not in corpus_ids
    )
    if unresolved:
        raise SystemExit(f"unresolved expected precedent IDs: {', '.join(unresolved)}")

    policies = load_policies(args.policies)
    engine = EligibilityEngine()
    eligibility_failures: list[dict[str, object]] = []
    applications: dict[str, BorrowerApplication] = {}
    for case in release_cases:
        application = _application(case.application)
        applications[case.case_id] = application
        actual = {
            item.funder_id for item in engine.evaluate_all(application, policies) if item.eligible
        }
        expected = set(case.expected_eligible_funders)
        if actual != expected or actual.intersection(case.forbidden_funders):
            eligibility_failures.append(
                {"case_id": case.case_id, "expected": sorted(expected), "actual": sorted(actual)}
            )

    embedder = BgeM3Embedder(BgeM3Config(snapshot_dir=args.model_dir, use_fp16=True))
    client = QdrantClient(url=args.qdrant_url)
    seed_ms: float | None = None
    if not args.skip_seed:
        started = time.perf_counter()
        QdrantPrecedentStore(
            QdrantPrecedentConfig(url=args.qdrant_url, collection=args.collection), client=client
        ).seed(corpus, embedder, recreate=True)
        seed_ms = (time.perf_counter() - started) * 1000
    retriever = RuleGatedPrecedentRetriever(
        client=client,
        embedder=embedder,
        config=RetrievalConfig(
            collection=args.collection,
            top_k=5,
            candidate_limit=20,
            min_score=args.min_score,
            require_active_lifecycle=True,
        ),
    )
    observations: list[RetrievalObservation] = []
    retrieval_cases: list[dict[str, object]] = []
    perf: list[PerformanceObservation] = []
    if seed_ms is not None:
        perf.append(
            PerformanceObservation(environment=_environment(), stage="seed", latency_ms=seed_ms)
        )
    for case in release_cases:
        if case.cohort not in {"eligible_aligned", "no_close_precedent"}:
            continue
        started = time.perf_counter()
        result = retriever.retrieve(applications[case.case_id], policies)
        perf.append(
            PerformanceObservation(
                environment=_environment(),
                stage="retrieval",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        )
        ranked = tuple(item.precedent.case_id for item in result.matches)
        funders = {item.precedent.decision.funder_id for item in result.matches}
        leakage = bool(funders.intersection(case.forbidden_funders))
        retrieval_cases.append(
            {"case_id": case.case_id, "ranked_precedent_ids": ranked, "leakage": leakage}
        )
        if case.expected_precedent_ids:
            observations.append(
                RetrievalObservation(
                    case_id=case.case_id,
                    expected_precedent_ids=case.expected_precedent_ids,
                    ranked_precedent_ids=ranked,
                    forbidden_funder_leakage=leakage,
                )
            )

    metrics = retrieval_metrics(tuple(observations))
    missing_expected = [
        item.case_id
        for item in observations
        if not set(item.expected_precedent_ids).intersection(item.ranked_precedent_ids)
    ]
    unexpected_no_close = [
        item["case_id"]
        for item in retrieval_cases
        if item["case_id"] in {
            case.case_id for case in release_cases if case.cohort == "no_close_precedent"
        }
        and item["ranked_precedent_ids"]
    ]
    report = {
        "n": len(release_cases),
        "retrieval_n": metrics.n,
        "environment": _environment(),
        "eligibility_failures": eligibility_failures,
        "missing_expected_precedents": missing_expected,
        "unexpected_no_close_matches": unexpected_no_close,
        "retrieval_metrics": metrics.model_dump(mode="json"),
        "performance": [item.model_dump(mode="json") for item in performance_metrics(tuple(perf))],
        "retrieval_cases": retrieval_cases,
        "adversarial_n": sum(case.cohort == "adversarial" for case in release_cases),
        "note": (
            "Adversarial guardrail cases are executed by the deterministic guardrail "
            "test suite."
        ),
    }
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if eligibility_failures or missing_expected or unexpected_no_close:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
