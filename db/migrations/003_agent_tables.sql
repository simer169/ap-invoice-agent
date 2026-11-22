-- Agent's own state. Kept separate from trex (the system of record).

CREATE TABLE IF NOT EXISTS agent.processed_emails (
    message_id   text PRIMARY KEY,
    received_at  timestamptz,
    from_address text,
    subject      text,
    outcome      text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT processed_emails_outcome_ck CHECK (outcome IN ('PROCESSED', 'IGNORED', 'ERROR'))
);

CREATE TABLE IF NOT EXISTS agent.parked_invoices (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id      text NOT NULL,
    content_hash   text NOT NULL,
    s3_key         text,
    vendor_id      bigint REFERENCES trex.vendors (id) ON DELETE SET NULL,
    po_number      text,
    invoice_number text,
    invoice_date   date,
    due_date       date,
    currency       text,
    total          numeric(12,2),
    status         text NOT NULL DEFAULT 'WAITING',
    extracted_json jsonb,
    anomalies      jsonb,
    parked_at      timestamptz NOT NULL DEFAULT now(),
    ready_at       timestamptz,
    decided_by     text,
    decided_at     timestamptz,
    CONSTRAINT parked_invoices_thread_uq UNIQUE (thread_id),
    CONSTRAINT parked_invoices_hash_uq UNIQUE (content_hash),
    CONSTRAINT parked_invoices_status_ck
        CHECK (status IN ('WAITING', 'READY', 'EXCEPTION', 'ATTACHED', 'REJECTED')),
    CONSTRAINT parked_invoices_currency_ck CHECK (currency IS NULL OR currency IN ('CAD', 'USD'))
);
-- Same vendor cannot park the same invoice number twice (only when the vendor is known).
CREATE UNIQUE INDEX IF NOT EXISTS parked_invoices_vendor_invoice_uq
    ON agent.parked_invoices (vendor_id, invoice_number) WHERE vendor_id IS NOT NULL;
-- Queue/dashboard filtering by status (e.g. all WAITING invoices awaiting receipt).
CREATE INDEX IF NOT EXISTS parked_invoices_status_idx ON agent.parked_invoices (status);
-- Payment-due sweeps: which parked invoices are due soonest.
CREATE INDEX IF NOT EXISTS parked_invoices_due_date_idx ON agent.parked_invoices (due_date);
-- Note: the vendor_id FK index is covered by the partial unique index above only for
-- non-null vendors, which is the only case ON DELETE SET NULL has to find.

CREATE TABLE IF NOT EXISTS agent.audit_log (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    action  text NOT NULL,
    ref     text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);
-- Look up the history of one object (invoice, email) by its reference.
CREATE INDEX IF NOT EXISTS audit_log_ref_idx ON agent.audit_log (ref);
-- Time-ordered browsing of the audit trail.
CREATE INDEX IF NOT EXISTS audit_log_at_idx ON agent.audit_log (at);
