import data
import pandas as pd
import streamlit as st

st.header("Inbox")
counts = data.inbox_counts()
cols = st.columns(len(counts) + 1)
cols[0].metric("All", sum(counts.values()))
for col, (outcome, n) in zip(cols[1:], counts.items(), strict=False):
    col.metric(outcome, n)

choice = st.selectbox("Outcome", ["All", *counts])
rows = data.list_inbox(None if choice == "All" else choice)
df = pd.DataFrame(
    [
        {
            "received_at": r.received_at,
            "from": r.from_address,
            "subject": r.subject,
            "outcome": r.outcome,
            "error": r.error_message,
        }
        for r in rows
    ]
)
st.dataframe(df, width="stretch", hide_index=True)
st.caption(f"{len(rows)} email(s)")
