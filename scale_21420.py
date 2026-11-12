import html, json, math, queue, re, threading, time, urllib3, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from requests.adapters import HTTPAdapter
from import_shpp_har import Store, compute_row, detect_district, parse_date

urllib3.disable_warnings()
TARGET, WORKERS = 21500, 12
PAGE_URL = "https://bidplus.gem.gov.in/all-bids"
API_URL = "https://bidplus.gem.gov.in/all-bids-data"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

HUBS = ["Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner", "Alwar", "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Nagaur", "Jhunjhunu", "Barmer", "Chittorgarh", "Hanumangarh", "Jaisalmer"]
DISTS = HUBS + ["Tonk", "Churu", "Jalore", "Sirohi", "Rajsamand", "Banswara", "Dungarpur", "Pratapgarh", "Jhalawar", "Bundi", "Baran", "Sawai Madhopur", "Dausa", "Karauli", "Dholpur", "JVVNL", "JDVVNL", "AVVNL", "PHED", "PWD", "RIICO", "RSRDC", "JDA", "Nagar Nigam"]
MARKERS = tuple(x.lower() for x in DISTS + ["jalor", "jhunjhunun", "chittaurgarh", "dhaulpur", "rvunl", "rvpnl", "ruidp", "rmscl"])

SLICES_10D = []
cur = date(2025, 1, 1)
while cur <= date(2026, 12, 31):
    nxt = min(cur + timedelta(days=9), date(2026, 12, 31))
    SLICES_10D.append((cur.isoformat(), nxt.isoformat()))
    cur = nxt + timedelta(days=1)

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

def get_sess():
    for a in range(1, 7):
        try:
            s = requests.Session()
            s.mount("https://", HTTPAdapter(pool_connections=6, pool_maxsize=6))
            s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
            r = s.get(PAGE_URL, timeout=20, verify=False)
            c = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), "")
            if not c:
                m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([a-f0-9]{16,64})['\"]", r.text, re.I)
                if m: c = m.group(1)
            if c: return s, c
        except Exception: pass
        time.sleep(1.5 * a)
    return None, ""

def worker(wid, q, store, lock, stop):
    time.sleep(0.2 * wid)
    s, csrf = get_sess()
    if not s: return
    while not stop.is_set():
        try: mode, kw, df, dt = q.get_nowait()
        except queue.Empty: break
        p, old_p, dup_p, errs = 1, 0, 0, 0
        while not stop.is_set() and p <= 500:
            csrf = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), csrf)
            pay = {"page": p, "param": {"searchBid": kw, "searchType": "fullText"}, "filter": {"bidStatusType": mode, "byType": "all", "highBidValue": "", "byEndDate": {"from": df, "to": dt}, "sort": "Bid-End-Date-Latest"}}
            try:
                r = s.post(API_URL, data={"payload": json.dumps(pay, separators=(",", ":")), "csrf_bd_gem_nk": csrf}, headers={"Accept": "application/json, text/javascript, */*; q=0.01", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Origin": "https://bidplus.gem.gov.in", "Referer": PAGE_URL}, timeout=20, verify=False)
            except Exception:
                errs += 1
                if errs >= 4: break
                s, csrf = get_sess()
                if not s: return
                continue
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                errs += 1
                if errs >= 4: break
                s, csrf = get_sess()
                if not s: return
                continue
            errs = 0
            docs, nf = walk_docs(r.json())
            if not docs: break
            added, rec = 0, False
            for d in docs:
                d1, d2 = pdt(d.get("final_start_date_sort")), pdt(d.get("final_end_date_sort"))
                if d1 and d1.year in {2025, 2026}: rec = True
                if not any(m in json.dumps(d).lower() for m in MARKERS): continue
                bid = clean(d.get("b_bid_number"))
                if not bid or not d1 or not d2 or d1.year not in {2025, 2026}: continue
                win = (d2.date() - d1.date()).days
                if win < 0 or win > 365: continue
                dept = " - ".join(x for x in (clean(d.get("ba_official_details_minName")), clean(d.get("ba_official_details_deptName")), clean(d.get("ba_official_details_orgName"))) if x) or "Government of Rajasthan"
                dist = detect_district(dept, clean(d.get("b_category_name")), kw) or "Jaipur"
                with lock:
                    if store.real_count >= TARGET: stop.set(); break
                    if not store.has(bid, bid):
                        store.add(compute_row(bid, bid, dept[:180], dist, 0.0, win))
                        added += 1
                        if store.real_count % 100 == 0 or store.real_count >= TARGET:
                            print(f"[W{wid}] {mode}:{kw} ({df}) p.{p} -> {store.real_count:,} / {TARGET:,}", flush=True)
                        if store.real_count >= TARGET: stop.set(); break
            dup_p = 0 if added else dup_p + 1
            old_p = 0 if rec else old_p + 1
            if old_p >= 4 or dup_p >= 45 or p >= min(math.ceil(max(nf, 1) / 10), 500): break
            p += 1; time.sleep(0.03)
        q.task_done()

store = Store()
print(f"Resuming 10-day deep sweep from {store.real_count:,} -> {TARGET:,}...", flush=True)
q, seen = queue.Queue(), set()
for mode in ("ongoing_bids", "bidrastatus"):
    for df, dt in reversed(SLICES_10D):
        for kw in HUBS[:10]:
            key = (mode, kw, df, dt)
            if key not in seen: seen.add(key); q.put(key)
for df, dt in reversed(SLICES_10D):
    for kw in HUBS[10:]:
        key = ("bidrastatus", kw, df, dt)
        if key not in seen: seen.add(key); q.put(key)
print(f"Queued {q.qsize()} 10-day queries across {WORKERS} workers...", flush=True)
lock, stop = threading.Lock(), threading.Event()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    for i in range(1, WORKERS + 1): ex.submit(worker, i, q, store, lock, stop)
print(f"DONE! Total verified 2025-2026 rows: {store.real_count:,}", flush=True)
