"""DB layer: delete only GEN- rows, insert the generated dataset, count rows.

Nothing here commits; the caller owns the transaction.
"""

import psycopg

from data_gen.generate import GEN_PREFIX, Dataset

_GEN_VENDORS = "SELECT id FROM trex.vendors WHERE starts_with(vendor_code, %(p)s)"


def clean(conn: psycopg.Connection) -> None:
    """Delete rows created by a previous build (GEN- codes) and nothing else.

    Invoices are never deleted: if a non-generated row (e.g. an invoice attached by the agent)
    references a GEN- vendor/PO, the RESTRICT foreign key fails loudly instead.
    """
    p = {"p": GEN_PREFIX}
    conn.execute(f"DELETE FROM trex.vendor_invoice_history WHERE vendor_id IN ({_GEN_VENDORS})", p)
    conn.execute("DELETE FROM trex.receiving_reports WHERE starts_with(rr_number, %(p)s)", p)
    conn.execute("DELETE FROM trex.purchase_orders WHERE starts_with(po_number, %(p)s)", p)
    conn.execute("DELETE FROM trex.vendors WHERE starts_with(vendor_code, %(p)s)", p)


def _one(conn: psycopg.Connection, sql: str, params: dict[str, object]) -> int:
    row = conn.execute(sql, params).fetchone()  # type: ignore[arg-type]
    assert row is not None
    return int(row[0])


def load(conn: psycopg.Connection, ds: Dataset) -> None:
    """Insert the dataset (upserts keyed on the unique codes). Future deliveries are NOT inserted."""
    vendor_ids: dict[str, int] = {}
    for v in ds.vendors:
        vendor_ids[v.vendor_code] = _one(
            conn,
            """
            INSERT INTO trex.vendors (vendor_code, name, email_domain, currency,
                payment_terms_days, discount_pct, discount_days, bank_ref)
            VALUES (%(vendor_code)s, %(name)s, %(email_domain)s, %(currency)s,
                %(payment_terms_days)s, %(discount_pct)s, %(discount_days)s, %(bank_ref)s)
            ON CONFLICT (vendor_code) DO UPDATE SET
                name = EXCLUDED.name, email_domain = EXCLUDED.email_domain,
                currency = EXCLUDED.currency, payment_terms_days = EXCLUDED.payment_terms_days,
                discount_pct = EXCLUDED.discount_pct, discount_days = EXCLUDED.discount_days,
                bank_ref = EXCLUDED.bank_ref
            RETURNING id
            """,
            v.model_dump(exclude={"category"}),
        )

    for po in ds.purchase_orders:
        po_id = _one(
            conn,
            """
            INSERT INTO trex.purchase_orders (po_number, vendor_id, order_date, status,
                currency, total)
            VALUES (%(po_number)s, %(vendor_id)s, %(order_date)s, %(status)s,
                %(currency)s, %(total)s)
            ON CONFLICT (po_number) DO UPDATE SET
                vendor_id = EXCLUDED.vendor_id, order_date = EXCLUDED.order_date,
                status = EXCLUDED.status, currency = EXCLUDED.currency, total = EXCLUDED.total
            RETURNING id
            """,
            {
                "po_number": po.po_number,
                "vendor_id": vendor_ids[po.vendor_code],
                "order_date": po.order_date,
                "status": po.status,
                "currency": po.currency,
                "total": po.total,
            },
        )
        line_ids: dict[int, int] = {}
        for ln in po.lines:
            line_ids[ln.line_no] = _one(
                conn,
                """
                INSERT INTO trex.po_lines (po_id, line_no, part_no, description,
                    qty_ordered, unit_price)
                VALUES (%(po_id)s, %(line_no)s, %(part_no)s, %(description)s,
                    %(qty_ordered)s, %(unit_price)s)
                ON CONFLICT (po_id, line_no) DO UPDATE SET
                    part_no = EXCLUDED.part_no, description = EXCLUDED.description,
                    qty_ordered = EXCLUDED.qty_ordered, unit_price = EXCLUDED.unit_price
                RETURNING id
                """,
                {"po_id": po_id, **ln.model_dump()},
            )
        for r in po.receipts:
            row = conn.execute(
                "INSERT INTO trex.receiving_reports (rr_number, po_id, received_at, received_by) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (rr_number) DO NOTHING RETURNING id",
                (r.rr_number, po_id, r.received_at, r.received_by),
            ).fetchone()
            if row is None:  # already there: lines were inserted with it
                continue
            for rl in r.lines:
                conn.execute(
                    "INSERT INTO trex.receiving_lines (rr_id, po_line_id, qty_received) "
                    "VALUES (%s, %s, %s)",
                    (row[0], line_ids[rl.line_no], rl.qty),
                )

    for h in ds.history:
        conn.execute(
            "INSERT INTO trex.vendor_invoice_history (vendor_id, invoice_number, invoice_date, "
            "currency, total, line_summary) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                vendor_ids[h.vendor_code],
                h.invoice_number,
                h.invoice_date,
                h.currency,
                h.total,
                h.line_summary,
            ),
        )


def counts(conn: psycopg.Connection) -> dict[str, int]:
    """Row counts of GEN- data per table."""
    p = {"p": GEN_PREFIX}
    po_ids = "SELECT id FROM trex.purchase_orders WHERE starts_with(po_number, %(p)s)"
    queries = {
        "vendors": "SELECT count(*) FROM trex.vendors WHERE starts_with(vendor_code, %(p)s)",
        "purchase_orders": f"SELECT count(*) FROM ({po_ids}) x",
        "po_lines": f"SELECT count(*) FROM trex.po_lines WHERE po_id IN ({po_ids})",
        "receiving_reports": f"SELECT count(*) FROM trex.receiving_reports WHERE po_id IN ({po_ids})",
        "receiving_lines": "SELECT count(*) FROM trex.receiving_lines rl JOIN "
        f"trex.receiving_reports rr ON rr.id = rl.rr_id WHERE rr.po_id IN ({po_ids})",
        "vendor_invoice_history": "SELECT count(*) FROM trex.vendor_invoice_history "
        f"WHERE vendor_id IN ({_GEN_VENDORS})",
    }
    return {k: _one(conn, q, p) for k, q in queries.items()}


def status_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) FROM trex.purchase_orders "
        "WHERE starts_with(po_number, %s) GROUP BY status ORDER BY status",
        (GEN_PREFIX,),
    ).fetchall()
    return {str(s): int(n) for s, n in rows}
