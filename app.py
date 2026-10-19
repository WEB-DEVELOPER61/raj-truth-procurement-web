import os
import glob
import time
import math
import hashlib
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from scipy import stats
from sklearn.ensemble import IsolationForest

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION & STYLES
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Project TRUTH | Forensic Procurement Engine",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .metric-card {
        background-color: #1e2130;
        padding: 18px;
        border-radius: 8px;
        border: 1px solid #2d3250;
        text-align: center;
    }
    .metric-title { color: #8b949e; font-size: 13px; font-weight: 600; text-transform: uppercase; margin-bottom: 6px; }
    .metric-val { color: #f0f6fc; font-size: 28px; font-weight: 700; }
    .metric-sub-red { color: #f85149; font-size: 12px; font-weight: 600; margin-top: 4px; }
    .metric-sub-green { color: #3fb950; font-size: 12px; font-weight: 600; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 2. CRYPTOGRAPHIC MERKLE TREE (RFC 6962 COMPLIANT)
# -----------------------------------------------------------------------------
class RFC6962MerkleTree:
    """
    Standard RFC 6962 Certificate Transparency Merkle Tree.
    Prefixes: 0x00 for Leaf nodes, 0x01 for Internal nodes.
    Prevents second-preimage collision attacks.
    """
    def __init__(self, data_list):
        self.leaf_prefix = b'\x00'
        self.node_prefix = b'\x01'
        self.leaves = [self._hash_leaf(str(x).encode('utf-8')) for x in data_list]
        self.levels = [self.leaves]
        self.root = self._build()

    def _hash_leaf(self, b_data: bytes) -> bytes:
        return hashlib.sha256(self.leaf_prefix + b_data).digest()

    def _hash_nodes(self, left: bytes, right: bytes) -> bytes:
        return hashlib.sha256(self.node_prefix + left + right).digest()

    def _build(self) -> bytes:
        if not self.leaves:
            return hashlib.sha256(b"").digest()
        
        current = self.leaves
        while len(current) > 1:
            next_level = []
            for i in range(0, len(current), 2):
                l = current[i]
                r = current[i + 1] if i + 1 < len(current) else l
                next_level.append(self._hash_nodes(l, r))
            self.levels.append(next_level)
            current = next_level
        return current[0]

    def get_root_hex(self) -> str:
        return self.root.hex()

    def get_proof(self, index: int):
        proof = []
        for level in self.levels[:-1]:
            is_right = (index % 2 == 1)
            sibling_idx = index - 1 if is_right else index + 1
            if sibling_idx < len(level):
                proof.append(('L' if is_right else 'R', level[sibling_idx].hex()))
            else:
                proof.append(('R', level[index].hex()))
            index //= 2
        return proof

    @staticmethod
    def verify_proof(leaf_data: str, proof: list, root_hex: str) -> bool:
        current = hashlib.sha256(b'\x00' + str(leaf_data).encode('utf-8')).digest()
        for direction, sibling_hex in proof:
            sibling = bytes.fromhex(sibling_hex)
            if direction == 'L':
                current = hashlib.sha256(b'\x01' + sibling + current).digest()
            else:
                current = hashlib.sha256(b'\x01' + current + sibling).digest()
        return current.hex() == root_hex

# -----------------------------------------------------------------------------
# 3. STATUTORY RULE 43 LOGIC & DATA INGESTION
# -----------------------------------------------------------------------------
def calculate_rule_43(amount: float, days: float):
    """Rajasthan Transparency in Public Procurement (RTPP) Act Rule 43 Mandates"""
    if amount > 20_000_000:
        mandated = 30
    elif amount > 5_000_000:
        mandated = 15
    elif amount > 1_000_000:
        mandated = 10
    else:
        mandated = 7
    
    violation = days < mandated
    deficit = max(0.0, mandated - days)
    
    # Procurement Risk Index (PRI) 0-100 score
    if violation:
        val_weight = min(30.0, (math.log10(max(1000, amount)) / 8.0) * 30.0)
        dur_weight = min(70.0, (deficit / mandated) * 70.0)
        pri = round(val_weight + dur_weight, 1)
    else:
        pri = 0.0
    return mandated, violation, deficit, pri

@st.cache_data
def load_all_district_data():
    """Loads all authentic CSV files from data/districts/ directory"""
    districts = ['jodhpur', 'jaipur', 'bikaner', 'udaipur', 'kota']
    combined_rows = []
    
    # Check both data/districts and local root paths
    for dist in districts:
        possible_paths = [
            f"data/districts/{dist}.csv",
            f"data/{dist}.csv",
            f"{dist}.csv"
        ]
        file_path = next((p for p in possible_paths if os.path.exists(p)), None)
        
        if file_path:
            try:
                df = pd.read_csv(file_path)
                # Normalize column naming
                col_map = {c.strip().lower(): c for c in df.columns}
                id_col = col_map.get('tender_id')
                amt_col = col_map.get('tender_amount') or col_map.get('estimated_cost_lac')
                dur_col = col_map.get('published_duration_days') or col_map.get('actual_window_days')
                
                if id_col and amt_col and dur_col:
                    for _, r in df.iterrows():
                        raw_amt = float(r[amt_col])
                        # If values were saved in lakhs, normalize to INR
                        amt = raw_amt * 100_000 if raw_amt < 10_000 else raw_amt
                        days = float(r[dur_col])
                        
                        mandated, is_viol, deficit, pri = calculate_rule_43(amt, days)
                        
                        combined_rows.append({
                            'tender_id': str(r[id_col]).strip(),
                            'nit_no': f"NIT {r[id_col][-8:]}",
                            'division': f"{dist.capitalize()} Division",
                            'district': dist.capitalize(),
                            'tender_amount': amt,
                            'estimated_cost_lac': round(amt / 100_000, 2),
                            'actual_window_days': int(round(days)),
                            'statutory_min_days': mandated,
                            'deficit_days': round(deficit, 1),
                            'is_violation': is_viol,
                            'risk_score': pri
                        })
            except Exception as e:
                st.error(f"Error loading {dist}: {e}")

    df_master = pd.DataFrame(combined_rows).drop_duplicates(subset=['tender_id'])
    return df_master

df_all = load_all_district_data()

# -----------------------------------------------------------------------------
# 4. SIDEBAR NAVIGATION & FILTERING
# -----------------------------------------------------------------------------
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/5/55/Emblem_of_India.svg", width=70)
    st.title("Forensic Controls")
    
    circle_options = ["All Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Bikaner", "Kota"]
    selected_circle = st.selectbox("Administrative Circle:", circle_options)
    
    st.markdown("---")
    st.markdown("#### **Statutory Framework**")
    st.caption("**Rajasthan Transparency in Public
