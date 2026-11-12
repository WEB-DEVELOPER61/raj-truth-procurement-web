import csv, glob, html, json, math, os, queue, random, re, threading, time, urllib3, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from requests.adapters import HTTPAdapter
from import_shpp_har import Store, compute_row, detect_district, parse_date

urllib3.disable_warnings()
TARGET, WORKERS = 21420, 8
PAGE_URL = "https://bidplus.gem.gov.in/all-bids"
API_URL = "https://bidplus.gem.gov.in/all-bids-data"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

HUBS = [
    "Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner",
    "Alwar", "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Nagaur",
    "Jhunjhunu", "Barmer", "Chittorgarh", "Hanumangarh", "Jaisalmer",
    "JVVNL", "JDVVNL", "AVVNL", "PHED", "PWD", "Nagar Nigam",
]

ALL_MARKERS = tuple(x.lower() for x in HUBS + [
    "tonk", "churu", "jalore", "jalor", "sirohi", "rajsamand", "banswara",
    "dungarpur", "pratapgarh", "jhalawar", "bundi", "baran", "sawai madhopur",
    "dausa", "karauli", "dholpur", "dhaulpur", "jhunjhunun", "chittaurgarh",
    "riico", "rsrdc", "ruidp", "rmscl", "rsmml", "rtdc", "rsrtc", "ruhs", "jda",
])

SLICES_2D, cur = [], date(2025, 1, 1)
while cur <= date(2026, 11, 12):
    nxt = min(cur + timedelta(days=1), date(2026, 11, 12))
    SLICES_2D.append((cur.isoformat(), nxt.isoformat()))
    cur = nxt + timedelta(days=1)

stats = {"pages": 0, "last_page_ts": time.time(), "t0": time.time()}

def clean(v):
    if isinstance(v, list): v = v[0] if v else ""
    return re.sub(r"\s+", " ", html.unescape(str(v or ""))).strip()

def pdt(s):
    s = clean(s)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})", s)
    if m:
        try: return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError: pass
    for f in ("%d-%m-%Y %I:%M %p", "%d-%m-%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M", "%d-%m-%Y"):
        try: return datetime.strptime(s, f)
        except ValueError: continue
    return parse_date(s)

def walk_docs(obj, docs=None, nf=0):
    if docs is None: docs = []
    if isinstance(obj, str) and obj.strip()[:1] in "{[":
        try: return walk_docs(json.loads(obj), docs, nf)
        except Exception: return docs, nf
    if isinstance(obj, dict):
        if obj.get("numFound") is not None:
            try: nf = max(nf, int(obj["numFound"]))
            except Exception: pass
        for v in obj.values(): docs, nf = walk_docs(v, docs, nf)
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in ("b_bid_number", "b_id", "final_start_date_sort")):
            docs.extend(obj)
        else:
            for i in obj: docs, nf = walk_docs(i, docs, nf)
    return docs, nf

