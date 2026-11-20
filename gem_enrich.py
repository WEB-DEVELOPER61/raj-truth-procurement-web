"""
gem_enrich.py - fetch start date, end date and (when stated) the estimated cost for GeM bids
listed in your CSVs.

Usage
  python gem_enrich.py --probe GEM/2026/B/7753447     # 1) see GeM's raw response for ONE bid
  python gem_enrich.py --csv "data/districts/*.csv" --limit 50 --pdf   # 2) small test run
  python gem_enrich.py --csv "data/districts/*.csv" --pdf              # 3) full run (resumable)

Output: gem_enrichment.csv   (cache: gem_cache.jsonl - safe to stop and restart any time)

Rules this script follows
  * A cost is saved ONLY if the bid PDF states it next to an explicit "estimated value/cost" label.
    EMD, bid security, performance security etc. are never used as a cost.
  * Nothing is guessed. If a field is not found it stays blank and status says why.
  * Polite: one request at a time, delay between requests, backs off and stops if blocked.

NOTE: written without access to the GeM site. The request format and field names below are from
memory of how bidplus.gem.gov.in works; run --probe first and adjust FIELD_* if the JSON differs.
"""
import argparse
import glob
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

BASE = "https://bidplus.gem.gov.in"
LIST_PAGE = f"{BASE}/all-bids"
SEARCH_URL = f"{BASE}/all-bids-data"
DOC_URL = f"{BASE}/showbidDocument/{{bid_id}}"

# Field names in GeM's search JSON (adjust after --probe if needed)
FIELD_BIDNO = ("b_bid_number", "bid_number")
FIELD_ID = ("b_id", "id")
FIELD_START = ("final_start_date_sort", "b_start_date", "start_date")
FIELD_END = ("final_end_date_sort", "b_end_date", "end_date")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}

# ---- cost extraction (labels must say "estimated"; EMD / security lines are ignored) ----------
_NUM = r"(?:₹|Rs\.?|INR)?\s*([\d][\d,]*(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?)?"
COST_PATTERNS = [
    re.compile(r"(?:total\s+)?estimated\s+(?:bid\s+)?(?:value|cost|amount|price)[^\d₹]{0,40}" + _NUM, re.I),
    re.compile(r"(?:bid|tender|contract)\s+estimated\s+(?:value|cost)[^\d₹]{0,40}" + _NUM, re.I),
]
BAD_CONTEXT = re.compile(r"\b(emd|earnest|security|epbg|performance|turnover|tender fee|document fee)\b", re.I)


def parse_cost(text: str):
    """Return (amount_in_rupees, matched_snippet) or (None, '')."""
    for pat in COST_PATTERNS:
        for m in pat.finditer(text or ""):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start: line_end if line_end != -1 else len(text)]
            if BAD_CONTEXT.search(line):
                continue
            try:
                val = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            unit = (m.group(2) or "").lower()
            if unit.startswith(("crore", "cr")):
                val *= 1e7
            elif unit.startswith(("lakh", "lac")):
                val *= 1e5
            if val > 0:
                return val, m.group(0).strip()[:120]
    return None, ""


def pdf_to_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            raise SystemExit("Install a PDF reader first:  pip install pypdf")
    reader = PdfReader(io.BytesIO(content))
    return "\n".join((p.extract_text() or "") for p in reader.pages[:6])


# ---- helpers ------------------------------------------------------------------------------------
def first(doc: dict, keys):
    for k in keys:
        if k in doc and doc[k] not in (None, "", []):
            v = doc[k]
            return v[0] if isinstance(v, list) else v
    return None


