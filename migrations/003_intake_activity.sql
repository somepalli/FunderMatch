CREATE TABLE IF NOT EXISTS intake_jobs (
    job_id text PRIMARY KEY,
    application_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    last_sequence integer NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    error_code text,
    retryable boolean,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS intake_jobs_active_application
ON intake_jobs (application_id)
WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS intake_job_events (
    job_id text NOT NULL REFERENCES intake_jobs(job_id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence >= 1),
    stage text NOT NULL,
    message text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (job_id, sequence)
);

CREATE INDEX IF NOT EXISTS intake_job_events_time
ON intake_job_events (job_id, occurred_at);
