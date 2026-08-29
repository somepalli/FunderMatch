CREATE TABLE IF NOT EXISTS guardrail_outbox (
    command_id uuid PRIMARY KEY,
    application_id text NOT NULL,
    operation text NOT NULL,
    payload_hash char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempts integer NOT NULL DEFAULT 0,
    receipt jsonb,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS guardrail_outbox_pending_idx
    ON guardrail_outbox (status, created_at);

CREATE TABLE IF NOT EXISTS api_rate_limits (
    subject text NOT NULL,
    category text NOT NULL,
    window_started_at timestamptz NOT NULL,
    request_count integer NOT NULL,
    PRIMARY KEY (subject, category, window_started_at)
);

CREATE TABLE IF NOT EXISTS api_concurrency_leases (
    request_id text PRIMARY KEY,
    subject text NOT NULL,
    category text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS api_concurrency_leases_active_idx
    ON api_concurrency_leases (category, expires_at);

CREATE TABLE IF NOT EXISTS sensitive_reveal_audit (
    command_id uuid PRIMARY KEY,
    application_id text NOT NULL,
    actor_id text NOT NULL,
    field_name text NOT NULL,
    reason text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retention_tombstones (
    application_id text PRIMARY KEY,
    policy_hash char(64) NOT NULL,
    artifact_hashes jsonb NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS precedent_lifecycle (
    case_id text PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('active', 'revoked', 'superseded')),
    policy_hash char(64) NOT NULL,
    valid_until timestamptz,
    supersedes_case_id text,
    command_id uuid NOT NULL UNIQUE,
    reason text NOT NULL,
    actor_id text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS key_rotation_events (
    event_id uuid PRIMARY KEY,
    old_key_version text NOT NULL,
    new_key_version text NOT NULL,
    object_count integer NOT NULL CHECK (object_count >= 0),
    policy_hash char(64) NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
