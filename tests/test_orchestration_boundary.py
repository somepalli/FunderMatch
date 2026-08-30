from pathlib import Path

from fundermatch.orchestration.graph import ApplicationMemoryGraph
from fundermatch.orchestration.schema import DEFAULT_WORKER_ORDER, GraphStatus, WorkerName


def test_graph_route_only_continues_or_stops_from_operational_status() -> None:
    assert ApplicationMemoryGraph._route({"status": GraphStatus.RUNNING.value}) == "continue"
    for status in GraphStatus:
        if status != GraphStatus.RUNNING:
            assert ApplicationMemoryGraph._route({"status": status.value}) == "stop"


def test_fixed_order_keeps_eligibility_and_guardrails_before_human_review() -> None:
    assert DEFAULT_WORKER_ORDER == (
        WorkerName.DOCUMENT_PROCESSING,
        WorkerName.FINANCIAL_ANALYSIS,
        WorkerName.ELIGIBILITY,
        WorkerName.PRECEDENT_RETRIEVAL,
        WorkerName.SUGGESTION,
        WorkerName.GUARDRAILS,
        WorkerName.HUMAN_REVIEW,
    )


def test_graph_module_contains_no_financial_or_lending_decision_logic() -> None:
    source = Path("src/fundermatch/orchestration/graph.py").read_text(encoding="utf-8")
    for forbidden in ("EligibilityEngine", "HumanAction", "approve_with_conditions"):
        assert forbidden not in source
