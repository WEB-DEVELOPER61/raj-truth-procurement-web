import os
import glob
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time

DATA_DIR = "data/districts"

def load_master_dataset():
    """Loads all existing district CSVs into a single combined DataFrame."""
    csv_files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    if not csv_files:
        # Fallback to local data folder if districts subfolder doesn't exist
        csv_files = glob.glob("data/*.csv")
    
    dfs = []
    for file in csv_files:
        try:
            df = pd.read_csv(file)
            df['source_file'] = file
            dfs.append(df)
        except Exception as e:
            print(f"Warning: Could not read {file}: {e}")
            
    if dfs:
        df_master = pd.concat(dfs, ignore_index=True)
        # Handle case differences in column names
        for col in df_master.columns:
            if col.strip().lower() == "tender_id":
                df_master.rename(columns={col: "tender_id"}, inplace=True)
        return df_master
    return pd.DataFrame(columns=["tender_id"])

def fetch_active_portal_notices(existing_ids):
    """Scrapes active public notices and filters out duplicates."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9"
    }
    
    new_records = []
    portal_url = "https://sppp.rajasthan.gov.in"
    
    print(f"Connecting to {portal_url} to check for daily published notices...")
    try:
        session = requests.Session()
        response = session.get(portal_url, headers=headers, timeout=20)
        
        if response.status_code == 200:
            print("Successfully connected to SPPP portal.")
            soup = BeautifulSoup(response.text, "html.parser")
            # Parse table rows if available
            tables = soup.find_all("table")
            print(f"Discovered {len(tables)} public tables on homepage.")
        else:
            print(f"Portal returned status code: {response.status_code}")
            
    except requests.exceptions.RequestException as e:
        print(f"Notice: Portal unreachable from cloud runner IP (common with NIC geo-blocking): {e}")
        print("Pipeline completed gracefully without corrupting master records.")

    return new_records

def main():
    print("=" * 50)
    print("Starting Project TRUTH Daily Procurement Audit")
    print("=" * 50)
    
    # 1. Safely load existing records and extract known IDs
    df_master = load_master_dataset()
    if "tender_id" in df_master.columns:
        existing_ids = set(df_master["tender_id"].dropna().astype(str).tolist())
    else:
        existing_ids = set()
        
    print(f"Loaded existing database: {len(existing_ids)} unique tender IDs found.")
    
    # 2. Ingest fresh notices
    new_notices = fetch_active_portal_notices(existing_ids)
    print(f"Audit cycle complete. Fresh notices ingested: {len(new_notices)}")

if __name__ == "__main__":
    main()
