"""PostgreSQL-backed fixed-window API rate limits."""

from __future__ import annotations

import asyncpg

from fundermatch.security.policy import ApiLimitPolicy


class PostgresRateLimiter:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def allow(self, subject: str, category: str, policy: ApiLimitPolicy) -> bool:
        async with self.pool.acquire() as connection:
            count = await connection.fetchval(
                """
                INSERT INTO api_rate_limits
                    (subject, category, window_started_at, request_count)
                VALUES (
                    $1, $2,
                    to_timestamp(floor(extract(epoch FROM now()) / $3) * $3),
                    1
                )
                ON CONFLICT (subject, category, window_started_at)
                DO UPDATE SET request_count = api_rate_limits.request_count + 1
                RETURNING request_count
                """,
                subject,
                category,
                policy.window_seconds,
            )
        return int(count) <= policy.requests

    async def acquire_concurrency(
        self,
        subject: str,
        category: str,
        request_id: str,
        policy: ApiLimitPolicy,
    ) -> bool:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"api:{category}"
            )
            await connection.execute(
                "DELETE FROM api_concurrency_leases WHERE expires_at <= now()"
            )
            count = await connection.fetchval(
                "SELECT count(*) FROM api_concurrency_leases WHERE category = $1",
                category,
            )
            if int(count) >= policy.max_concurrent:
                return False
            await connection.execute(
                """
                INSERT INTO api_concurrency_leases
                    (request_id, subject, category, expires_at)
                VALUES ($1, $2, $3, now() + make_interval(secs => $4))
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                subject,
                category,
                policy.concurrency_lease_seconds,
            )
            return True

    async def release_concurrency(self, request_id: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM api_concurrency_leases WHERE request_id = $1", request_id
            )
