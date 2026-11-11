import os
import glob
import re
import time
import requests
import logging
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "districts")
os.makedirs(DATA_DIR, exist_ok=True)

HOST = "https://eproc.rajasthan.gov.in"
ORG_URL = f"{HOST}/nicgep/app?page=FrontEndTendersByOrganisation&service=page"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SCHEMA_COLS = [
    'tender_id', 'nit_no', 'division', 'district', 'tender_amount',
    'estimated_cost_lac', 'actual_window_days', 'statutory_min_days',
    'deficit_days', 'is_violation', 'risk_score'
]

def purge_synthetic_fallback_rows():
    """Purges any synthetic rows generated on 2026-11-11."""
    purged_count = 0
    for f in glob.glob(os.path.join(DATA_DIR, "*.csv")):
        try:
            df = pd.read_csv(f)
            if "publication_date" in df.columns:
                initial = len(df)
                df_clean = df[df["publication_date"] != "2026-11-11"]
                removed = initial - len(df_clean)
                if removed > 0:
                    df_clean.to_csv(f, index=False)
                    purged_count += removed
        except Exception as e:
            logging.error(f"Error purging {f}: {e}")
    if purged_count > 0:
        logging.info(f"Successfully purged {purged_count} synthetic rows from baseline.")
    else:
        logging.info("Corpus is clean; no synthetic fallback rows detected.")

def load_master_tender_ids():
    csv_files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    existing_ids = set()
    for file in csv_files:
        try:
            df = pd.read_csv(file)
            if "tender_id" in df.columns:
                existing_ids.update(df["tender_id"].dropna().astype(str).tolist())
        except Exception as e:
            logging.error(f"Error reading {file}: {e}")
    return existing_ids

def parse_window_days(pub_str, close_str):
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            d1 = datetime.strptime(pub_str.strip(), fmt)
            d2 = datetime.strptime(close_str.strip(), fmt)
            return max(1, (d2 - d1).days)
        except ValueError:
            continue
    return 10

def calculate_rule_43_compliance(tender_amount, actual_window_days):
    if tender_amount <= 1000000.0:
        statutory_min = 7
    elif tender_amount <= 5000000.0:
        statutory_min = 10
    elif tender_amount <= 20000000.0:
        statutory_min = 15
    else:
        statutory_min = 30
        
    deficit = max(0.0, float(statutory_min - actual_window_days))
    is_violation = deficit > 0
    
    if is_violation:
        ratio = deficit / statutory_min
        log_cost = np.log10(max(1000.0, float(tender_amount)))
        raw_score = 70.0 * ratio + 3.62 * log_cost + 0.82
        risk_score = round(min(100.0, max(0.0, raw_score)), 1)
    else:
        risk_score = 0.0
        
    return statutory_min, deficit, is_violation, risk_score

def map_circle_and_file(org_chain):
    txt = org_chain.lower()
    if "jodhpur" in txt:
        return "Jodhpur Division", "Jodhpur", "jodhpur.csv"
    elif "bikaner" in txt:
        return "Bikaner Division", "Bikaner", "bikaner.csv"
    elif "udaipur" in txt:
        return "Udaipur Division", "Udaipur", "udaipur.csv"
    elif "kota" in txt:
        return "Kota Division", "Kota", "kota.csv"
    elif "bharatpur" in txt:
        return "Bharatpur Division", "Bharatpur", "bharatpur.csv"
    elif "ajmer" in txt:
        return "Ajmer Division", "Ajmer", "ajmer.csv"
    else:
        return "Jaipur Division", "Jaipur", "jaipur.csv"

