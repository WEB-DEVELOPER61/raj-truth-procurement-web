#!/usr/bin/env python3
import html as html_lib
import json
import math
import queue
import re
import threading
import time
import urllib3
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from requests.adapters import HTTPAdapter

from import_shpp_har import Store, compute_row, detect_district, parse_date

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TARGET_ROWS = 20250
ALLOWED_YEARS = {2025, 2026}
WORKERS = 8
GEM_PAGE_URL = "https://bidplus.gem.gov.in/all-bids"
GEM_API_URL = "https://bidplus.gem.gov.in/all-bids-data"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

TOP_HUBS = [
    "Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner",
    "Alwar", "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Nagaur",
    "Jhunjhunu", "Barmer", "Chittorgarh", "Hanumangarh", "Jaisalmer",
]

DATE_SLICES = [
    ("2025-01-01", "2025-03-31"),
    ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"),
    ("2025-10-01", "2025-12-31"),
    ("2026-01-01", "2026-03-31"),
    ("2026-04-01", "2026-06-30"),
]

PROC_TERMS = [
    "hiring", "manpower", "vehicle", "security", "maintenance", "repair",
    "supply", "computer", "electrical", "civil", "medical", "furniture",
    "printing", "stationery", "cleaning", "catering", "solar", "CCTV",
    "UPS", "AC", "transformer", "cable", "pipe", "pump", "water",
    "equipment", "medicine", "uniform", "taxi", "bus", "guard", "housekeeping",
]

ALL_DISTRICTS = [
    "Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner",
    "Alwar", "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Nagaur",
    "Jhunjhunu", "Barmer", "Tonk", "Churu", "Chittorgarh", "Hanumangarh",
    "Jaisalmer", "Jalore", "Sirohi", "Rajsamand", "Banswara", "Dungarpur",
    "Pratapgarh", "Jhalawar", "Bundi", "Baran", "Sawai Madhopur", "Dausa",
    "Karauli", "Dholpur",
]

RAJ_MARKERS = tuple(
    x.lower()
    for x in (
        [
            "rajasthan", "jalor", "jhunjhunun", "chittaurgarh", "dhaulpur",
            "jvvnl", "jdvvnl", "avvnl", "rvunl", "rvpnl", "rsrdc", "riico",
            "ruidp", "rmscl", "rsmml", "rtdc", "rsrtc", "ruhs", "jnvu", "mlsu", "mdsu",
        ]
        + ALL_DISTRICTS
    )
)


def clean_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        val = val[0] if val else ""
    return re.sub(r"\s+", " ", html_lib.unescape(str(val))).strip()


