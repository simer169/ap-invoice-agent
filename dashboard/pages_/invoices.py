import data
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

AMBER = "background-color: #ffe08a; color: #3b2f00"

st.header("Extracted invoices")
rows = data.list_extracted_invoices()
if not rows:
    st.info("No extractions yet. Run: uv run python -m ap_agent.extract --pending")
    st.stop()


def _flags(r) -> str:  # type: ignore[no-untyped-def]
    flags = []
    if r.error:
        flags.append("ERROR")
    if r.date_ambiguous:
        flags.append("DATE AMBIGUOUS")
    if r.missing_po:
        flags.append("NO PO")
    return ", ".join(flags)


df = pd.DataFrame(
    [
        {
            "vendor": r.vendor_name,
            "invoice #": r.invoice_number,
            "date": r.invoice_date,
            "PO #": r.po_number,
            "currency": r.currency,
            "total": float(r.total) if r.total is not None else None,
            "method": r.method,
            "cost_usd": r.cost_usd,
            "date_ambiguous": r.date_ambiguous,
            "flags": _flags(r),
        }
        for r in rows
    ]
)


def _highlight(row: pd.Series):  # type: ignore[type-arg]
    return [AMBER if row["flags"] else ""] * len(row)


event = st.dataframe(
    df.style.apply(_highlight, axis=1),
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={"total": st.column_config.NumberColumn(format="%.2f")},
)
st.caption("Amber rows need a look: ambiguous date, missing PO, or an extraction error.")

selected = event.selection.rows
if not selected:
    st.write("Select a row to see the PDF next to the extracted fields.")
    st.stop()

r = rows[selected[0]]
left, right = st.columns(2)
with left:
    st.subheader("PDF")
    if r.s3_key:
        try:
            url = data.pdf_url(r.s3_key)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not create a PDF link ({type(exc).__name__}).")
        else:
            st.markdown(f"[Open PDF in a new tab]({url}) (link valid 10 minutes)")
            components.iframe(url, height=800, scrolling=True)
    else:
        st.info("No S3 key recorded for this PDF.")
with right:
    st.subheader("Extracted fields")
    if r.error:
        st.error(r.error)
    if r.date_ambiguous:
        st.warning(
            f"Date printed as {r.invoice_date_raw!r} could be day-first or month-first; "
            f"read as {r.invoice_date}. Please verify."
        )
    if r.missing_po:
        st.warning("No PO number on this invoice.")
    st.write(
        {
            "vendor": r.vendor_name,
            "invoice #": r.invoice_number,
            "date (as printed)": r.invoice_date_raw,
            "date (read as)": str(r.invoice_date),
            "PO #": r.po_number,
            "currency": r.currency,
            "total": str(r.total),
            "payment terms": r.invoice.payment_terms if r.invoice else None,
            "subtotal": str(r.invoice.subtotal) if r.invoice else None,
            "tax": str(r.invoice.tax) if r.invoice else None,
            "received": str(r.received_at),
            "from": r.email_from,
            "method / model": f"{r.method} / {r.model}",
            "tokens in/out": f"{r.tokens_in} / {r.tokens_out}",
            "cost_usd": r.cost_usd,
            "retries": r.retries,
        }
    )
    if r.invoice:
        st.subheader("Line items")
        st.dataframe(
            pd.DataFrame([li.model_dump(mode="json") for li in r.invoice.line_items]),
            width="stretch",
            hide_index=True,
        )
