import data
import pandas as pd
import streamlit as st

st.header("Purchase orders")
vendors = {v.name: v.id for v in data.list_vendor_options()}
c1, c2 = st.columns(2)
status = c1.selectbox("Status", ["All", *data.list_po_statuses()])
vendor = c2.selectbox("Vendor", ["All", *vendors])

pos = data.list_purchase_orders(
    None if status == "All" else status, None if vendor == "All" else vendors[vendor]
)
df = pd.DataFrame(
    [
        {
            "PO #": p.po_number,
            "vendor": p.vendor_name,
            "order date": p.order_date,
            "status": p.status,
            "fully received": p.fully_received,
            "lines received": f"{p.lines_fully_received}/{p.lines_total}",
            "currency": p.currency,
            "total": float(p.total),
        }
        for p in pos
    ]
)
event = st.dataframe(
    df,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={"total": st.column_config.NumberColumn(format="%.2f")},
)
st.caption(f"{len(pos)} purchase order(s)")

if not event.selection.rows:
    st.write("Select a PO to see its lines, receipts and matching invoices.")
    st.stop()

detail = data.get_po_detail(pos[event.selection.rows[0]].po_number)
if detail is None:
    st.warning("PO not found.")
    st.stop()

st.subheader(f"{detail.summary.po_number}: {detail.summary.vendor_name}")
st.markdown("**Lines: ordered vs received**")
st.dataframe(
    pd.DataFrame(
        [
            {
                "line": ln.line_no,
                "part": ln.part_no,
                "description": ln.description,
                "unit price": float(ln.unit_price),
                "ordered": float(ln.qty_ordered),
                "received": float(ln.qty_received),
                "outstanding": float(max(ln.qty_ordered - ln.qty_received, 0)),
                "fully received": ln.fully_received,
            }
            for ln in detail.lines
        ]
    ),
    width="stretch",
    hide_index=True,
)

st.markdown("**Receiving reports**")
if not detail.receiving_reports:
    st.write("Nothing received yet.")
for rr in detail.receiving_reports:
    with st.expander(f"{rr.rr_number}: {rr.received_at:%Y-%m-%d %H:%M} by {rr.received_by}"):
        st.dataframe(
            pd.DataFrame(
                [
                    {"line": ln.line_no, "part": ln.part_no, "qty received": float(ln.qty_received)}
                    for ln in rr.lines
                ]
            ),
            width="stretch",
            hide_index=True,
        )

st.markdown("**Extracted invoices mentioning this PO**")
if not detail.invoices:
    st.write("None.")
else:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "invoice #": i.invoice_number,
                    "date": i.invoice_date,
                    "currency": i.currency,
                    "total": float(i.total) if i.total is not None else None,
                    "date ambiguous": i.date_ambiguous,
                }
                for i in detail.invoices
            ]
        ),
        width="stretch",
        hide_index=True,
    )
