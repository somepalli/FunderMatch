"""PostgreSQL setup for the dedicated LangGraph checkpoint schema."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from fundermatch.security.secrets import read_secret


def checkpoint_dsn(dsn: str) -> str:
    """Force all unqualified LangGraph checkpointer SQL into its own schema."""
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("FUNDERMATCH_DATABASE_URL must be a PostgreSQL URL")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "options" in query:
        raise ValueError("database URL already contains PostgreSQL options")
    query["options"] = "-csearch_path=langgraph,public"
    return urlunsplit(parsed._replace(query=urlencode(query)))


@asynccontextmanager
async def open_checkpointer(
    dsn: str,
    *,
    setup: bool = False,
) -> AsyncIterator[AsyncPostgresSaver]:
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(dsn)) as saver:
        if setup:
            await saver.setup()
        yield saver


async def setup_memory(dsn: str, migration_path: Path | None = None) -> None:
    root = Path(__file__).parents[3]
    migration = migration_path or root / "migrations" / "004_agent_memory.sql"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(migration.read_text(encoding="utf-8"))
    finally:
        await connection.close()
    async with open_checkpointer(dsn, setup=True):
        pass


def main() -> None:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(setup_memory(read_secret("FUNDERMATCH_DATABASE_URL")))
