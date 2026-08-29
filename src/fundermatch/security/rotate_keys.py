"""Rewrap encrypted FunderMatch workspace keys and audit the operation."""

from __future__ import annotations

import argparse
from base64 import b64decode
from pathlib import Path
from uuid import uuid4

import psycopg

from fundermatch.orchestration.workspace import ApplicationWorkspace
from fundermatch.security.policy import ProductionGuardrailPolicy
from fundermatch.security.secrets import read_secret


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--old-key-file", type=Path, required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-key-file", type=Path, required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument(
        "--policy", type=Path, default=Path("configs/guardrails/production.yaml")
    )
    args = parser.parse_args()
    dsn = read_secret("FUNDERMATCH_DATABASE_URL")
    policy_hash = ProductionGuardrailPolicy.from_yaml(args.policy).policy_hash
    old_key = b64decode(args.old_key_file.read_text(encoding="utf-8").strip(), validate=True)
    new_key = b64decode(args.new_key_file.read_text(encoding="utf-8").strip(), validate=True)
    workspace = ApplicationWorkspace(
        args.root, master_key=old_key, key_version=args.old_version
    )
    changed = workspace.rewrap_all(new_key, args.new_version)
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO key_rotation_events
                (event_id, old_key_version, new_key_version, object_count, policy_hash)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid4(), args.old_version, args.new_version, changed, policy_hash),
        )
    print(f"rewrapped={changed} key_version={args.new_version}")


if __name__ == "__main__":
    main()
