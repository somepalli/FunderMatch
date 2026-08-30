"""Production Compose trust-boundary validation."""

from pathlib import Path

import yaml


def validate_topology(path: Path) -> tuple[str, ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = payload.get("services", {})
    networks = payload.get("networks", {})
    failures: list[str] = []
    published = {name for name, service in services.items() if service.get("ports")}
    if published != {"proxy"}:
        failures.append(f"only proxy may publish host ports; found {sorted(published)}")
    for name in ("findociq", "postgres", "qdrant", "vllm", "clamav"):
        if services.get(name, {}).get("ports"):
            failures.append(f"{name} must not publish host ports")
    for name in ("application", "model_data"):
        if networks.get(name, {}).get("internal") is not True:
            failures.append(f"network {name} must remain internal")
    findociq = services.get("findociq", {})
    if (
        findociq.get("build", {}).get("context")
        != "${FINDOCIQ_REPO_PATH:-../FinDocs_Analysis_Evals}"
    ):
        failures.append("FinDocIQ build context must use the canonical repository name")
    fundermatch = services.get("fundermatch", {})
    if fundermatch.get("environment", {}).get("FINDOCIQ_BASE_URL") != "http://findociq:8989":
        failures.append("FunderMatch must call FinDocIQ over the private service network")
    if "service_jwt" not in fundermatch.get("secrets", []):
        failures.append("FunderMatch must receive the shared FinDocIQ service JWT secret")
    if "service_jwt" not in findociq.get("secrets", []):
        failures.append("FinDocIQ must receive the shared service JWT secret")
    if (
        fundermatch.get("environment", {}).get("FUNDERMATCH_PRODUCTION_GUARDRAILS_ENABLED")
        != "true"
    ):
        failures.append("FunderMatch production guardrails must be enabled")
    if (
        findociq.get("environment", {}).get("FINDOCIQ_PRODUCTION_GUARDRAILS_ENABLED")
        != "true"
    ):
        failures.append("FinDocIQ production guardrails must be enabled")
    return tuple(failures)
