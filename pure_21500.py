import html, json, math, queue, random, re, threading, time, urllib3, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from requests.adapters import HTTPAdapter
from import_shpp_har import Store, compute_row, detect_district, parse_date

urllib3.disable_warnings()
TARGET, WORKERS = 21500, 5
PAGE_URL = "https://bidplus.gem.gov.in/all-bids"
API_URL = "https://bidplus.gem.gov.in/all-bids-data"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

TOP_HUBS = [
    "Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner",
    "Alwar", "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Nagaur",
    "Jhunjhunu", "Barmer", "Chittorgarh", "Hanumangarh", "Jaisalmer",
    "Local Self Government", "Medical And Health", "Public Works Department",
    "Panchayati Raj", "Energy Department", "Water Resources",
]

ALL_MARKERS = tuple(x.lower() for x in TOP_HUBS + [
    "tonk", "churu", "jalore", "jalor", "sirohi", "rajsamand", "banswara",
    "dungarpur", "pratapgarh", "jhalawar", "bundi", "baran", "sawai madhopur",
    "dausa", "karauli", "dholpur", "dhaulpur", "jhunjhunun", "chittaurgarh",
    "jvvnl", "jdvvnl", "avvnl", "rvunl", "rvpnl", "phed", "riico", "rsrdc",
    "ruidp", "rmscl", "rsmml", "rtdc", "rsrtc", "ruhs", "jda", "nagar nigam",
])

SLICES_2D = []
cur = date(2025, 1, 1)
while cur <= date(2026, 11, 12):
    nxt = min(cur + timedelta(days=1), date(2026, 11, 12))
    SLICES_2D.append((cur.isoformat(), nxt.isoformat()))
    cur = nxt + timedelta(days=1)

SLICES_5D = []
cur = date(2025, 1, 1)
while cur <= date(2026, 11, 12):
    nxt = min(cur + timedelta(days=4), date(2026, 11, 12))
    SLICES_5D.append((cur.isoformat(), nxt.isoformat()))
    cur = nxt + timedelta(days=1)

stats = {"pages": 0, "added": 0, "last_page_ts": time.time(), "t0": time.time()}

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
    for attempt in range(1, 6):
        if stop.is_set(): return None, ""
        try:
            s = requests.Session()
            s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4))
            s.headers.update({
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            r = s.get(PAGE_URL, timeout=18, verify=False)
            c = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), "")
            if not c:
                m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([a-f0-9]{16,64})['\"]", r.text, re.I)
                if m: c = m.group(1)
            if c: return s, c
        except Exception:
            pass
        time.sleep(1.5 * attempt + random.uniform(0.1, 0.5))
    return None, ""

def worker(wid, q, store, lock, stop):
    time.sleep(0.4 * wid)
    s, csrf = get_sess(stop)
    if not s: return
    while not stop.is_set():
        try:
            kw, df, dt, tries = q.get_nowait()
        except queue.Empty:
            break

        p, dup_p, errs, aborted = 1, 0, 0, False
        while not stop.is_set() and p <= 500:
            csrf = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), csrf)
            pay = {
                "page": p,
                "param": {"searchBid": kw, "searchType": "fullText"},
                "filter": {
                    "bidStatusType": "bidrastatus",
                    "byType": "all",
                    "highBidValue": "",
                    "byEndDate": {"from": df, "to": dt},
                    "sort": "Bid-End-Date-Latest",
                },
            }
            try:
                r = s.post(
                    API_URL,
                    data={"payload": json.dumps(pay, separators=(",", ":")), "csrf_bd_gem_nk": csrf},
                    headers={
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": "https://bidplus.gem.gov.in",
                        "Referer": PAGE_URL,
                    },
                    timeout=18,
                    verify=False,
                )
            except Exception:
                errs += 1
                time.sleep(1.2 * errs)
                s, csrf = get_sess(stop)
                if not s or errs >= 3:
                    aborted = True
                    break
                continue

            if r.status_code != 200 or not r.text.strip().startswith("{"):
                errs += 1
                time.sleep(1.2 * errs)
                s, csrf = get_sess(stop)
                if not s or errs >= 3:
                    aborted = True
                    break
                continue

            errs = 0
            with lock:
                stats["pages"] += 1
                stats["last_page_ts"] = time.time()

            docs, nf = walk_docs(r.json())
            if not docs:
                break

            added = 0
            for d in docs:
                d1, d2 = pdt(d.get("final_start_date_sort")), pdt(d.get("final_end_date_sort"))
                if not any(m in json.dumps(d).lower() for m in ALL_MARKERS):
                    continue
                bid = clean(d.get("b_bid_number"))
                if not bid or "/2024/" in bid or not d1 or not d2 or d1.year not in {2025, 2026}:
                    continue
                win = (d2.date() - d1.date()).days
                if win < 0 or win > 365:
                    continue
                dept = " - ".join(x for x in (
                    clean(d.get("ba_official_details_minName")),
                    clean(d.get("ba_official_details_deptName")),
                    clean(d.get("ba_official_details_orgName")),
                ) if x) or "Government of Rajasthan"
                dist = detect_district(dept, clean(d.get("b_category_name")), kw) or "Jaipur"

                with lock:
                    if store.real_count >= TARGET:
                        stop.set()
                        break
                    if not store.has(bid, bid):
                        store.add(compute_row(bid, bid, dept[:180], dist, 0.0, win))
                        added += 1
                        stats["added"] += 1
                        print(f"[+{stats['added']:,}] {store.real_count:,}/{TARGET:,} | W{wid} | {dist} | {bid}", flush=True)
                        if store.real_count >= TARGET:
                            stop.set()
                            break

            dup_p = 0 if added else dup_p + 1
            max_p = min(math.ceil(max(nf, 1) / 10), 500)
            if dup_p >= 25 or p >= max_p:
                break
            p += 1
            time.sleep(random.uniform(0.26, 0.42))

        if aborted and tries < 2 and not stop.is_set():
            q.put((kw, df, dt, tries + 1))
        q.task_done()

