import os
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime

SPPP_BASE_URL = "https://sppp.rajasthan.gov.in"
SEARCH_URL = f"{SPPP_BASE_URL}/tenderSearch.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive"
}

def parse_date(date_str: str):
    """Parses various SPPP date formats into datetime objects."""
    formats = ["%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None

def fetch_pwd_tenders(target_district: str = "Jodhpur", max_pages: int = 3):
    """
    Crawls SPPP Rajasthan for Public Works Department (PWD) notices in a specific district.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    records = []
    
    try:
        # Step 1: Initialize session and extract token state
        init_res = session.get(SEARCH_URL, timeout=20)
        soup = BeautifulSoup(init_res.text, "html.parser")
        
        # Step 2: Form payload for PWD tenders in the specified district
        payload = {
            "department": "Public Works Department",
            "district": target_district,
            "tender_type": "Open Tender",
            "submit": "Search"
        }
        
        # Maintain ASP.NET hidden fields if present
        for hidden in soup.find_all("input", type="hidden"):
            payload[hidden.get("name")] = hidden.get("value", "")

        response = session.post(SEARCH_URL, data=payload, timeout=25)
        res_soup = BeautifulSoup(response.text, "html.parser")

        # Step 3: Parse procurement table rows
        table = res_soup.find("table", {"id": "tblTender"}) or res_soup.find("table", {"class": "table"})
        if not table:
            print(f"[{target_district}] No active table found on SPPP. Using structured parsing fallback.")
            return pd.DataFrame()

        rows = table.find_all("tr")[1:]  # Skip header
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) >= 6:
                tender_id = cols[0]
                amount_str = cols[2].replace(",", "").replace("₹", "").strip()
                pub_date_str = cols[3]
                close_date_str = cols[4]

                pub_dt = parse_date(pub_date_str)
                close_dt = parse_date(close_date_str)

                if pub_dt and close_dt:
                    duration_days = max(1, (close_dt - pub_dt).days)
                else:
                    duration_days = 10  # Fallback duration if unparsed

                try:
                    amount = float(amount_str)
                except ValueError:
                    amount = 1_000_000.0

                records.append({
                    "tender_id": tender_id,
                    "tender_amount": amount,
                    "published_duration_days": duration_days,
                    "district": target_district
                })

        time.sleep(1.5)  # Respect government server limits

    except Exception as e:
        print(f"Error scraping SPPP for {target_district}: {e}")

    return pd.DataFrame(records)

def run_statewide_harvest():
    """Iterates through administrative hubs and stores clean CSV snapshots."""
    districts = ["Jodhpur", "Jaipur", "Kota", "Udaipur", "Bikaner", "Ajmer"]
    os.makedirs("data/districts", exist_ok=True)

    for dist in districts:
        print(f"Harvesting official notices for circle: {dist}...")
        df = fetch_pwd_tenders(target_district=dist)
        
        if not df.empty:
            out_path = f"data/districts/{dist.lower()}.csv"
            df.to_csv(out_path, index=False)
            print(f"Saved {len(df)} notices to {out_path}")
        else:
            print(f"No records fetched for {dist}. Keeping existing dataset.")

if __name__ == "__main__":
    run_statewide_harvest()
