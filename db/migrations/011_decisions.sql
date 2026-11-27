-- Phase 7c: human decisions. What the approval UI needs on the parked row (it reads SQL only),
-- plus the decision and its note. Safe to run twice.

ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS po_id bigint;
-- Vendor/PO match details and candidates, receipt summary: shown on the approval card.
ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS review_json jsonb;
ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS decision text;
ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS decision_note text;

-- "Decided (last 50)" tab.
CREATE INDEX IF NOT EXISTS parked_invoices_decided_at_idx ON agent.parked_invoices (decided_at);
