"""Apply the idempotent HITL workflow migration."""

import asyncio
import os
from pathlib import Path

from fundermatch.workflow.postgres import PostgresWorkflowRepository


async def run() -> None:
    dsn = os.environ["FUNDERMATCH_DATABASE_URL"]
    migration_dir = Path(__file__).resolve().parents[3] / "migrations"
    migrations = sorted(migration_dir.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"no workflow migrations found in {migration_dir}")
    for migration in migrations:
        await PostgresWorkflowRepository.migrate(dsn, migration)
        print(f"applied {migration.name}")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
