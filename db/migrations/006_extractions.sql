-- Cache of LLM invoice extractions, keyed by PDF content hash. Safe to run more than once.

CREATE TABLE IF NOT EXISTS agent.extractions (
    content_hash   text PRIMARY KEY,
    model          text NOT NULL,
    method         text NOT NULL,
    extracted_json jsonb NOT NULL,
    tokens_in      integer NOT NULL DEFAULT 0,
    tokens_out     integer NOT NULL DEFAULT 0,
    cost_usd       numeric(12,6),
    retries        integer NOT NULL DEFAULT 0,
    duration_ms    integer NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT extractions_method_ck CHECK (method IN ('text', 'vision'))
);
