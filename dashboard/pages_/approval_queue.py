import streamlit as st

st.header("Approval queue (coming in Phase 7)")
st.info(
    "Approving or rejecting invoices arrives with the LangGraph human-in-the-loop step "
    "(Phases 7-8). This workbench is read-only and cannot start a payment."
)
st.button("Approve", disabled=True)
st.button("Reject", disabled=True)
