import html, json, math, queue, re, threading, time, urllib3, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from requests.adapters import HTTPAdapter
from import_shpp_har import Store, compute_row, detect_district, parse_date

urllib3.disable_warnings()
TARGET, WORKERS = 21420, 5
PAGE_URL = "https://bidplus.gem.gov.in/all-bids"
API_URL = "https://bidplus.gem.gov.in/all-bids-data"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

HUBS = ["Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner", "Alwar", "Bhilwara", "Sikar", "Pali", "Bharatpur", "Ganganagar", "Nagaur", "Jhunjhunu", "Barmer", "Chittorgarh", "Hanumangarh", "Jaisalmer"]
DISTS = HUBS + ["Tonk", "Churu", "Jalore", "Sirohi", "Rajsamand", "Banswara", "Dungarpur", "Pratapgarh", "Jhalawar", "Bundi", "Baran", "Sawai Madhopur", "Dausa", "Karauli", "Dholpur", "JVVNL", "JDVVNL", "AVVNL", "PHED", "PWD", "RIICO", "RSRDC", "JDA", "Nagar Nigam"]
SLICES = [("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"), ("2025-04-01", "2025-06-30"), ("2025-01-01", "2025-03-31"), ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
TERMS = ["hiring", "manpower", "vehicle", "security", "maintenance", "repair", "supply", "computer", "electrical", "civil", "medical", "furniture", "printing", "cleaning", "solar", "CCTV", "water", "equipment", "taxi", "guard"]
MARKERS = tuple(x.lower() for x in DISTS + ["jalor", "jhunjhunun", "chittaurgarh", "dhaulpur", "rvunl", "rvpnl", "ruidp", "rmscl"])

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
            s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4))
            s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
            r = s.get(PAGE_URL, timeout=25, verify=False)
            c = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), "")
            if not c:
                m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([a-f0-9]{16,64})['\"]", r.text, re.I)
                if m: c = m.group(1)
            if c: return s, c
            print(f"[Auth] HTTP {r.status_code} without CSRF (retry {a}/6)...", flush=True)
        except Exception as e:
            print(f"[Auth] Connection retry {a}/6: {e}", flush=True)
        time.sleep(2 * a)
    return None, ""

def worker(wid, q, store, lock, stop):
    time.sleep(0.4 * wid)
    s, csrf = get_sess()
    if not s:
        print(f"[W{wid}] Could not open GeM session.", flush=True)
        return
    print(f"[W{wid}] Connected to GeM.", flush=True)
    while not stop.is_set():
        try: mode, btype, kw, df, dt = q.get_nowait()
        except queue.Empty: break
        p, old_p, dup_p, errs = 1, 0, 0, 0
        while not stop.is_set() and p <= 500:
            csrf = next((x.value for x in s.cookies if "csrf" in x.name.lower() and x.value), csrf)
            pay = {"page": p, "param": {"searchBid": kw, "searchType": "fullText"}, "filter": {"bidStatusType": mode, "byType": btype, "highBidValue": "", "byEndDate": {"from": df, "to": dt}, "sort": "Bid-End-Date-Latest"}}
            try:
                r = s.post(API_URL, data={"payload": json.dumps(pay, separators=(",", ":")), "csrf_bd_gem_nk": csrf}, headers={"Accept": "application/json, text/javascript, */*; q=0.01", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Origin": "https://bidplus.gem.gov.in", "Referer": PAGE_URL}, timeout=25, verify=False)
            except Exception:
                errs += 1
                if errs >= 5: break
                time.sleep(2 * errs); s, csrf = get_sess()
                if not s: return
                continue
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                errs += 1
                if errs >= 5: break
                time.sleep(2 * errs); s, csrf = get_sess()
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
                        if store.real_count % 50 == 0 or store.real_count >= TARGET:
                            print(f"[W{wid}] {mode}:{kw} ({df or 'all'}) p.{p} -> {store.real_count:,} / {TARGET:,}", flush=True)
                        if store.real_count >= TARGET: stop.set(); break
            dup_p = 0 if added else dup_p + 1
            old_p = 0 if rec else old_p + 1
            if old_p >= 4 or dup_p >= 20 or p >= min(math.ceil(max(nf, 1) / 10), 500): break
            p += 1; time.sleep(0.15)
        q.task_done()

store = Store()
print(f"Resuming from {store.real_count:,} verified 2025-2026 rows...", flush=True)
q, seen = queue.Queue(), set()
for kw in DISTS:
    if ("ongoing_bids", "all", kw, "", "") not in seen: seen.add(("ongoing_bids", "all", kw, "", "")); q.put(("ongoing_bids", "all", kw, "", ""))
for df, dt in SLICES:
    for kw in HUBS:
        if ("bidrastatus", "all", kw, df, dt) not in seen: seen.add(("bidrastatus", "all", kw, df, dt)); q.put(("bidrastatus", "all", kw, df, dt))
for dist in DISTS:
    for t in TERMS:
        kw = f"{dist} {t}"
        if ("bidrastatus", "all", kw, "", "") not in seen: seen.add(("bidrastatus", "all", kw, "", "")); q.put(("bidrastatus", "all", kw, "", ""))
print(f"Queued {q.qsize()} queries across {WORKERS} workers...", flush=True)
lock, stop = threading.Lock(), threading.Event()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    for i in range(1, WORKERS + 1): ex.submit(worker, i, q, store, lock, stop)
print(f"DONE! Total verified 2025-2026 rows: {store.real_count:,}", flush=True)
