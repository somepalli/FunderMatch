"""Validate FunderMatch's models against FinDocIQ's public contract bundle."""

import argparse
import json
from pathlib import Path

from fundermatch.validation.contracts import validate_contract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.contract.read_text(encoding="utf-8"))
    failures = validate_contract(payload)
    if failures:
        raise SystemExit("\n\n".join(failures))
    print(f"FinDocIQ contract is compatible: {args.contract}")


if __name__ == "__main__":
    main()
