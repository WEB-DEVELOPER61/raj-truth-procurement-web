"""
merge_gem.py - write gem_enrichment.csv back into your district CSVs.

  python merge_gem.py --csv "data/districts/*.csv" --enrich gem_enrichment.csv

For every GeM row found in the enrichment file it fills:
  publication_date, bid_end_date           (from GeM start / end date)
  actual_window_days                       (end date - start date, only when both dates exist)
  tender_amount, estimated_cost_lac        (ONLY when the bid PDF stated an estimated cost)
and recomputes statutory_min_days, deficit_days, is_violation, risk_score with the same Rule 43
formulas as app.py. Rows without a stated cost keep their old amount (the app then still treats the
Rs 25 L default as 'unverified'). A .bak copy of every changed CSV is written first.
"""
import argparse
import glob
import math
import shutil

import pandas as pd


def rule43(amount, days):
    if amount > 20_000_000:
        m = 30
    elif amount > 5_000_000:
        m = 15
    elif amount > 1_000_000:
        m = 10
    else:
        m = 7
    viol = days < m
    deficit = max(0.0, float(m - days))
    if viol:
        val_w = min(30.0, (math.log10(max(1000.0, amount)) / 8.0) * 30.0)
        dur_w = min(70.0, (deficit / m) * 70.0)
        pri = round(min(100.0, val_w + dur_w), 1)
    else:
        pri = 0.0
    return m, deficit, viol, pri


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", nargs="+", default=["data/districts/*.csv"])
    ap.add_argument("--enrich", default="gem_enrichment.csv")
    a = ap.parse_args()

    en = pd.read_csv(a.enrich)
    en = en[en["status"].astype(str).str.startswith(("dates_ok", "dates_missing"))].set_index("tender_id")
    print(f"{len(en)} enriched bids available")

    tot_dates = tot_cost = 0
    for pattern in a.csv:
        for f in glob.glob(pattern):
            df = pd.read_csv(f)
            if "tender_id" not in df.columns:
                continue
            hit = df["tender_id"].isin(en.index)
            if not hit.any():
                continue
            shutil.copy(f, f + ".bak")
            for col in ("publication_date", "bid_end_date"):
                if col not in df.columns:
                    df[col] = ""
            for i in df.index[hit]:
                e = en.loc[df.at[i, "tender_id"]]
                if isinstance(e["start_date"], str) and e["start_date"]:
                    df.at[i, "publication_date"] = e["start_date"]
                if isinstance(e["end_date"], str) and e["end_date"]:
                    df.at[i, "bid_end_date"] = e["end_date"]
                if pd.notna(e["window_days"]) and str(e["window_days"]) != "":
                    df.at[i, "actual_window_days"] = int(float(e["window_days"]))
                    tot_dates += 1
                amt = pd.to_numeric(e.get("published_cost"), errors="coerce")
                if pd.notna(amt) and amt > 0:
                    df.at[i, "tender_amount"] = float(amt)
                    df.at[i, "estimated_cost_lac"] = round(float(amt) / 100000.0, 2)
                    tot_cost += 1
                amt_now = float(pd.to_numeric(df.at[i, "tender_amount"], errors="coerce") or 2_500_000)
                days_now = float(pd.to_numeric(df.at[i, "actual_window_days"], errors="coerce") or 10)
                m, d, v, p = rule43(amt_now, days_now)
                df.at[i, "statutory_min_days"] = m
                df.at[i, "deficit_days"] = d
                df.at[i, "is_violation"] = v
                df.at[i, "risk_score"] = p
            df.to_csv(f, index=False)
            print(f"updated {int(hit.sum())} rows in {f}")
    print(f"done: window/date set on {tot_dates} rows, real cost set on {tot_cost} rows")


if __name__ == "__main__":
    main()
