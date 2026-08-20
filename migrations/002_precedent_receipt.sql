ALTER TABLE workflow_cases
ADD COLUMN IF NOT EXISTS precedent_receipt jsonb;
