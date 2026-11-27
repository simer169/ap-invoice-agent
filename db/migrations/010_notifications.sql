-- Phase 7b: in-app notification inbox and the discount deadline on parked invoices.
-- Safe to run twice.

CREATE TABLE IF NOT EXISTS agent.notifications (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    thread_id  text,
    kind       text NOT NULL,
    title      text NOT NULL,
    body       text NOT NULL,
    read_at    timestamptz
);
-- Bell/inbox: unread first, newest first.
CREATE INDEX IF NOT EXISTS notifications_inbox_idx ON agent.notifications (read_at, created_at DESC);
-- A thread is woken by its PO being received only once, so it is announced only once.
CREATE UNIQUE INDEX IF NOT EXISTS notifications_po_received_uq
    ON agent.notifications (thread_id, kind) WHERE kind = 'PO_RECEIVED';

-- The discount alert needs the deadline in SQL (it used to live only in graph state).
ALTER TABLE agent.parked_invoices ADD COLUMN IF NOT EXISTS discount_deadline date;
CREATE INDEX IF NOT EXISTS parked_invoices_discount_deadline_idx
    ON agent.parked_invoices (discount_deadline);

-- Backfill rows parked before this migration (vendor terms as they are now).
UPDATE agent.parked_invoices p
SET discount_deadline = p.invoice_date + v.discount_days
FROM trex.vendors v
WHERE v.id = p.vendor_id AND p.discount_deadline IS NULL AND p.invoice_date IS NOT NULL
  AND v.discount_pct > 0 AND v.discount_days > 0;
