-- LangGraph checkpoint tables live in their own schema (created by PostgresSaver.setup(), which
-- scripts/migrate.py calls with search_path=langgraph). Safe to run more than once.
CREATE SCHEMA IF NOT EXISTS langgraph;

-- A reissue (same vendor + invoice number, different PDF) is parked as an EXCEPTION next to the
-- original, so the uniqueness rule only applies to live (non-exception, non-rejected) rows.
DROP INDEX IF EXISTS agent.parked_invoices_vendor_invoice_uq;
CREATE UNIQUE INDEX IF NOT EXISTS parked_invoices_vendor_invoice_uq
    ON agent.parked_invoices (vendor_id, invoice_number)
    WHERE vendor_id IS NOT NULL AND status NOT IN ('EXCEPTION', 'REJECTED');