def parse_dt(s: str):
    if not s:
        return None
    s = clean_str(s)
    iso_m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})", s)
    if iso_m:
        try:
            return datetime.strptime(f"{iso_m.group(1)} {iso_m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    for fmt in (
        "%d-%m-%Y %I:%M %p", "%d-%m-%Y %H:%M",
        "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M",
        "%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M",
        "%d-%m-%Y", "%d-%b-%Y",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return parse_date(s)


def extract_docs_and_total(payload) -> tuple[list[dict], int]:
    found_docs, num_found = [], 0

    def walk(obj):
        nonlocal num_found
        if isinstance(obj, str):
            s = obj.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    walk(json.loads(s))
                except Exception:
                    pass
            return
        if isinstance(obj, dict):
            if "numFound" in obj and obj["numFound"] is not None:
                try:
                    num_found = max(num_found, int(obj["numFound"]))
                except Exception:
                    pass
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and any(
                k in obj[0] for k in ("b_bid_number", "b_id", "bid_number", "final_start_date_sort")
            ):
                found_docs.extend(obj)
            else:
                for item in obj:
                    walk(item)

    walk(payload)
    return found_docs, num_found


def new_gem_session():
    for attempt in range(1, 6):
        try:
            sess = requests.Session()
            sess.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=1))
            sess.headers.update({
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            r = sess.get(GEM_PAGE_URL, timeout=20, verify=False)
            csrf = next((c.value for c in sess.cookies if "csrf" in c.name.lower() and c.value), "")
            if not csrf:
                m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([a-f0-9]{16,64})['\"]", r.text, re.I)
                if m:
                    csrf = m.group(1)
            if csrf:
                return sess, csrf
        except Exception:
            time.sleep(1.5 * attempt)
    return None, ""


def gem_worker(wid: int, task_q: queue.Queue, store: Store, lock: threading.Lock, stop_evt: threading.Event):
    sess, csrf = new_gem_session()
    if not sess:
        return

    while not stop_evt.is_set():
        try:
            mode, kw, d_from, d_to = task_q.get_nowait()
        except queue.Empty:
            break

        page_num, old_year_pages, dup_pages = 1, 0, 0
        while not stop_evt.is_set() and page_num <= 500:
            for c in sess.cookies:
                if "csrf" in c.name.lower() and c.value:
                    csrf = c.value
                    break

            payload_obj = {
                "page": page_num,
                "param": {"searchBid": kw, "searchType": "fullText"},
                "filter": {
                    "bidStatusType": mode,
                    "byType": "all",
                    "highBidValue": "",
                    "byEndDate": {"from": d_from, "to": d_to},
                    "sort": "Bid-End-Date-Latest",
                },
            }
            try:
                resp = sess.post(
                    GEM_API_URL,
                    data={
                        "payload": json.dumps(payload_obj, separators=(",", ":")),
                        "csrf_bd_gem_nk": csrf,
                    },
                    headers={
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": "https://bidplus.gem.gov.in",
                        "Referer": GEM_PAGE_URL,
                    },
                    timeout=20,
                    verify=False,
                )
            except Exception:
                sess, csrf = new_gem_session()
                if not sess:
                    return
                continue

            if resp.status_code == 404:
                break

            if resp.status_code != 200 or not resp.text.strip().startswith("{"):
                time.sleep(1.0)
                sess, csrf = new_gem_session()
                if not sess:
                    return
                continue

            docs, num_found = extract_docs_and_total(resp.json())
            if not docs:
                break

            max_pages = min(math.ceil(max(num_found, len(docs)) / max(len(docs), 1)), 500)
            added, has_recent_year = 0, False

            for doc in docs:
                pub_dt = parse_dt(clean_str(doc.get("final_start_date_sort")))
                close_dt = parse_dt(clean_str(doc.get("final_end_date_sort")))
                if pub_dt and pub_dt.year in ALLOWED_YEARS:
                    has_recent_year = True

                blob = json.dumps(doc).lower()
                if not any(m in blob for m in RAJ_MARKERS):
                    continue

                bid_no = clean_str(doc.get("b_bid_number"))
                if not bid_no or not pub_dt or not close_dt or pub_dt.year not in ALLOWED_YEARS:
                    continue

                win = (close_dt.date() - pub_dt.date()).days
                if win < 0 or win > 365:
                    continue

                ministry = clean_str(doc.get("ba_official_details_minName"))
                dept = clean_str(doc.get("ba_official_details_deptName"))
                org = clean_str(doc.get("ba_official_details_orgName"))
                cat = clean_str(doc.get("b_category_name"))
                full_dept = " - ".join(p for p in (ministry, dept, org) if p) or "Government of Rajasthan"
                district = detect_district(full_dept, cat, kw) or "Jaipur"

                with lock:
                    if store.real_count >= TARGET_ROWS:
                        stop_evt.set()
                        break
                    if not store.has(bid_no, bid_no):
                        store.add(compute_row(bid_no, bid_no, full_dept[:180], district, 0.0, win))
                        added += 1
                        if store.real_count % 50 == 0 or store.real_count >= TARGET_ROWS:
                            print(f"[Pass 3 | W{wid}] {kw} ({d_from or 'all'}) p.{page_num} -> Total verified 2025-2026: {store.real_count:,} / 20,000", flush=True)
                        if store.real_count >= TARGET_ROWS:
                            stop_evt.set()
                            break

            dup_pages = 0 if added else dup_pages + 1
            old_year_pages = 0 if has_recent_year else old_year_pages + 1
            if old_year_pages >= 4 or dup_pages >= 20 or page_num >= max_pages:
                break

            page_num += 1
            time.sleep(0.1)

        task_q.task_done()


def main():
    t0 = time.time()
    store = Store()
    print(f"Resuming Pass 3 from {store.real_count:,} verified 2025-2026 rows...", flush=True)

    task_q = queue.Queue()
    seen_q = set()

    for d_from, d_to in DATE_SLICES:
        for kw in TOP_HUBS:
            key = ("bidrastatus", kw, d_from, d_to)
            if key not in seen_q:
                seen_q.add(key)
                task_q.put(key)

    for dist in ALL_DISTRICTS:
        for term in PROC_TERMS:
            kw = f"{dist} {term}"
            key = ("bidrastatus", kw, "", "")
            if key not in seen_q:
                seen_q.add(key)
                task_q.put(key)

    print(f"Launching {WORKERS} parallel Keep-Alive workers across {task_q.qsize()} deep 2025-2026 queries...", flush=True)
    lock = threading.Lock()
    stop_evt = threading.Event()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for wid in range(1, WORKERS + 1):
            ex.submit(gem_worker, wid, task_q, store, lock, stop_evt)

    elapsed = time.time() - t0
    print(f"\nDONE in {elapsed/60:.1f} min! Total verified 2025-2026 Rajasthan tenders: {store.real_count:,}", flush=True)


if __name__ == "__main__":
    main()
