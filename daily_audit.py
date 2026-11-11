- name: Purge Synthetic Fallback Rows
  run: |
    python -c '
    import glob, pandas as pd
    for f in glob.glob("data/districts/*.csv"):
        df = pd.read_csv(f)
        if "publication_date" in df.columns:
            clean = df[df["publication_date"] != "2026-11-11"]
            clean.to_csv(f, index=False)
    print("Purged all synthetic 2026-11-11 rows.")
    '
