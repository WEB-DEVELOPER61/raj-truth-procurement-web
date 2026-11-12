import os
import re
import ssl
import sys
import glob
import time
import urllib3
import argparse
import requests
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter

# Suppress insecure request warnings caused by legacy NIC/RISL SSL chains
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "districts")
os.makedirs(DATA_DIR, exist_ok=True)

BASE_URL = "https://sppp.rajasthan.gov.in"

SCHEMA_COLS = [
    'tender_id', 'nit_no', 'division', 'district', 'tender_amount',
    'estimated_cost_lac', 'actual_window_days', 'statutory_min_days',
    'deficit_days', 'is_violation', 'risk_score'
]

RAJASTHAN_ADMIN_HIERARCHY = {
    "Jaipur Division": ["Jaipur", "Alwar", "Dausa", "Jhunjhunu", "Sikar", "Khairthal-Tijara", "Kotputli-Behror"],
    "Jodhpur Division": ["Jodhpur", "Barmer", "Jaisalmer", "Jalore", "Pali", "Sirohi", "Balotra", "Phalodi"],
    "Ajmer Division": ["Ajmer", "Bhilwara", "Nagaur", "Tonk", "Beawar", "Didwana-Kuchaman"],
    "Udaipur Division": ["Udaipur", "Banswara", "Chittorgarh", "Dungarpur", "Pratapgarh", "Rajsamand", "Salumbar"],
    "Bikaner Division": ["Bikaner", "Churu", "Hanumangarh", "Sri Ganganagar"],
    "Kota Division": ["Kota", "Baran", "Bundi", "Jhalawar"],
    "Bharatpur Division": ["Bharatpur", "Dholpur", "Karauli", "Sawai Madhopur", "Deeg"]
}