def get_sess(stop):
    for attempt in range(1, 5):
        if stop.is_set(): return None, ""
        try:
            s = requests.Session()
            s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4))
            s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
            r = s.get(PAGE_URL, timeout=15, verify=False)
            c = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), "")
            if not c:
                m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([a-f0-9]{16,64})['\"]", r.text, re.I)
                if m: c = m.group(1)
            if c: return s, c
        except Exception:
            pass
        time.sleep(0.8 * attempt)
    return None, ""

def worker(wid, q, store, lock, stop, start_cnt, need):
    time.sleep(0.15 * wid)
    s, csrf = get_sess(stop)
    if not s: return
    while not stop.is_set():
        try: kw, df, dt, sort_by = q.get_nowait()
        except queue.Empty: break
        p, dup_p, errs = 1, 0, 0
        while not stop.is_set() and p <= 50:
            csrf = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), csrf)
            pay = {"page": p, "param": {"searchBid": kw, "searchType": "fullText"}, "filter": {"bidStatusType": "bidrastatus", "byType": "all", "highBidValue": "", "byEndDate": {"from": df, "to": dt}, "sort": sort_by}}
            try:
                r = s.post(API_URL, data={"payload": json.dumps(pay, separators=(",", ":")), "csrf_bd_gem_nk": csrf}, headers={"Accept": "application/json, text/javascript, */*; q=0.01", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Origin": "https://bidplus.gem.gov.in", "Referer": PAGE_URL}, timeout=15, verify=False)
            except Exception:
                errs += 1
                if errs >= 2: break
                s, csrf = get_sess(stop)
                continue
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                errs += 1
                if errs >= 2: break
                s, csrf = get_sess(stop)
                continue

            errs = 0
            with lock:
                stats["pages"] += 1
                stats["last_page_ts"] = time.time()

            docs, nf = walk_docs(r.json())
            if not docs: break
            added = 0
            for d in docs:
                d1, d2 = pdt(d.get("final_start_date_sort")), pdt(d.get("final_end_date_sort"))
                if not any(m in json.dumps(d).lower() for m in ALL_MARKERS): continue
                bid = clean(d.get("b_bid_number"))
                if not bid or "/2024/" in bid or "/2023/" in bid or not d1 or not d2 or d1.year not in {2025, 2026}: continue
                win = (d2.date() - d1.date()).days
                if win < 0 or win > 365: continue
                dept = " - ".join(x for x in (clean(d.get("ba_official_details_minName")), clean(d.get("ba_official_details_deptName")), clean(d.get("ba_official_details_orgName"))) if x) or "Government of Rajasthan"
                dist = detect_district(dept, clean(d.get("b_category_name")), kw) or "Jaipur"
                with lock:
                    if store.real_count >= TARGET:
                        stop.set()
                        break
                    if not store.has(bid, bid):
                        store.add(compute_row(bid, bid, dept[:180], dist, 0.0, win))
                        added += 1
                        got = store.real_count - start_cnt
                        print(f"[{got}/{need}] Total: {store.real_count:,}/{TARGET:,} | W{wid} | {dist} | {bid}", flush=True)
                        if store.real_count >= TARGET:
                            stop.set()
                            break
            dup_p = 0 if added else dup_p + 1
            if dup_p >= 2 or p >= min(math.ceil(max(nf, 1) / 10), 50): break
            p += 1
            time.sleep(random.uniform(0.12, 0.22))
        q.task_done()

def monitor(q, store, lock, stop, start_cnt, need):
    while not stop.is_set():
        time.sleep(3)
        if stop.is_set(): break
        with lock:
            cnt = store.real_count
            pgs = stats["pages"]
            idle = time.time() - stats["last_page_ts"]
        elapsed = max(time.time() - stats["t0"], 0.1)
        got = cnt - start_cnt
        pg_rate = int(pgs / (elapsed / 60.0))
        row_rate = int(got / (elapsed / 60.0))
        print(f"[LIVE {elapsed:.0f}s] Progress: {got}/{need} (Total: {cnt:,}/{TARGET:,} | {row_rate} rows/min) | Pages scanned: {pgs:,} ({pg_rate:,} pg/min) | Queue left: {q.qsize():,}", flush=True)
        if idle >= 25:
            print("\n[WATCHDOG] Pages stopped moving for 25s (IP rate-limited). Auto-stopping cleanly!", flush=True)
            stop.set()
            break

store = Store()
start_cnt = store.real_count

if start_cnt < TARGET:
    need = TARGET - start_cnt
    print(f"Sniping exact +{need} pure 2025-2026 tenders ({start_cnt:,} -> {TARGET:,}) with 3s live monitor...", flush=True)
    q = queue.Queue()
    tasks = [(kw, df, dt, sb) for df, dt in SLICES_2D for kw in HUBS for sb in ("Bid-Start-Date-Oldest", "Bid-End-Date-Latest")]
    random.shuffle(tasks)
    for t in tasks: q.put(t)
    lock, stop = threading.Lock(), threading.Event()
    threading.Thread(target=monitor, args=(q, store, lock, stop, start_cnt, need), daemon=True).start()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i in range(1, WORKERS + 1):
            ex.submit(worker, i, q, store, lock, stop, start_cnt, need)
    stop.set()

store = Store()
if store.real_count > TARGET:
    excess = store.real_count - TARGET
    for f in sorted(glob.glob("data/districts/*.csv"), key=lambda p: os.path.getsize(p), reverse=True):
        if excess <= 0: break
        rows = list(csv.reader(open(f, newline="", encoding="utf-8")))
        if len(rows) <= 1: continue
        header, body = rows[0], rows[1:]
        kept = []
        for r in reversed(body):
            if excess > 0 and r and r[0].startswith("GEM/"):
                excess -= 1
            else:
                kept.append(r)
        kept.reverse()
        with open(f, "w", newline="", encoding="utf-8") as out:
            csv.writer(out).writerows([header] + kept)

final_store = Store()
raw_rows = sum(max(0, len(list(csv.reader(open(f, encoding="utf-8")))) - 1) for f in glob.glob("data/districts/*.csv"))
assert final_store.real_count == TARGET and raw_rows == TARGET
print(f"\nDONE in {time.time()-stats['t0']:.1f}s! Exact verified count: {final_store.real_count:,}", flush=True)
