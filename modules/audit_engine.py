import os
import glob
import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from modules.merkle_tree import hash_leaf, compute_merkle_root, build_hierarchical_state_root

RTPP_THRESHOLDS = [
    (0, 1_000_000, 10),
    (1_000_000, 5_000_000, 15),
    (5_000_000, 20_000_000, 20),
    (20_000_000, float("inf"), 30)
]

def get_required_timeline(amount: float) -> int:
    for mn, mx, days in RTPP_THRESHOLDS:
        if mn <= amount < mx:
            return days
    return 10

def process_district_file(filepath: str, chunksize: int = 1000):
    chunks, leaves = [], []
    for chunk in pd.read_csv(filepath, chunksize=chunksize):
        chunk["tender_amount"] = pd.to_numeric(chunk["tender_amount"], errors="coerce").fillna(0)
        chunk["published_duration_days"] = pd.to_numeric(chunk["published_duration_days"], errors="coerce").fillna(0)
        chunk["required_days"] = chunk["tender_amount"].apply(get_required_timeline)
        chunk["time_deficit_days"] = np.maximum(0.0, chunk["required_days"] - chunk["published_duration_days"])
        chunk["is_violation"] = chunk["published_duration_days"] < chunk["required_days"]
        for _, r in chunk.iterrows():
            leaves.append(hash_leaf(f"{r['tender_id']}|{r['tender_amount']}|{r['published_duration_days']}|{r['is_violation']}"))
        chunks.append(chunk)
    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    return df, compute_merkle_root(leaves)

def run_statewide_pipeline(dist_dir: str):
    csvs = glob.glob(os.path.join(dist_dir, "*.csv"))
    dfs, roots = [], {}
    for f in csvs:
        name = os.path.splitext(os.path.basename(f))[0].title()
        d_df, d_root = process_district_file(f)
        if not d_df.empty:
            d_df["district"] = name
            dfs.append(d_df)
            roots[name] = d_root
    if not dfs:
        return pd.DataFrame(), "", {}
    m_df = pd.concat(dfs, ignore_index=True)
    feats = m_df[["tender_amount", "published_duration_days", "time_deficit_days"]].values
    iso = IsolationForest(n_estimators=100, contamination=0.1, random_state=42)
    m_df["is_anomaly"] = iso.fit_predict(feats) == -1
    state_root, _ = build_hierarchical_state_root(roots)
    return m_df, state_root, roots