def to_dt(v):
    if v is None:
        return None
    s = str(v).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y %I:%M %p", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class Gem:
    def __init__(self, delay):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.token = None
        self.fail_streak = 0

    def init(self):
        r = self.s.get(LIST_PAGE, timeout=40)
        r.raise_for_status()
        self.token = next((v for k, v in self.s.cookies.items() if "csrf" in k.lower()), None)
        m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=,]\s*['\"]([0-9a-f]{16,})", r.text)
        if m:
            self.token = m.group(1)
        if not self.token:
            raise SystemExit("Could not obtain GeM CSRF token (site layout changed or blocked). Open the site in a browser and check.")

    def _wait(self):
        time.sleep(self.delay)

    def search(self, bid_no):
        payload = {
            "param": {"searchBid": bid_no, "searchType": "fullText"},
            "filter": {"bidStatusType": "all_bids", "byType": "all", "highBidValue": "",
                       "byEndDate": {"from": "", "to": ""}, "sort": "Bid-End-Date-Latest"},
        }
        data = {"payload": json.dumps(payload), "csrf_bd_gem_nk": self.token}
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": LIST_PAGE}
        self._wait()
        r = self.s.post(SEARCH_URL, data=data, headers=headers, timeout=40)
        if r.status_code in (403, 429):
            raise PermissionError(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    def pdf_text(self, bid_id):
        self._wait()
        r = self.s.get(DOC_URL.format(bid_id=bid_id), timeout=60)
        if r.status_code in (403, 429):
            raise PermissionError(f"HTTP {r.status_code}")
        r.raise_for_status()
        return pdf_to_text(r.content)


def find_doc(js, bid_no):
    docs = (((js or {}).get("response") or {}).get("response") or {}).get("docs") or []
    for d in docs:
        if str(first(d, FIELD_BIDNO)).strip().upper() == bid_no.strip().upper():
            return d
    return None


def process(gem, bid_no, want_pdf):
    rec = {"tender_id": bid_no, "status": "", "gem_internal_id": "", "start_date": "", "end_date": "",
           "window_days": "", "published_cost": "", "cost_source": "", "cost_evidence": "",
           "fetched_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")}
    js = gem.search(bid_no)
    doc = find_doc(js, bid_no)
    if not doc:
        rec["status"] = "not_found_in_search"
        return rec
    bid_id = first(doc, FIELD_ID)
    sd, ed = to_dt(first(doc, FIELD_START)), to_dt(first(doc, FIELD_END))
    rec["gem_internal_id"] = bid_id or ""
    rec["start_date"] = sd.date().isoformat() if sd else ""
    rec["end_date"] = ed.date().isoformat() if ed else ""
    if sd and ed:
        rec["window_days"] = max(0, (ed.date() - sd.date()).days)
    rec["status"] = "dates_ok" if sd and ed else "dates_missing"
    if want_pdf and bid_id:
        try:
            cost, ev = parse_cost(gem.pdf_text(bid_id))
        except PermissionError:
            raise
        except Exception as e:  # unreadable PDF etc.
            rec["status"] += f"|pdf_error:{type(e).__name__}"
            return rec
        if cost:
            rec["published_cost"], rec["cost_source"], rec["cost_evidence"] = cost, "gem_bid_pdf", ev
            rec["status"] += "|cost_ok"
        else:
            rec["status"] += "|no_cost_stated"
    return rec


def load_ids(patterns, only_violations=False, years=None):
    frames = []
    for p in patterns:
        for f in glob.glob(p):
            try:
                frames.append(pd.read_csv(f, dtype=str))
            except Exception as e:
                print(f"skip {f}: {e}")
    if not frames:
        raise SystemExit("No CSV files matched.")
    df = pd.concat(frames, ignore_index=True)
    df = df[df["tender_id"].astype(str).str.upper().str.startswith("GEM/")].drop_duplicates("tender_id")
    # most important first: flagged violations, then everything else
    if years:
        yr = df["tender_id"].astype(str).str.extract(r"GEM/(\d{4})/")[0]
        df = df[yr.isin([str(y) for y in years])]
    if only_violations and "is_violation" in df.columns:
        df = df[df["is_violation"].astype(str).str.lower() == "true"]
    if "is_violation" in df.columns:
        df["_p"] = (df["is_violation"].astype(str).str.lower() != "true").astype(int)
        df = df.sort_values("_p")
    return df["tender_id"].tolist()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", nargs="+", default=["data/districts/*.csv"])
    ap.add_argument("--out", default="gem_enrichment.csv")
    ap.add_argument("--cache", default="gem_cache.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--pdf", action="store_true", help="also open each bid PDF to look for a stated estimated cost")
    ap.add_argument("--workers", type=int, default=1, help="parallel requests (try 3-4; more risks a block)")
    ap.add_argument("--only-violations", action="store_true", help="only bids currently flagged as violations")
    ap.add_argument("--years", nargs="+", type=int, default=[2025, 2026],
                    help="only GeM bids numbered GEM/<year>/... (default: 2025 2026)")
    ap.add_argument("--probe", help="print GeM's raw search JSON for one bid number and exit")
    a = ap.parse_args()

    gem = Gem(a.delay)
    gem.init()

    if a.probe:
        js = gem.search(a.probe)
        print(json.dumps(js, indent=2)[:6000])
        doc = find_doc(js, a.probe)
        print("\nMATCHED DOC:", "yes" if doc else "NO - adjust FIELD_* names or search payload")
        return

    done = {}
    if os.path.exists(a.cache):
        for line in open(a.cache, encoding="utf-8"):
            try:
                r = json.loads(line)
                if not r["status"].startswith("error"):
                    done[r["tender_id"]] = r
            except Exception:
                pass

    todo = [i for i in load_ids(a.csv, a.only_violations, a.years) if i not in done]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(done)} already cached, {len(todo)} to fetch, {a.workers} worker(s)")

    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()
    local = threading.local()
    stop = threading.Event()
    counters = {"n": 0, "blocked": 0}

    def get_client():
        if not hasattr(local, "g"):
            local.g = Gem(a.delay)
            local.g.token = gem.token
            local.g.s.cookies.update(gem.s.cookies)
        return local.g

    def work(bid):
        if stop.is_set():
            return
        try:
            rec = process(get_client(), bid, a.pdf)
            counters["blocked"] = 0
        except PermissionError as e:
            with lock:
                counters["blocked"] += 1
                blocked = counters["blocked"]
            print(f"blocked ({e}); sleeping 60s (count {blocked})")
            time.sleep(60)
            if blocked >= 5:
                stop.set()
                print("Blocked repeatedly - stopping. Re-run later; progress is saved.")
            return
        except Exception as e:
            rec = {"tender_id": bid, "status": f"error:{type(e).__name__}"}
        with lock:
            cache.write(json.dumps(rec) + "\n")
            cache.flush()
            done[bid] = rec
            counters["n"] += 1
            if counters["n"] % 50 == 0:
                print(f"{counters['n']}/{len(todo)}  last status: {rec.get('status')}")
                pd.DataFrame(done.values()).to_csv(a.out, index=False)

    with open(a.cache, "a", encoding="utf-8") as cache:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            list(ex.map(work, todo))

    pd.DataFrame(done.values()).to_csv(a.out, index=False)
    df = pd.DataFrame(done.values())
    print(f"\nSaved {len(df)} rows -> {a.out}")
    print(df["status"].value_counts().to_string())


if __name__ == "__main__":
    main()