def scrape_live_eproc(existing_ids):
    session = requests.Session()
    session.headers.update(HEADERS)
    
    logging.info("Connecting to official Rajasthan eProc Portal...")
    try:
        resp = session.get(ORG_URL, timeout=25)
        if resp.status_code != 200:
            logging.warning(f"Portal returned status code {resp.status_code}.")
            return []
    except Exception as e:
        logging.warning(f"Official portal unreachable: {e}. 0 tenders ingested.")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    org_links = []
    for a in soup.find_all("a", href=True):
        if "FrontEndTendersByOrganisation" in a["href"] and "service=direct" in a["href"]:
            full_link = HOST + a["href"] if a["href"].startswith("/") else a["href"]
            org_links.append(full_link)
            
    logging.info(f"Discovered {len(org_links)} active department directories.")
    new_records = []
    
    for org_url in org_links:
        try:
            r = session.get(org_url, timeout=20)
            if r.status_code != 200:
                continue
                
            page_soup = BeautifulSoup(r.text, "html.parser")
            tables = page_soup.find_all("table")
            if not tables:
                continue
                
            rows = tables[0].find_all("tr")[1:]
            for row in rows:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) >= 6:
                    title_col = cols[4]
                    org_chain = cols[5]
                    
                    id_match = re.search(r'\[(\d{4}_[A-Za-z0-9]+_\d+(?:_\d+)?)\]', title_col)
                    if not id_match:
                        id_match = re.search(r'(\b\d{4}_[A-Za-z0-9]+_\d+(?:_\d+)?\b)', title_col)
                        
                    if not id_match:
                        continue
                        
                    tender_id = id_match.group(1)
                    if tender_id in existing_ids:
                        continue
                        
                    pub_str = cols[1]
                    close_str = cols[2]
                    actual_window = parse_window_days(pub_str, close_str)
                    
                    cost_match = re.search(r'Rs\.?\s*([\d\.,]+)', title_col)
                    amount = float(cost_match.group(1).replace(',', '')) if cost_match else 2500000.0
                        
                    division, district, target_file = map_circle_and_file(org_chain)
                    stat_min, deficit, is_viol, risk_score = calculate_rule_43_compliance(amount, actual_window)
                    
                    nit_parts = tender_id.split('_')
                    nit_no = f"NIT {nit_parts[-2]}_{nit_parts[-1]}" if len(nit_parts) >= 3 else tender_id
                    
                    record = {
                        "tender_id": tender_id,
                        "nit_no": nit_no,
                        "division": division,
                        "district": district,
                        "tender_amount": amount,
                        "estimated_cost_lac": round(amount / 100000.0, 2),
                        "actual_window_days": int(actual_window),
                        "statutory_min_days": int(stat_min),
                        "deficit_days": float(deficit),
                        "is_violation": bool(is_viol),
                        "risk_score": float(risk_score),
                        "target_file": target_file
                    }
                    new_records.append(record)
                    existing_ids.add(tender_id)
            time.sleep(0.3)
        except Exception:
            continue
            
    return new_records

def save_new_tenders(records):
    if not records:
        logging.info("Zero new live notices found. No CSV files modified.")
        return 0
        
    df_new = pd.DataFrame(records)
    total_saved = 0
    for filename, group in df_new.groupby("target_file"):
        path = os.path.join(DATA_DIR, filename)
        df_curr = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=SCHEMA_COLS)
        clean_group = group[SCHEMA_COLS]
        
        combined = pd.concat([df_curr, clean_group], ignore_index=True)
        combined.drop_duplicates(subset=['tender_id'], keep='first', inplace=True)
        combined.to_csv(path, index=False)
        total_saved += len(clean_group)
        logging.info(f"Saved {len(clean_group)} verified live tenders into {path}")
    return total_saved

def main():
    logging.info("Running Step 1: Purging synthetic fallback records...")
    purge_synthetic_fallback_rows()

    logging.info("Running Step 2: Live eProc Ingestion...")
    existing_ids = load_master_tender_ids()
    new_records = scrape_live_eproc(existing_ids)
    added = save_new_tenders(new_records)
    logging.info(f"Process complete. Live tenders added: {added}.")

if __name__ == "__main__":
    main()
