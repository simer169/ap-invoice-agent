-- Phase 7a: anomaly check. pgvector embeddings (384 dims, BAAI/bge-small-en-v1.5), the structured
-- anomaly report on parked invoices, and a cache for the LLM explanation. Safe to run twice.
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE trex.vendor_invoice_history ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS vendor_invoice_history_embedding_idx
    ON trex.vendor_invoice_history USING hnsw (embedding vector_cosine_ops);

ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS embedding vector(384);
ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS anomaly_report jsonb;

-- One explanation per thread. flags_hash changes when the flags/evidence change, which
-- invalidates the cached text.
CREATE TABLE IF NOT EXISTS agent.anomaly_explanations (
    thread_id   text PRIMARY KEY,
    flags_hash  text NOT NULL,
    explanation text NOT NULL,
    model       text NOT NULL,
    tokens_in   integer,
    tokens_out  integer,
    cost_usd    numeric(12,6),
    created_at  timestamptz NOT NULL DEFAULT now()
);
