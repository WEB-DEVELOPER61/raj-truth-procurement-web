import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from scipy.stats import fisher_exact
from modules.audit_engine import run_statewide_pipeline

st.set_page_config(
    page_title="Project TRUTH | Statewide Forensic Audit",
    page_icon="⚖️",
    layout="wide"
)

# Custom Styling
st.markdown("""
<style>
    .metric-card {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 15px;
        border-left: 5px solid #1f77b4;
    }
</style>
""", unsafe_allow_html=True)

@st.cache_data
def load_audit_data():
    return run_statewide_pipeline("data/districts")

df, state_master_root, district_roots = load_audit_data()

st.title("Project TRUTH: Rajasthan Statewide Forensic Procurement Audit")
st.caption("Forensic statutory timeline audit under Rajasthan Transparency in Public Procurement (RTPP) Act Rule 43")

if df.empty:
    st.warning("No district files detected in data/districts/. Add CSV files to begin processing.")
    st.stop()

# --- TOP METRIC CARDS ---
c1, c2, c3, c4 = st.columns(4)
total_tenders = len(df)
rule43_violations = int(df["is_violation"].sum())
anomalies_detected = int(df["is_anomaly"].sum())
districts_count = len(district_roots)

c1.metric("Total State Tenders", f"{total_tenders:,}")
c2.metric("Rule 43 Violations", f"{rule43_violations:,}", delta=f"{(rule43_violations/total_tenders)*100:.1f}% flag rate", delta_color="inverse")
c3.metric("3D ML Anomalies", f"{anomalies_detected:,}", delta="Isolation Forest", delta_color="off")
c4.metric("Districts Ingested", f"{districts_count}")

# --- CRYPTOGRAPHIC STATE ANCHOR ---
st.markdown("---")
st.subheader("RFC 6962 Hierarchical Merkle Root Verification")
st.info(f"**State Master Merkle Root:** `{state_master_root}`")

# Sidebar Filter Circle
district_list = ["All Rajasthan"] + sorted(df["district"].unique().tolist())
selected_district = st.sidebar.selectbox("Select Administrative Circle", district_list)

display_df = df if selected_district == "All Rajasthan" else df[df["district"] == selected_district]

if selected_district != "All Rajasthan":
    st.sidebar.markdown("---")
    st.sidebar.markdown(f"**Circle Root Hash ({selected_district}):**")
    st.sidebar.code(district_roots.get(selected_district, "N/A"), language="text")

# --- 3D ISOLATION FOREST VISUALIZATION ---
st.markdown("---")
st.subheader("3D Unsupervised Anomaly Detection Landscape")
st.caption("Feature space: Tender Value (INR) vs. Publication Duration (Days) vs. Timeline Deficit (Days)")

plot_df = display_df.copy()
plot_df["Classification"] = plot_df["is_anomaly"].apply(lambda x: "Forensic Anomaly" if x else "Standard Notice")

fig_3d = px.scatter_3d(
    plot_df,
    x="published_duration_days",
    y="tender_amount",
    z="time_deficit_days",
    color="Classification",
    color_discrete_map={"Standard Notice": "#1f77b4", "Forensic Anomaly": "#d62728"},
    hover_name="tender_id",
    hover_data=["district", "required_days", "is_violation"],
    labels={
        "published_duration_days": "Duration (Days)",
        "tender_amount": "Amount (INR)",
        "time_deficit_days": "Deficit (Days)"
    },
    opacity=0.85
)
fig_3d.update_layout(margin=dict(l=0, r=0, b=0, t=20), height=550)
st.plotly_chart(fig_3d, use_container_width=True)

# --- STATISTICAL CORRELATION & CONTINGENCY MATRIX ---
st.markdown("---")
st.subheader("Empirical Statistical Significance (Fisher's Exact Test)")

contingency_table = pd.crosstab(
    display_df["is_violation"], 
    display_df["is_anomaly"], 
    rownames=["Rule 43 Violation"], 
    colnames=["Isolation Forest Anomaly"]
)

col_stat1, col_stat2 = st.columns([1, 2])
with col_stat1:
    st.markdown("**2x2 Contingency Matrix**")
    st.dataframe(contingency_table, use_container_width=True)

with col_stat2:
    if contingency_table.shape == (2, 2):
        odds_ratio, p_value = fisher_exact(contingency_table)
        st.markdown(f"**Fisher's Exact Test p-value:** `{p_value:.6e}`")
        if p_value < 0.05:
            st.success("Statistically Significant (p < 0.05): Timeline compression is non-randomly clustered around high-risk procurements.")
        else:
            st.warning("Sample size too small or null hypothesis cannot be rejected at p < 0.05.")
    else:
        st.write("Full 2x2 matrix will populate once more records with varied outcomes are added.")

# --- FORENSIC AUDIT TABLE ---
st.markdown("---")
st.subheader(f"Statutory Audit Records ({selected_district})")
st.dataframe(
    display_df[[
        "tender_id", "district", "tender_amount", "published_duration_days",
        "required_days", "time_deficit_days", "is_violation", "is_anomaly"
    ]].sort_values(by="time_deficit_days", ascending=False),
    use_container_width=True
)

# --- SECTION 6 RTI DRAFT PETITION GENERATOR ---
st.markdown("---")
st.subheader("Automated Statutory Section 6 RTI Petition Generator")
flagged_tenders = display_df[display_df["is_violation"]]["tender_id"].tolist()

if flagged_tenders:
    target_tender = st.selectbox("Select Flagged Notice for RTI Generation", flagged_tenders)
    target_row = display_df[display_df["tender_id"] == target_tender].iloc[0]

    rti_text = f"""To,
The Public Information Officer (PIO)
Public Works Department (PWD), Circle: {target_row['district']}
Government of Rajasthan.

Subject: Formal Request for Information under Section 6(1) of the Right to Information Act, 2005.

Ref: Tender Notice ID: {target_row['tender_id']} (Estimated Value: INR {target_row['tender_amount']:,.2f})

Sir/Madam,

Pursuant to Rule 43 of the Rajasthan Transparency in Public Procurement (RTPP) Rules, 2013, procurement notices with estimated contract values exceeding statutory thresholds mandate a minimum publication window of {target_row['required_days']} calendar days.

Empirical verification of official dispatch logs reveals this tender was closed after only {target_row['published_duration_days']} days, establishing a statutory timeline deficit of {target_row['time_deficit_days']} days.

Under Section 6(1) of the RTI Act, 2005, please furnish certified records for:
1. The sanctioned dispatch order authorizing compressed publication duration under Rule 43 proviso.
2. Inward and outward registry logs for physical state-level newspaper distribution.
3. Server access logs recording the timestamp of initial tender publication versus bid submission cutoff.

Cryptographic Verification Anchor (State Merkle Root): {state_master_root}
Audit Fingerprint: {district_roots.get(str(target_row['district']), state_master_root)[:32]}...
"""
    st.text_area("Formulated Section 6 RTI Draft", rti_text, height=300)
else:
    st.success("No statutory timeline violations found in this view.")
