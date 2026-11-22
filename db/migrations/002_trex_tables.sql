-- Mock Octopos purchasing system. ALL data is fake.

CREATE TABLE IF NOT EXISTS trex.vendors (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor_code        text NOT NULL,
    name               text NOT NULL,
    email_domain       text,
    currency           text NOT NULL DEFAULT 'CAD',
    payment_terms_days integer NOT NULL DEFAULT 30,
    discount_pct       numeric(5,2) NOT NULL DEFAULT 0,
    discount_days      integer NOT NULL DEFAULT 0,
    bank_ref           text,
    active             boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT vendors_code_uq UNIQUE (vendor_code),
    CONSTRAINT vendors_currency_ck CHECK (currency IN ('CAD', 'USD')),
    CONSTRAINT vendors_terms_ck CHECK (payment_terms_days >= 0),
    CONSTRAINT vendors_discount_pct_ck CHECK (discount_pct >= 0 AND discount_pct <= 100),
    CONSTRAINT vendors_discount_days_ck CHECK (discount_days >= 0)
);

CREATE TABLE IF NOT EXISTS trex.purchase_orders (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    po_number  text NOT NULL,
    vendor_id  bigint NOT NULL REFERENCES trex.vendors (id) ON DELETE RESTRICT,
    order_date date NOT NULL,
    status     text NOT NULL DEFAULT 'OPEN',
    currency   text NOT NULL DEFAULT 'CAD',
    total      numeric(12,2) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT purchase_orders_po_number_uq UNIQUE (po_number),
    CONSTRAINT purchase_orders_status_ck CHECK (status IN ('OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED')),
    CONSTRAINT purchase_orders_currency_ck CHECK (currency IN ('CAD', 'USD'))
);
-- FK index: list a vendor's POs; also speeds up vendor deletes (RESTRICT check).
CREATE INDEX IF NOT EXISTS purchase_orders_vendor_id_idx ON trex.purchase_orders (vendor_id);

CREATE TABLE IF NOT EXISTS trex.po_lines (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    po_id       bigint NOT NULL REFERENCES trex.purchase_orders (id) ON DELETE CASCADE,
    line_no     integer NOT NULL,
    part_no     text NOT NULL,
    description text,
    qty_ordered numeric(12,3) NOT NULL,
    unit_price  numeric(12,2) NOT NULL,
    CONSTRAINT po_lines_po_line_uq UNIQUE (po_id, line_no),
    CONSTRAINT po_lines_qty_ck CHECK (qty_ordered > 0),
    CONSTRAINT po_lines_price_ck CHECK (unit_price >= 0)
);
-- FK index on po_id is provided by the unique (po_id, line_no) constraint (leading column).

CREATE TABLE IF NOT EXISTS trex.receiving_reports (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rr_number   text NOT NULL,
    po_id       bigint NOT NULL REFERENCES trex.purchase_orders (id) ON DELETE CASCADE,
    received_at timestamptz NOT NULL DEFAULT now(),
    received_by text,
    CONSTRAINT receiving_reports_rr_number_uq UNIQUE (rr_number)
);
-- FK index: find all receipts for a PO (three-way match).
CREATE INDEX IF NOT EXISTS receiving_reports_po_id_idx ON trex.receiving_reports (po_id);

CREATE TABLE IF NOT EXISTS trex.receiving_lines (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rr_id        bigint NOT NULL REFERENCES trex.receiving_reports (id) ON DELETE CASCADE,
    po_line_id   bigint NOT NULL REFERENCES trex.po_lines (id) ON DELETE RESTRICT,
    qty_received numeric(12,3) NOT NULL,
    CONSTRAINT receiving_lines_qty_ck CHECK (qty_received > 0)
);
-- FK index: load the lines of a receiving report.
CREATE INDEX IF NOT EXISTS receiving_lines_rr_id_idx ON trex.receiving_lines (rr_id);
-- FK index: sum received qty per PO line (used by the receipt views).
CREATE INDEX IF NOT EXISTS receiving_lines_po_line_id_idx ON trex.receiving_lines (po_line_id);

CREATE TABLE IF NOT EXISTS trex.invoices (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor_id      bigint NOT NULL REFERENCES trex.vendors (id) ON DELETE RESTRICT,
    po_id          bigint REFERENCES trex.purchase_orders (id) ON DELETE RESTRICT,
    invoice_number text NOT NULL,
    invoice_date   date NOT NULL,
    currency       text NOT NULL DEFAULT 'CAD',
    total          numeric(12,2) NOT NULL,
    status         text NOT NULL DEFAULT 'PENDING',
    attached_at    timestamptz,
    CONSTRAINT invoices_vendor_number_uq UNIQUE (vendor_id, invoice_number),
    CONSTRAINT invoices_status_ck CHECK (status IN ('PENDING', 'ATTACHED', 'PAID', 'VOID')),
    CONSTRAINT invoices_currency_ck CHECK (currency IN ('CAD', 'USD'))
);
-- FK index on vendor_id is provided by the unique (vendor_id, invoice_number) constraint.
-- FK index: find invoices attached to a PO.
CREATE INDEX IF NOT EXISTS invoices_po_id_idx ON trex.invoices (po_id);

CREATE TABLE IF NOT EXISTS trex.vendor_invoice_history (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor_id      bigint NOT NULL REFERENCES trex.vendors (id) ON DELETE RESTRICT,
    invoice_number text NOT NULL,
    invoice_date   date NOT NULL,
    currency       text NOT NULL DEFAULT 'CAD',
    total          numeric(12,2) NOT NULL,
    line_summary   text,
    CONSTRAINT vendor_invoice_history_currency_ck CHECK (currency IN ('CAD', 'USD'))
);
-- FK index: pull a vendor's past invoices for duplicate / anomaly checks.
CREATE INDEX IF NOT EXISTS vendor_invoice_history_vendor_id_idx
    ON trex.vendor_invoice_history (vendor_id);
