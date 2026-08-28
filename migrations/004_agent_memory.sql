CREATE SCHEMA IF NOT EXISTS langgraph;

CREATE TABLE IF NOT EXISTS langgraph.memory_threads (
    thread_id text PRIMARY KEY,
    application_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN (
        'running', 'failed_retryable', 'needs_attention', 'waiting_for_review',
        'completed', 'cancelled', 'failed_terminal'
    )),
    current_node text,
    last_error jsonb,
    is_stale boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    terminal_at timestamptz,
    delete_after timestamptz,
    CONSTRAINT memory_thread_is_application CHECK (thread_id = application_id),
    CONSTRAINT terminal_retention_consistent CHECK (
        (status IN ('completed', 'cancelled', 'failed_terminal')
            AND terminal_at IS NOT NULL AND delete_after IS NOT NULL)
        OR
        (status NOT IN ('completed', 'cancelled', 'failed_terminal')
            AND terminal_at IS NULL AND delete_after IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS memory_threads_cleanup
ON langgraph.memory_threads (delete_after)
WHERE delete_after IS NOT NULL;

CREATE INDEX IF NOT EXISTS memory_threads_stale
ON langgraph.memory_threads (updated_at)
WHERE status IN ('running', 'failed_retryable', 'needs_attention', 'waiting_for_review');