class LegacyTLSAdapter(HTTPAdapter):
    """Allows connections to Indian government portals using older TLS/SSL configurations."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def create_session(cookie_str: str = ""):
    session = requests.Session()
    session.mount("https://", LegacyTLSAdapter())
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Referer": BASE_URL + "/"
    })
    if cookie_str:
        session.headers["Cookie"] = cookie_str.strip()
    return session


def load_existing_real_ids():
    """Loads all non-synthetic tender IDs currently stored on disk."""
    existing_ids = set()
    for path in glob.glob(os.path.join(DATA_DIR, "*.csv")):
        try:
            df = pd.read_csv(path)
            if "tender_id" in df.columns and "nit_no" in df.columns:
                # Exclude simulated corpus rows (NIT_XX/DIST/2025-26) from real count
                is_sim = df["nit_no"].astype(str).str.match(r"^NIT_\d{2,3}/[A-Z]{3}/202\d")
                real_ids = df.loc[~is_sim, "tender_id"].dropna().astype(str).tolist()
                existing_ids.update(real_ids)
        except Exception as err:
            print(f"[WARN] Could not read {path}: {err}")
    return existing_ids


def parse_indian_amount(raw_text: str):
    """Extracts strict numeric INR valuation. Returns None if no genuine amount is found."""
    if not raw_text:
        return None
    cleaned = raw_text.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    
    # Handle Crore / Lakh text if present
    crore_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:cr|crore)", cleaned, re.IGNORECASE)
    if crore_match:
        val = float(crore_match.group(1)) * 10000000.0
        return val if val > 0 else None

    lakh_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:lac|lakh)", cleaned, re.IGNORECASE)
    if lakh_match:
        val = float(lakh_match.group(1)) * 100000.0
        return val if val > 0 else None

    num_match = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if num_match:
        val = float(num_match.group(1))
        # Ignore tiny stray numbers like serial numbers (< Rs. 1,000)
        return val if val >= 1000.0 else None
    return None


def parse_dates_and_window(pub_str: str, close_str: str):
    """Parses real publication & closing timestamps. Returns None if dates cannot be verified."""
    formats = (
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y %I:%M %p",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p",
        "%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M",
        "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d"
    )
    d1, d2 = None, None
    for fmt in formats:
        if d1 is None:
            try:
                d1 = datetime.strptime(pub_str.strip(), fmt)
            except ValueError:
                pass
        if d2 is None:
            try:
                d2 = datetime.strptime(close_str.strip(), fmt)
            except ValueError:
                pass
    if d1 and d2 and d2 >= d1:
        return max(1, (d2 - d1).days)
    return None


def calculate_rule_43_compliance(tender_amount: float, actual_window_days: int):
    """Applies RTPP Rule 43(7) statutory minimums and computes Procurement Risk Index."""
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


def map_district_file(text_blob: str, fallback_district: str = "Jaipur"):
    """Maps department/location text to its exact Rajasthan district CSV."""
    low = text_blob.lower()
    for division, districts in RAJASTHAN_ADMIN_HIERARCHY.items():
        for dist in districts:
            if dist.lower() in low:
                fname = dist.lower().replace(" ", "_").replace("-", "_") + ".csv"
                return division, dist, fname
    fname = fallback_district.lower().replace(" ", "_").replace("-", "_") + ".csv"
    return "Jaipur Division", fallback_district, fname


def extract_rows_from_html(html_text: str, existing_ids: set, hint_district: str = "Jaipur"):
    """Parses SPPP HTML tables into verified records. Drops incomplete rows instead of faking values."""
    soup = BeautifulSoup(html_text, "html.parser")
    extracted = []
    next_page_urls = []

    # Capture pagination links on the page
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(strip=True).lower()
        if any(k in href.lower() for k in ["page=", "d-49653-p=", "offset=", "pageno="]) or label in ["next", "next >", "»"]:
            full_u = urljoin(BASE_URL, href)
            if full_u not in next_page_urls:
                next_page_urls.append(full_u)

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Identify column indexes dynamically from table header if present
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        
        for tr in rows[1:]:
            cols = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cols) < 4:
                continue

            row_joined = " | ".join(cols)

            # Match official SPPP UBN (Unique Bid Number) or NIC Tender ID
            ubn_match = re.search(r"\b([A-Z]{2,6}\d{6,14}|\d{4}_[A-Za-z0-9]+_\d+(?:_\d+)?)\b", row_joined)
            if not ubn_match:
                continue
            tender_id = ubn_match.group(1)
            if tender_id in existing_ids:
                continue

            # Extract all date strings in the row to identify publication vs closing date
            date_tokens = re.findall(
                r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APMapm]{2})?)?\b|\b\d{1,2}-[A-Za-z]{3}-\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*[APMapm]{2})?)?\b",
                row_joined
            )
            if len(date_tokens) < 2:
                continue

            actual_window = parse_dates_and_window(date_tokens[0], date_tokens[1])
            if actual_window is None:
                continue

            # Extract valuation from currency/numeric cells (skip serial numbers & dates)
            amount = None
            for idx, cell in enumerate(cols):
                if headers and idx < len(headers) and any(k in headers[idx] for k in ["cost", "amount", "value", "estimate", "inr", "rs"]):
                    amount = parse_indian_amount(cell)
                    if amount:
                        break
            if amount is None:
                for cell in cols:
                    if any(sym in cell for sym in ["₹", "Rs", "INR", "Lac", "Lakh", "Cr"]) or re.match(r"^\d{1,3}(?:,\d{2,3})+\.\d{2}$", cell.strip()):
                        amount = parse_indian_amount(cell)
                        if amount:
                            break

            # STRICT INTEGRITY GATE: Skip row if genuine valuation is missing
            if amount is None:
                continue

            division, district, target_file = map_district_file(row_joined, fallback_district=hint_district)
            stat_min, deficit, is_viol, risk_score = calculate_rule_43_compliance(amount, actual_window)

            nit_match = re.search(r"(NIT[^\s|]+|NIB[^\s|]+|\d+/\d{4}-\d{2})", row_joined, re.IGNORECASE)
            nit_no = nit_match.group(1) if nit_match else f"SPPP-{tender_id}"

            extracted.append({
                "tender_id": tender_id,
                "nit_no": nit_no,
                "division": division,
                "district": district,
                "tender_amount": round(amount, 2),
                "estimated_cost_lac": round(amount / 100000.0, 2),
                "actual_window_days": int(actual_window),
                "statutory_min_days": int(stat_min),
                "deficit_days": float(deficit),
                "is_violation": bool(is_viol),
                "risk_score": float(risk_score),
                "target_file": target_file
            })
            existing_ids.add(tender_id)

    return extracted, next_page_urls


def save_batch(records: list):
    """Appends verified real records to district CSVs immediately so progress is never lost."""
    if not records:
        return 0
    df_new = pd.DataFrame(records)
    saved = 0
    for filename, group in df_new.groupby("target_file"):
        path = os.path.join(DATA_DIR, filename)
        df_curr = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=SCHEMA_COLS)
        clean_group = group[SCHEMA_COLS]
        combined = pd.concat([df_curr, clean_group], ignore_index=True)
        combined.drop_duplicates(subset=["tender_id"], keep="first", inplace=True)
        combined.to_csv(path, index=False)
        saved += len(clean_group)
    return saved


def main():
    parser = argparse.ArgumentParser(description="Project TRUTH: Historical SPPP Rajasthan Scraper")
    parser.add_argument("--max-pages", type=int, default=150, help="Max pagination pages to crawl per endpoint")
    parser.add_argument("--cookie", type=str, default=os.environ.get("SPPP_COOKIE", ""), help="Optional browser session cookie")
    args = parser.parse_args()

    print("=" * 65)
    print("PROJECT TRUTH: SPPP.RAJASTHAN.GOV.IN HISTORICAL ARCHIVE SCRAPER")
    print("=" * 65)

    session = create_session(cookie_str=args.cookie)

    # Step 1: 5-second connectivity diagnostic
    print("[1/3] Testing connection to https://sppp.rajasthan.gov.in ...")
    try:
        home_resp = session.get(BASE_URL + "/", timeout=15, verify=False)
        print(f"      -> Connected! HTTP Status: {home_resp.status_code} | Payload: {len(home_resp.text):,} bytes")
        if home_resp.status_code in (403, 429, 503):
            print("[BLOCKED] SPPP portal rejected this cloud IP (HTTP 403/429/503).")
            print("          Switch to running this exact script on your local PC/laptop.")
            sys.exit(1)
    except Exception as e:
        print(f"[BLOCKED/TIMEOUT] Could not reach SPPP from this environment: {e}")
        print("                  Switch to running this exact script on your local PC/laptop.")
        sys.exit(1)

    existing_real_ids = load_existing_real_ids()
    print(f"[2/3] Verified authentic records already on disk: {len(existing_real_ids):,}")

    # Step 2: Discover all public tender listing links from SPPP homepage
    home_soup = BeautifulSoup(home_resp.text, "html.parser")
    seed_urls = []
    candidate_paths = [
        "/activeTenders.action",
        "/archiveTenders.action",
        "/closedTenders.action",
        "/tendersByDistrict.action",
        "/tenderSearch.action",
        "/home.php"
    ]
    for path in candidate_paths:
        seed_urls.append(urljoin(BASE_URL, path))

    for a in home_soup.find_all("a", href=True):
        href = a["href"]
        if any(w in href.lower() for w in ["tender", "archive", "district", "bid", "nit"]):
            full_u = urljoin(BASE_URL, href)
            if full_u.startswith(BASE_URL) and full_u not in seed_urls:
                seed_urls.append(full_u)

    print(f"[3/3] Discovered {len(seed_urls)} candidate SPPP endpoints. Starting harvest...")

    visited_urls = set()
    total_newly_scraped = 0

    for seed in seed_urls:
        queue = [seed]
        pages_crawled = 0

        while queue and pages_crawled < args.max_pages:
            current_url = queue.pop(0)
            if current_url in visited_urls:
                continue
            visited_urls.add(current_url)

            try:
                resp = session.get(current_url, timeout=20, verify=False)
                if resp.status_code != 200:
                    continue

                records, next_links = extract_rows_from_html(resp.text, existing_real_ids)
                if records:
                    saved = save_batch(records)
                    total_newly_scraped += saved
                    print(f"      [+] Page {pages_crawled + 1}: +{saved} real notices | Total Real Corpus: {len(existing_real_ids):,}")

                for nxt in next_links:
                    if nxt not in visited_urls and nxt not in queue:
                        queue.append(nxt)

                pages_crawled += 1
                time.sleep(0.6)
            except Exception:
                continue

    # Also query district-specific endpoints across all 41 districts
    for division, districts in RAJASTHAN_ADMIN_HIERARCHY.items():
        for dist in districts:
            dist_url = f"{BASE_URL}/tendersByDistrict.action?districtName={dist}"
            if dist_url in visited_urls:
                continue
            try:
                resp = session.get(dist_url, timeout=15, verify=False)
                if resp.status_code == 200:
                    records, _ = extract_rows_from_html(resp.text, existing_real_ids, hint_district=dist)
                    if records:
                        saved = save_batch(records)
                        total_newly_scraped += saved
                        print(f"      [+] {dist}: +{saved} real notices | Total Real Corpus: {len(existing_real_ids):,}")
                time.sleep(0.4)
            except Exception:
                continue

    print("-" * 65)
    print(f"Harvest Complete! Newly added authentic notices: {total_newly_scraped:,}")
    print(f"Total 100% Authentic Corpus Now on Disk: {len(existing_real_ids):,}")


if __name__ == "__main__":
    main()
