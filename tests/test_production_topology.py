from pathlib import Path

from fundermatch.validation.topology import validate_topology


def test_production_compose_preserves_network_and_secret_boundaries() -> None:
    assert validate_topology(Path("docker-compose.production.yml")) == ()
