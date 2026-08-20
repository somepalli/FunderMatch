CREATE TABLE IF NOT EXISTS workflow_cases (
    application_id text PRIMARY KEY,
    state text NOT NULL CHECK (state IN (
        'INTAKE', 'EXTRACTED', 'RULE_GATED', 'AI_SUGGESTED',
        'AWAITING_HUMAN', 'HUMAN_DECIDED', 'PRECEDENT_WRITTEN'
    )),
    version integer NOT NULL CHECK (version >= 0),
    suggestion jsonb,
    decision jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_audit (
    audit_id uuid PRIMARY KEY,
    command_id uuid NOT NULL,
    application_id text NOT NULL REFERENCES workflow_cases(application_id),
    sequence integer NOT NULL CHECK (sequence >= 1),
    actor_id text NOT NULL,
    actor_display_name text NOT NULL,
    actor_roles text[] NOT NULL,
    from_state text,
    to_state text NOT NULL,
    action text NOT NULL,
    reason text NOT NULL,
    changes jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    UNIQUE (application_id, sequence),
    UNIQUE (application_id, command_id)
);

CREATE TABLE IF NOT EXISTS workflow_commands (
    application_id text NOT NULL REFERENCES workflow_cases(application_id),
    command_id uuid NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (application_id, command_id)
);

CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'workflow audit is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workflow_audit_append_only ON workflow_audit;
CREATE TRIGGER workflow_audit_append_only
BEFORE UPDATE OR DELETE ON workflow_audit
FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();
