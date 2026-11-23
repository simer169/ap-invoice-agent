-- Email watcher outcomes. Safe to run more than once.

ALTER TABLE agent.processed_emails ADD COLUMN IF NOT EXISTS content_hash   text;
ALTER TABLE agent.processed_emails ADD COLUMN IF NOT EXISTS s3_key         text;
ALTER TABLE agent.processed_emails ADD COLUMN IF NOT EXISTS error_message  text;

ALTER TABLE agent.processed_emails DROP CONSTRAINT IF EXISTS processed_emails_outcome_ck;
ALTER TABLE agent.processed_emails ADD CONSTRAINT processed_emails_outcome_ck
    CHECK (outcome IN ('PROCESSED', 'DUPLICATE_CONTENT', 'IGNORED', 'ERROR'));

-- Duplicate-content lookup: has this PDF hash been seen before?
CREATE INDEX IF NOT EXISTS processed_emails_content_hash_idx
    ON agent.processed_emails (content_hash) WHERE content_hash IS NOT NULL;
