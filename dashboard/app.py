"""AP clerk workbench (read-only). Run: uv run streamlit run dashboard/app.py"""

import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (str(HERE.parent), str(HERE)):  # project root (data_gen) and this folder (data.py)
    if p not in sys.path:
        sys.path.insert(0, p)

import data
import streamlit as st

st.set_page_config(page_title="AP Workbench", layout="wide")

with st.sidebar:
    st.title("AP Workbench")
    st.caption("Read-only. All data is fake.")
    today = data.demo_today()
    if today:
        st.info(f"Demo date: {today.isoformat()}")
    if st.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

    try:
        demo_enabled = data.demo_mode()
    except Exception:  # noqa: BLE001 - settings problems are reported by the pages
        demo_enabled = False
    if demo_enabled:
        st.divider()
        st.subheader("Dev only: simulate")
        day = st.date_input("Apply deliveries up to", value=today or date.today())
        if st.button("Run simulation"):
            try:
                changes = data.simulate_day(day)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Simulation failed: {type(exc).__name__}")
            else:
                st.cache_data.clear()
                st.success(f"{len(changes)} change(s) applied" if changes else "Nothing to apply")
                if changes:
                    st.code("\n".join(changes))

pages = [
    st.Page("pages_/inbox.py", title="Inbox", icon=":material/inbox:", default=True),
    st.Page("pages_/invoices.py", title="Invoices", icon=":material/receipt_long:"),
    st.Page("pages_/purchase_orders.py", title="Purchase orders", icon=":material/package_2:"),
    st.Page("pages_/vendors.py", title="Vendors", icon=":material/storefront:"),
    st.Page("pages_/approval_queue.py", title="Approval queue (Phase 7)", icon=":material/lock:"),
]
st.navigation(pages).run()
