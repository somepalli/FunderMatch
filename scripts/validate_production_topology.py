"""Fail CI when the production Compose trust boundaries drift."""

import argparse
from pathlib import Path

from fundermatch.validation.topology import validate_topology


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=Path("docker-compose.production.yml"))
    args = parser.parse_args()
    failures = validate_topology(args.compose)
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"production topology is valid: {args.compose}")


if __name__ == "__main__":
    main()
