-- Per-line ordered vs received quantities.
CREATE OR REPLACE VIEW trex.po_line_receipt AS
SELECT
    pl.id AS po_line_id,
    pl.po_id,
    pl.line_no,
    pl.part_no,
    pl.qty_ordered,
    COALESCE(SUM(rl.qty_received), 0)::numeric(12,3) AS qty_received,
    COALESCE(SUM(rl.qty_received), 0) >= pl.qty_ordered AS fully_received
FROM trex.po_lines pl
LEFT JOIN trex.receiving_lines rl ON rl.po_line_id = pl.id
GROUP BY pl.id, pl.po_id, pl.line_no, pl.part_no, pl.qty_ordered;

-- Per-PO receipt status: fully_received only when every line is fully received.
CREATE OR REPLACE VIEW trex.po_receipt_status AS
SELECT
    po.id AS po_id,
    po.po_number,
    po.vendor_id,
    COUNT(plr.po_line_id) AS lines_total,
    COUNT(plr.po_line_id) FILTER (WHERE plr.fully_received) AS lines_fully_received,
    (COUNT(plr.po_line_id) > 0
        AND COUNT(plr.po_line_id) FILTER (WHERE plr.fully_received) = COUNT(plr.po_line_id)
    ) AS fully_received
FROM trex.purchase_orders po
LEFT JOIN trex.po_line_receipt plr ON plr.po_id = po.id
GROUP BY po.id, po.po_number, po.vendor_id;
