#!/usr/bin/env python3
import hashlib
import html as html_lib
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import urllib3
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from requests.adapters import HTTPAdapter

from import_shpp_har import (
    Store,
    compute_row,
    detect_district,
    parse_amount,
    parse_date,
    SYNTHETIC_RE,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TARGET_ROWS = 20000
ALLOWED_YEARS = {2025, 2026}
WORKERS = 8
GEM_PAGE_URL = "https://bidplus.gem.gov.in/all-bids"
GEM_API_URL = "https://bidplus.gem.gov.in/all-bids-data"
HF_REPO = "fireboy21/tenders_aoc"
TMP_DIR = "/tmp/hf_2025_2026"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

RAJ_DISTRICTS = [
    "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner", "Alwar",
    "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Sri Ganganagar",
    "Nagaur", "Jhunjhunu", "Barmer", "Tonk", "Churu", "Chittorgarh",
    "Hanumangarh", "Jaisalmer", "Jalore", "Sirohi", "Rajsamand", "Banswara",
    "Dungarpur", "Pratapgarh", "Jhalawar", "Bundi", "Baran", "Sawai Madhopur",
    "Dausa", "Karauli", "Dholpur",
]

RAJ_AGENCIES = [
    "Rajasthan", "Government of Rajasthan", "JVVNL", "JDVVNL", "AVVNL",
    "RVUNL", "RVPNL", "RSRDC", "RIICO", "RUIDP", "RMSCL", "JDA", "RHB",
    "UIT", "Nagar Nigam", "Nagar Parishad", "Nagar Palika", "Gram Panchayat",
    "Panchayat Samiti", "Zila Parishad", "PHED", "PWD", "SMS Hospital",
    "CMHO", "Collectorate", "Panchayati Raj", "Medical Health",
]

RAJ_MARKERS = tuple(
    x.lower()
    for x in (
        ["rajasthan", "jalor", "jhunjhunun", "chittaurgarh", "dhaulpur"]
        + RAJ_DISTRICTS
        + ["jvvnl", "jdvvnl", "avvnl", "rvunl", "rvpnl", "rsrdc", "riico", "ruidp", "rmscl"]
    )
)

TEST_NOISE_RE = re.compile(r"\b(test\s*\d*|dummy|sample\s*tender)\b", re.I)


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


# ---------------- STAGE 1: STRICTLY 2025-2026 HF ePROC RECORDS ----------------
def flatten_dict(obj, out=None) -> dict[str, str]:
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                flatten_dict(v, out)
            elif v not in (None, "", "NA", "N/A", "null", "None"):
                lk = str(k).lower().strip()
                if lk not in out:
                    out[lk] = clean_str(v)
    elif isinstance(obj, list):
        for item in obj:
            flatten_dict(item, out)
    return out


def import_hf_2025_2026(store: Store):
    from huggingface_hub import hf_hub_download

    os.makedirs(TMP_DIR, exist_ok=True)
    added = 0
    try:
        for db_file, main_tbl, det_tbl, prefix, allow_zero in (
            ("aoc_tenders.db", "aoc_tenders", "aoc_details", "HF-AOC", False),
            ("tenders_vps.db", "tenders", "tender_details", "HF-VPS", True),
        ):
            if store.real_count >= TARGET_ROWS:
                break
            print(f"[Stage 1] Downloading {db_file} to filter strictly 2025-2026 Rajasthan records...", flush=True)
            db_path = hf_hub_download(repo_id=HF_REPO, filename=db_file, repo_type="dataset", local_dir=TMP_DIR)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({main_tbl})").fetchall()}
            order_sql = "ORDER BY m.year DESC" if "year" in cols else ""
            sql = f"""
                SELECT m.*, d.details_json
                FROM {main_tbl} m
                JOIN {det_tbl} d ON m.internal_id = d.internal_id
                WHERE m.detail_url LIKE '%eproc.rajasthan%'
                {order_sql}
            """
            for row in con.execute(sql):
                if store.real_count >= TARGET_ROWS:
                    break
                if "year" in cols and row["year"] not in (None, "", 2025, 2026, "2025", "2026"):
                    continue
                try:
                    det = json.loads(row["details_json"])
                    if isinstance(det, str):
                        det = json.loads(det)
                except Exception:
                    continue
                if not isinstance(det, dict):
                    continue

                flat = flatten_dict(det)
                rd = dict(row)

                pub = None
                close = parse_dt(rd.get("closing_date"))
                for k, v in flat.items():
                    if pub is None and ("published date" in k or "epublished" in k):
                        pub = parse_dt(v)
                    if close is None and ("submission end date" in k or "closing date" in k or "bid opening date" in k):
                        close = parse_dt(v)
                if pub is None:
                    pub = parse_dt(rd.get("e_published_date") or rd.get("published_date"))
                if not pub or not close or pub.year not in ALLOWED_YEARS:
                    continue

                win = (close.date() - pub.date()).days
                if win < 0 or win > 365:
                    continue

                org = next((flat[k] for k in ("organisation name", "organisation chain", "department") if k in flat), "") or clean_str(rd.get("org_name")) or "Government of Rajasthan"
                title = next((flat[k] for k in ("tender title", "work description", "tender description", "title") if k in flat), "") or clean_str(rd.get("title"))
                loc = next((flat[k] for k in ("location", "address") if k in flat), "")
                nit = next((flat[k] for k in ("tender ref. no.", "tender reference number", "tender ref.", "nit no") if k in flat), "") or clean_str(rd.get("ref_no"))
                ext_id = clean_str(rd.get("tender_id")) or flat.get("tender id", "")
                nit_no = nit or ext_id or title[:80]
                if not nit_no or SYNTHETIC_RE.match(nit_no) or TEST_NOISE_RE.search(f"{nit_no} {title}"):
                    continue

                tid = f"{prefix}-{ext_id}" if ext_id else f"{prefix}-" + hashlib.md5(f"{nit_no}|{org}".encode()).hexdigest()[:12]
                if store.has(tid, nit_no):
                    continue

                amt = None
                for k, v in flat.items():
                    if any(t in k for t in ("contract value", "awarded value", "tender value", "estimated cost", "cost of work")) and "fee" not in k and "emd" not in k:
                        amt = parse_amount(v)
                        if amt is not None:
                            break
                if amt is None:
                    if not allow_zero:
                        continue
                    amt = 0.0

                district = detect_district(org, loc, title, " ".join(flat.values())[:2000]) or "Jaipur"
                store.add(compute_row(tid, nit_no[:120], org, district, amt, win))
                added += 1

            con.close()
            os.remove(db_path)
            print(f"[Stage 1] After {db_file}: +{added} new 2025-2026 rows | Total verified: {store.real_count:,}", flush=True)
    except Exception as e:
        print(f"[Stage 1] Notice: {e}", flush=True)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)


