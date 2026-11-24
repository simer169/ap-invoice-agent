import data
import pandas as pd
import streamlit as st

st.header("Vendors")


def _f(x):  # type: ignore[no-untyped-def]
    return float(x) if x is not None else None


rows = data.list_vendors()
st.dataframe(
    pd.DataFrame(
        [
            {
                "code": v.vendor_code,
                "vendor": v.name,
                "terms (days)": v.payment_terms_days,
                "currency": v.currency,
                "discount %": float(v.discount_pct),
                "discount days": v.discount_days,
                "active": v.active,
                "invoices": v.invoice_count,
                "avg total": _f(v.avg_total),
                "min total": _f(v.min_total),
                "max total": _f(v.max_total),
            }
            for v in rows
        ]
    ),
    width="stretch",
    hide_index=True,
    column_config={
        c: st.column_config.NumberColumn(format="%.2f")
        for c in ("avg total", "min total", "max total")
    },
)
st.caption("Invoice stats come from the vendor's invoice history.")