def watchdog(q, store, lock, stop):
    while not stop.is_set():
        time.sleep(10)
        if stop.is_set(): break
        with lock:
            cnt, added, pgs = store.real_count, stats["added"], stats["pages"]
            idle = time.time() - stats["last_page_ts"]
        mins = max((time.time() - stats["t0"]) / 60.0, 0.01)
        print(f"[SPEED {mins:.1f}m] Total: {cnt:,}/{TARGET:,} (+{added:,} new | {int(added/mins):,} rows/min) | Pages: {pgs:,} | Queue: {q.qsize():,}", flush=True)
        if idle >= 50:
            print("\n[WATCHDOG] GeM paused responses for 50s. Auto-stopping cleanly to save & push progress!", flush=True)
            stop.set()
            break

store = Store()
print(f"Pure 2025-2026 Sweep starting from {store.real_count:,} -> target {TARGET:,}...", flush=True)
q, seen = queue.Queue(), set()

# 1. 2-day windows for "Rajasthan" across all of 2025-2026 (randomized so all months hit immediately)
raj_slices = list(SLICES_2D)
random.shuffle(raj_slices)
for df, dt in raj_slices:
    key = ("Rajasthan", df, dt)
    if key not in seen: seen.add(key); q.put((*key, 0))

# 2. 5-day windows across major districts & official state department names
hub_tasks = [(kw, df, dt) for df, dt in SLICES_5D for kw in TOP_HUBS[1:]]
random.shuffle(hub_tasks)
for kw, df, dt in hub_tasks:
    if (kw, df, dt) not in seen: seen.add((kw, df, dt)); q.put((kw, df, dt, 0))

print(f"Queued {q.qsize():,} high-yield 2025-2026 date-slice queries across {WORKERS} workers...", flush=True)
lock, stop = threading.Lock(), threading.Event()
threading.Thread(target=watchdog, args=(q, store, lock, stop), daemon=True).start()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    for i in range(1, WORKERS + 1):
        ex.submit(worker, i, q, store, lock, stop)
stop.set()
print(f"\nFINISHED! Total pure 2025-2026 rows saved: {store.real_count:,}", flush=True)
