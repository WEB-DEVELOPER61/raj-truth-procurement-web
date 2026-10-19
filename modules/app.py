import streamlit as st
import pandas as pd
from modules.audit_engine import run_statewide_pipeline

st.set_page_config(page_title="Project TRUTH | Statewide Audit", layout="wide")

@st.cache_data
def load():
    return run_statewide_pipeline("data/districts")

df, state_root, dist_roots = load()

st.title("Project TRUTH: Rajasthan Statewide Forensic Audit")
st.caption("Forensic compliance audit under RTPP Act Rule 43")

if df.empty:
    st.warning("No data found in data/districts/. Add district CSVs.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Tenders", f"{len(df):,}")
c2.metric("Rule 43 Violations", f"{df['is_violation'].sum():,}")
c3.metric("Anomalies", f"{df['is_anomaly'].sum():,}")
c4.metric("Districts Audited", f"{len(dist_roots)}")

st.info(f"**State Master Merkle Root:** `{state_root}`")
dists = ["All Rajasthan"] + sorted(df["district"].unique().tolist())
sel = st.sidebar.selectbox("Filter Circle", dists)
view = df if sel == "All Rajasthan" else df[df["district"] == sel]

st.dataframe(view[[
    "tender_id", "district", "tender_amount", "published_duration_days",
    "required_days", "time_deficit_days", "is_violation", "is_anomaly"
]], use_container_width=True)
