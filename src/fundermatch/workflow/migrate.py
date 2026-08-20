"""Apply the idempotent HITL workflow migration."""

import asyncio
import os
from pathlib import Path

from fundermatch.workflow.postgres import PostgresWorkflowRepository


async def run() -> None:
    dsn = os.environ["FUNDERMATCH_DATABASE_URL"]
    migration = Path(__file__).resolve().parents[3] / "migrations" / "001_hitl_workflow.sql"
    await PostgresWorkflowRepository.migrate(dsn, migration)
    print("workflow migration applied")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