# ---------------- STAGE 2: 8-THREAD HTTP KEEP-ALIVE GEM SWEEP ----------------
def extract_docs_and_total(payload) -> tuple[list[dict], int]:
    found_docs = []
    num_found = 0

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


def new_gem_session() -> tuple[requests.Session | None, str]:
    for attempt in range(1, 6):
        try:
            sess = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=1)
            sess.mount("https://", adapter)
            sess.headers.update({
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            r = sess.get(GEM_PAGE_URL, timeout=20, verify=False)
            csrf = ""
            for c in sess.cookies:
                if "csrf" in c.name.lower() and c.value:
                    csrf = c.value
                    break
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
            mode, kw = task_q.get_nowait()
        except queue.Empty:
            break

        page_num = 1
        old_year_pages = 0
        dup_pages = 0

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
                    "byEndDate": {"from": "", "to": ""},
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
            added = 0
            has_recent_year = False

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
                        if store.real_count % 500 == 0 or store.real_count >= TARGET_ROWS:
                            print(f"[Stage 2 | W{wid}] {mode}:{kw} p.{page_num} -> Total verified 2025-2026: {store.real_count:,} / {TARGET_ROWS:,}", flush=True)
                        if store.real_count >= TARGET_ROWS:
                            stop_evt.set()
                            break

            if added:
                dup_pages = 0
            else:
                dup_pages += 1

            if not has_recent_year:
                old_year_pages += 1
            else:
                old_year_pages = 0

            if old_year_pages >= 4 or dup_pages >= 20 or page_num >= max_pages:
                break

            page_num += 1
            time.sleep(0.1)

        task_q.task_done()


def main():
    t0 = time.time()
    store = Store()
    print(f"Starting 15-Minute 2025-2026 Pipeline | Existing verified rows: {store.real_count:,}", flush=True)

    # Stage 1: Pull strictly 2025-2026 eProc rows from HF
    if store.real_count < TARGET_ROWS:
        import_hf_2025_2026(store)

    # Stage 2: 8-thread Keep-Alive GeM sweep across all Rajasthan districts & utilities
    if store.real_count < TARGET_ROWS:
        task_q = queue.Queue()
        seen_q = set()
        for mode in ("bidrastatus", "ongoing_bids"):
            for kw in RAJ_AGENCIES + RAJ_DISTRICTS:
                if (mode, kw) not in seen_q:
                    seen_q.add((mode, kw))
                    task_q.put((mode, kw))

        print(f"\n[Stage 2] Launching {WORKERS} parallel Keep-Alive workers across {task_q.qsize()} Rajasthan queries...", flush=True)
        lock = threading.Lock()
        stop_evt = threading.Event()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for wid in range(1, WORKERS + 1):
                ex.submit(gem_worker, wid, task_q, store, lock, stop_evt)

    elapsed = time.time() - t0
    print(f"\nDONE in {elapsed/60:.1f} min! Total strictly 2025-2026 Rajasthan tenders in data/districts/*.csv: {store.real_count:,}", flush=True)


if __name__ == "__main__":
    main()
