#!/usr/bin/env python3
"""
portal_enrich.py  -  SPPP / eProc tender scraper (probe-first)

I have NOT seen a real SPPP or eProc page, so this script does not guess
at exact HTML. It finds "label : value" pairs (Tender Value, Published Date,
Bid Submission End ...) wherever they appear, and a PROBE mode shows you what
the real page looks like so labels can be tuned in LABELS below.

It stops (does not bypass) if a captcha or login page appears.

Install once:   pip install requests beautifulsoup4 lxml pandas

Commands
  probe   fetch one page, save it, print what the parser sees
  list    parse tender LIST pages (live URL or saved .html files) - dates, ids, cost if shown
  crawl   walk listing pages -> detail pages -> portal_cache.jsonl
  export  cache -> portal_enrichment.csv
  merge   write found costs/dates into data/districts/*.csv (backup first)
  selftest  offline test on fake pages (no internet)
"""
import argparse, glob, json, os, re, shutil, sys, time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

CACHE = "portal_cache.jsonl"
UA = "Mozilla/5.0 (research; Project TRUTH procurement audit)"

# label (lowercase, regex) -> field. Edit after running `probe`.
LABELS = {
    "published_cost": [r"tender value", r"estimated (cost|value|amount)", r"estimated tender value",
                       r"tender estimated", r"approx(imate)? (cost|value)", r"project cost", r"\bnit value\b"],
    "publication_date": [r"publish(ed)? date", r"date of publication", r"publication date",
                         r"bid (document )?(download|sale|start)", r"tender (issue|start) date", r"notice date"],
    "bid_end_date": [r"bid (submission )?(end|closing)", r"closing date", r"last date (of|for) (bid )?submission",
                     r"submission (end|closing)", r"bid due date", r"due date"],
    "nit_no": [r"\bnit (no|number)", r"tender (id|no|number|reference)", r"bid (no|number)", r"\bunique id\b"],
    "department": [r"department", r"organi[sz]ation", r"procuring entity", r"office name"],
    "title": [r"tender (title|name)", r"name of (work|tender)", r"work description", r"\btitle\b"],
}
CAPTCHA_HINTS = ["captcha", "recaptcha", "enter the characters", "security code", "verification code"]
LOGIN_HINTS = ["please login", "user id", "sign in to continue"]


# ---------------------------------------------------------------- parsing
def parse_cost(text):
    """'Rs. 12,34,567.00' / '12.5 Lakh' / '1.2 Cr' -> rupees (float) or None."""
    if not text:
        return None
    t = text.lower().replace(",", "").replace("₹", " ").replace("rs.", " ").replace("rs", " ").replace("inr", " ")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(crore|cr|lakhs?|lacs?|lakh|l\b)?", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "")
    if unit.startswith("cr"):
        val *= 1e7
    elif unit.startswith("l"):
        val *= 1e5
    return val if val > 0 else None


DATE_FORMATS = ["%d-%b-%Y %H:%M", "%d-%b-%Y %I:%M %p", "%d-%b-%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p",
                "%d/%m/%Y", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y %H:%M",
                "%d %b %Y", "%d %B %Y", "%d-%B-%Y"]


def parse_date(text):
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    m = re.search(r"\d{1,4}[-/ ][A-Za-z0-9]{1,9}[-/ ]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)?", t)
    if not m:
        return None
    s = m.group(0)
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def label_pairs(soup):
    """Yield (label, value) from th/td, td/td, dt/dd and 'Label : value' text."""
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        # pairs across the row: (0,1), (2,3) ...
        for i in range(0, len(cells) - 1, 2):
            yield cells[i], cells[i + 1]
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            yield dt.get_text(" ", strip=True), dd.get_text(" ", strip=True)
    for line in soup.get_text("\n").split("\n"):
        if ":" in line and len(line) < 250:
            a, _, b = line.partition(":")
            yield a.strip(), b.strip()


def parse_detail(html):
    soup = BeautifulSoup(html, "lxml")
    low = soup.get_text(" ").lower()
    out = {"published_cost": "", "publication_date": "", "bid_end_date": "", "nit_no": "",
           "department": "", "title": "", "status": ""}
    if any(h in low for h in CAPTCHA_HINTS):
        out["status"] = "captcha"
        return out
    for label, value in label_pairs(soup):
        lab = label.lower().strip(" :")
        if not lab or not value or len(lab) > 80:
            continue
        for field, pats in LABELS.items():
            if out[field]:
                continue
            if any(re.search(p, lab) for p in pats):
                if field == "published_cost":
                    c = parse_cost(value)
                    if c:
                        out[field] = c
                elif field in ("publication_date", "bid_end_date"):
                    d = parse_date(value)
                    if d:
                        out[field] = d
                else:
                    out[field] = value[:200]
    got_cost, got_date = bool(out["published_cost"]), bool(out["publication_date"] or out["bid_end_date"])
    out["status"] = ("dates_ok|cost_ok" if got_cost and got_date else "cost_ok" if got_cost
                     else "dates_ok|no_cost_stated" if got_date else "nothing_found")
    return out


# ---------------------------------------------------------------- network
def session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def get(s, url, delay, retries=3):
    for a in range(retries):
        try:
            time.sleep(delay)
            r = s.get(url, timeout=30)
            if r.status_code in (403, 429):
                print(f"  blocked ({r.status_code}) - waiting 60s")
                time.sleep(60)
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  retry {a+1}: {e}")
            time.sleep(5 * (a + 1))
    return None


def load_cache():
    d = {}
    if os.path.exists(CACHE):
        for l in open(CACHE, encoding="utf-8"):
            if l.strip():
                r = json.loads(l)
                d[r["url"]] = r
    return d


def save_cache(rec):
    with open(CACHE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")



# ---------------------------------------------------------------- list-page parsing
# Handles the two layouts seen in screenshots:
#  eProc (NIC GePNIC): S.No | e-Published Date | Bid Submission Closing Date | Tender Opening Date |
#                      Title and Ref.No./Tender ID | Organisation Chain | Tender Value in Rs
#  SPPP bidlist.php  : S NO | Publish Date | Bid Submission End Date | Bid Opening Date |
#                      Bid Title (UBN) NIB Code | Department Name | No of Corrigendum | Details   (no cost column)
EPROC_ID = re.compile(r"\[(\d{4}_[A-Z0-9]+_\d+_\d+)\]")
EPROC_NIT = re.compile(r"\[(NIT[^\]]*|[^\]]*/\d{4}-\d{2}[^\]]*)\]")
SPPP_UBN = re.compile(r"\b[A-Z]{2,8}\d{4}[A-Z]{2,8}\d{3,}\b")
SPPP_NIB = re.compile(r"NIB Code:\s*([A-Z0-9]+)")


def _col(headers, *words):
    for i, h in enumerate(headers):
        if any(w in h for w in words):
            return i
    return None


def parse_list(html, source=""):
    soup = BeautifulSoup(html, "lxml")
    recs = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        hdr = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        ip = _col(hdr, "publish")
        ie = _col(hdr, "closing", "end date")
        it = _col(hdr, "title")
        if ip is None or ie is None or it is None:
            continue
        iv = _col(hdr, "tender value", "value in", "estimated")
        idp = _col(hdr, "organisation", "department")
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) <= max(ip, ie, it):
                continue
            txt = [t.get_text(" ", strip=True) for t in tds]
            cell = txt[it]
            tid = (EPROC_ID.search(cell) or [None, None])[1] if EPROC_ID.search(cell) else None
            nit = EPROC_NIT.search(cell).group(1) if EPROC_NIT.search(cell) else None
            ubn = SPPP_UBN.search(cell).group(0) if SPPP_UBN.search(cell) else None
            nib = SPPP_NIB.search(cell).group(1) if SPPP_NIB.search(cell) else None
            primary = tid or ubn or nit
            if not primary:
                continue
            cost = parse_cost(txt[iv]) if iv is not None and iv < len(txt) else None
            pub, end = parse_date(txt[ip]), parse_date(txt[ie])
            recs.append({
                "url": f"list:{primary}", "nit_no": primary, "alt_id": nit or nib or "",
                "title": cell[:200], "department": txt[idp][:120] if idp is not None and idp < len(txt) else "",
                "published_cost": cost if cost else "", "publication_date": pub or "", "bid_end_date": end or "",
                "status": ("dates_ok|cost_ok" if cost and pub else "dates_ok|no_cost_stated" if pub
                           else "cost_ok" if cost else "nothing_found"),
                "portal": source, "fetched_at": datetime.now().isoformat(timespec="seconds")})
    return recs


def cmd_list(a):
    cache = load_cache()
    htmls = []
    if a.html_dir:
        for f in sorted(glob.glob(os.path.join(a.html_dir, "*.htm*"))):
            htmls.append((open(f, encoding="utf-8", errors="ignore").read(), f))
    if a.url:
        h = get(session(), a.url, 0)
        if h:
            htmls.append((h, a.url))
    new = 0
    for h, src in htmls:
        if any(w in h.lower() for w in CAPTCHA_HINTS) and not parse_list(h, src):
            print(f"{src}: captcha page / no table. Solve the captcha in your own browser, then save the "
                  "results page (Ctrl+S) into a folder and use --html-dir.")
            continue
        recs = parse_list(h, src)
        print(f"{src[:80]}: {len(recs)} tender rows found")
        for r in recs:
            if r["url"] not in cache:
                save_cache(r); cache[r["url"]] = r; new += 1
    print("new records:", new, "| total in cache:", len(cache))


# ---------------------------------------------------------------- SPPP: paged list (DataTables JSON) + detail pages
def ajax_rows_to_records(rows, base, cols, source="sppp"):
    """rows: list of lists/dicts from a DataTables JSON reply. cols: dict of 0-based column indexes."""
    recs = []
    for row in rows:
        cells = list(row.values()) if isinstance(row, dict) else list(row)
        txt = [BeautifulSoup(str(c), "lxml").get_text(" ", strip=True) for c in cells]
        need = max(cols.values())
        if len(cells) <= need:
            continue
        title = txt[cols["title"]]
        ubn = SPPP_UBN.search(title)
        nib = SPPP_NIB.search(title)
        ident = ubn.group(0) if ubn else None
        if not ident:
            continue
        link = BeautifulSoup(str(cells[cols["details"]]), "lxml").find("a", href=True)
        durl = urljoin(base, link["href"]) if link else ""
        pub, end = parse_date(txt[cols["pub"]]), parse_date(txt[cols["end"]])
        recs.append({"url": f"list:{ident}", "nit_no": ident, "alt_id": nib.group(1) if nib else "",
                     "title": title[:200], "department": txt[cols["dept"]][:120], "detail_url": durl,
                     "published_cost": "", "publication_date": pub or "", "bid_end_date": end or "",
                     "status": "dates_ok|no_cost_yet" if pub else "nothing_found", "portal": source,
                     "fetched_at": datetime.now().isoformat(timespec="seconds")})
    return recs


def cmd_ajax(a):
    """Page through a DataTables JSON endpoint (the URL you copy from DevTools > Network)."""
    s = session()
    cols = dict(pub=a.col_pub, end=a.col_end, title=a.col_title, dept=a.col_dept, details=a.col_details)
    extra = dict(kv.split("=", 1) for kv in (a.param or []))
    cache = load_cache()
    s.headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": a.url.rsplit("/", 1)[0] + "/bidlist.php",
                      "Accept": "application/json, text/javascript, */*; q=0.01"})
    # Like a browser: open the list page first so the server issues a session (and any hidden form tokens).
    page_url = a.page_url or a.url.rsplit("/", 1)[0] + "/bidlist.php"
    hidden = {}
    try:
        pr = s.get(page_url, timeout=30)
        for inp in BeautifulSoup(pr.text, "lxml").find_all("input", attrs={"name": True}):
            if (inp.get("type") or "").lower() == "hidden":
                hidden[inp["name"]] = inp.get("value", "")
        print("opened", page_url, "| session cookies:", list(s.cookies.keys()), "| hidden fields:", list(hidden))
    except Exception as e:
        print("could not open list page first:", e)
    start, new, draw = 0, 0, 1
    while True:
        legacy = {"iColumns": a.columns, "sColumns": ",".join([""] * a.columns), "sSearch": "", "bRegex": "false",
                  "iSortCol_0": 0, "sSortDir_0": "asc", "iSortingCols": 1}
        for i in range(a.columns):
            legacy.update({f"mDataProp_{i}": i, f"sSearch_{i}": "", f"bRegex_{i}": "false",
                           f"bSearchable_{i}": "true", f"bSortable_{i}": "true"})
        params = {**hidden, **legacy, "draw": draw, "sEcho": draw, "start": start, "iDisplayStart": start,
                  "length": a.length, "iDisplayLength": a.length, **extra}
        time.sleep(a.delay)
        r = None
        try:
            r = s.post(a.url, data=params, timeout=30) if a.post else s.get(a.url, params=params, timeout=30)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            print("request/JSON failed:", e)
            if r is not None:
                print("HTTP", r.status_code, "| content-type:", r.headers.get("content-type"))
                print("raw reply (first 400 chars):", repr(r.text[:400]))
            print("Send me this plus the Payload tab of the request in DevTools (no cookies).")
            return
        if str(j.get("status")) == "500":
            print("server says:", j.get("message"))
            print("It needs something the browser sends. Paste me the Payload tab of this request (no cookies).")
            return
        rows = j.get("data") or j.get("aaData") or []
        total = j.get("recordsTotal") or j.get("iTotalRecords")
        recs = ajax_rows_to_records(rows, a.url, cols)
        for rec in recs:
            if rec["url"] not in cache:
                save_cache(rec); cache[rec["url"]] = rec; new += 1
        for k in list(hidden):                      # some servers rotate the CSRF token on every reply
            if isinstance(j.get(k), str):
                hidden[k] = j[k]
        print(f"start={start}: {len(rows)} rows ({len(recs)} usable) of total {total}")
        if not rows and start == 0:
            print("Empty first page. Raw reply:", repr(r.text[:400]))
            print("Paste me this and the Payload tab of the browser's request (blank out long token values).")
        start += a.length; draw += 1
        if not rows or (total and start >= int(total)) or start >= a.max_rows:
            break
    print("new list records:", new, "| cache:", len(cache))


def cmd_details(a):
    """Open each tender's detail page (from the list) and read cost + dates."""
    s = session()
    cache = load_cache()
    todo = [r for r in cache.values() if r.get("detail_url") and not r.get("detail_done")]
    print(len(todo), "detail pages to fetch")
    n = 0
    for rec in todo[: a.limit]:
        html = get(s, rec["detail_url"], a.delay)
        if html is None:
            continue
        d = parse_detail(html)
        if d["status"] == "captcha":
            print("Captcha on detail page - stopping."); return
        for k in ("published_cost", "publication_date", "bid_end_date"):
            if d[k]:
                rec[k] = d[k]
        rec["status"] = d["status"]; rec["detail_done"] = True
        save_cache(rec); n += 1
        if n % 50 == 0:
            print(f"  {n} done")
    print("detail pages done this run:", n)

# ---------------------------------------------------------------- commands
def cmd_probe(a):
    s = session()
    html = get(s, a.url, 0)
    if html is None:
        print("could not fetch")
        return
    os.makedirs("probe", exist_ok=True)
    path = os.path.join("probe", re.sub(r"\W+", "_", urlparse(a.url).netloc + urlparse(a.url).path)[:60] + ".html")
    open(path, "w", encoding="utf-8").write(html)
    soup = BeautifulSoup(html, "lxml")
    print("saved:", path, "| size:", len(html))
    print("title:", soup.title.get_text(strip=True) if soup.title else None)
    low = soup.get_text(" ").lower()
    print("captcha words:", [h for h in CAPTCHA_HINTS if h in low])
    print("login words:", [h for h in LOGIN_HINTS if h in low])
    print("tables:", len(soup.find_all("table")), "| links:", len(soup.find_all("a", href=True)))
    print("parsed as a DETAIL page ->", parse_detail(html))
    print("first 25 label/value pairs seen:")
    for i, (l, v) in enumerate(label_pairs(soup)):
        if i >= 25:
            break
        print("  ", l[:50], "=>", v[:60])
    print("first 15 links:")
    for x in soup.find_all("a", href=True)[:15]:
        print("  ", x.get_text(" ", strip=True)[:50], "->", urljoin(a.url, x["href"])[:110])
    print("\nIf the pairs above do not include Tender Value / dates, open the saved file and send me "
          "the lines around them (no cookies).")


def find_detail_links(soup, base, pattern):
    rx = re.compile(pattern, re.I) if pattern else None
    links = []
    for x in soup.find_all("a", href=True):
        u = urljoin(base, x["href"])
        if u.startswith("javascript"):
            continue
        if rx is None or rx.search(u) or rx.search(x.get_text(" ")):
            links.append(u)
    return list(dict.fromkeys(links))


def find_next(soup, base):
    for x in soup.find_all("a", href=True):
        t = x.get_text(" ", strip=True).lower()
        if t in ("next", "next >", ">", ">>", "next page", "»"):
            return urljoin(base, x["href"])
    return None


def cmd_crawl(a):
    s = session()
    cache = load_cache()
    urls = [l.strip() for l in open(a.list_urls) if l.strip() and not l.startswith("#")] if a.list_urls else [a.list_url]
    new = 0
    for start in urls:
        page, pages = start, 0
        while page and pages < a.max_pages:
            html = get(s, page, a.delay)
            if html is None:
                break
            low = html.lower()
            if any(h in low for h in CAPTCHA_HINTS):
                print("Captcha on listing page. Stopping; I do not bypass captchas. Use the portal's own "
                      "download/export or an official data request instead.")
                return
            soup = BeautifulSoup(html, "lxml")
            links = find_detail_links(soup, page, a.detail_pattern)
            print(f"{page[:90]}  page {pages+1}: {len(links)} candidate links")
            for u in links:
                if u in cache or new >= a.limit:
                    continue
                d = get(s, u, a.delay)
                if d is None:
                    continue
                rec = parse_detail(d)
                rec.update(url=u, fetched_at=datetime.now().isoformat(timespec="seconds"),
                           portal=urlparse(u).netloc)
                if rec["status"] == "captcha":
                    print("Captcha on detail page. Stopping.")
                    return
                if rec["status"] != "nothing_found":
                    save_cache(rec)
                cache[u] = rec
                new += 1
                if new % 25 == 0:
                    print(f"  {new} fetched this run")
            if new >= a.limit:
                print("limit reached")
                return
            page, pages = find_next(soup, page), pages + 1
    print("done, new records:", new)


def _portal_of(r):
    p = (str(r.get("portal", "")) + " " + str(r.get("url", ""))).lower()
    if "sppp" in p or SPPP_UBN.fullmatch(str(r.get("nit_no", ""))):
        return "sppp"
    if "eproc" in p or EPROC_ID.search("[" + str(r.get("nit_no", "")) + "]"):
        return "eproc"
    return "other"


EXPORT_COLS = ["nit_no", "alt_id", "title", "department", "published_cost", "publication_date",
               "bid_end_date", "status", "detail_url", "url", "fetched_at"]


def cmd_export(a):
    """Write separate files into data/portal/ - the district CSVs are NOT touched."""
    rows = list(load_cache().values())
    if not rows:
        print("cache empty")
        return
    os.makedirs("data/portal", exist_ok=True)
    df = pd.DataFrame(rows)
    for c in EXPORT_COLS:
        if c not in df.columns:
            df[c] = ""
    df["portal"] = [_portal_of(r) for r in rows]
    for name, g in df.groupby("portal"):
        path = f"data/portal/{name}_tenders.csv"
        g[["portal"] + EXPORT_COLS].to_csv(path, index=False)
        print(f"{path}: {len(g)} rows | with cost: {(g.published_cost.astype(str).str.len() > 0).sum()}")
    df[["portal"] + EXPORT_COLS].to_csv("data/portal/all_portal_tenders.csv", index=False)
    print("also saved data/portal/all_portal_tenders.csv:", len(df), "rows")
    print(df.status.value_counts())


def cmd_merge(a):
    cache = [r for r in load_cache().values() if r.get("nit_no")]
    by_id = {}
    for r in cache:
        by_id[str(r["nit_no"]).strip()] = r
        if r.get("alt_id"):
            by_id[str(r["alt_id"]).strip()] = r
    print(len(by_id), "cached records with an ID")
    os.makedirs("data/backups", exist_ok=True)
    total = 0
    for f in glob.glob(a.csv):
        try:
            df = pd.read_csv(f)
        except Exception:
            continue                       # empty / unreadable file: skip, do not crash
        if df.empty:
            continue
        keys = [c for c in ("tender_id", "nit_no") if c in df.columns]
        if not keys:
            continue
        for c in ("publication_date", "bid_end_date"):
            if c not in df.columns:
                df[c] = ""
        if "tender_amount" not in df.columns:
            df["tender_amount"] = 0.0
        n = 0
        for i, row in df.iterrows():
            rec = next((by_id[str(row[k]).strip()] for k in keys if str(row[k]).strip() in by_id), None)
            if not rec:
                continue
            ch = False
            amt = pd.to_numeric(row["tender_amount"], errors="coerce")
            if rec["published_cost"] and (pd.isna(amt) or amt == 0):
                df.at[i, "tender_amount"] = float(rec["published_cost"]); ch = True
            for c in ("publication_date", "bid_end_date"):
                if rec[c] and not str(row[c]).strip().replace("nan", ""):
                    df.at[i, c] = rec[c]; ch = True
            n += ch
        if n:
            shutil.copy(f, os.path.join("data/backups", os.path.basename(f) + ".bak"))
            df.to_csv(f, index=False)
            print(f"updated {n} rows in {f}")
            total += n
    print("total updated:", total)


FAKE = """<html><body><h2>Tender Details</h2><table>
<tr><td>NIT No</td><td>RSRTC/2026/ENG/118</td><td>Department</td><td>Public Works Dept</td></tr>
<tr><td>Tender Value</td><td>Rs. 12,34,567.00</td><td>Published Date</td><td>05-Mar-2026 11:00</td></tr>
<tr><td>Bid Submission End Date</td><td>20-Mar-2026 15:00</td><td>Tender Title</td><td>Road repair</td></tr>
</table></body></html>"""
FAKE_EPROC_LIST = """<table><tr><th>S.No</th><th>e-Published Date</th><th>Bid Submission Closing Date</th>
<th>Tender Opening Date</th><th>Title and Ref.No./Tender ID</th><th>Organisation Chain</th><th>Tender Value in Rs</th></tr>
<tr><td>1.</td><td>20-Nov-2026 01:40 PM</td><td>02-Dec-2026 06:00 PM</td><td>03-Dec-2026 01:00 PM</td>
<td>[ renovation of room ] [NIT-26/2026-27 UIT BHILWARA] [2026_UITBH_606235_1]</td><td>UIT-BHILWARA</td><td>11,75,000</td></tr></table>"""
FAKE_SPPP_LIST = """<table><tr><th>S NO</th><th>Publish Date</th><th>Bid Submission End Date</th><th>Bid Opening Date</th>
<th>Bid Title (UBN) NIB Code</th><th>Department Name</th><th>No of Corrigendum /Appeal</th><th>Details</th></tr>
<tr><td>1</td><td>20/11/2026</td><td>07/12/2026</td><td>08/12/2026</td><td>2.Construction work PWD2627WSOB15877 NIB Code: PWD2627A3502</td>
<td>CE Public Works Department</td><td>0/0</td><td></td></tr></table>"""
FAKE_CAPTCHA = "<html><body>Please enter the characters shown in the captcha</body></html>"


def cmd_selftest(a):
    r = parse_detail(FAKE)
    print(r)
    assert r["published_cost"] == 1234567.0 and r["publication_date"] == "2026-03-05" and r["bid_end_date"] == "2026-03-20"
    assert r["nit_no"] == "RSRTC/2026/ENG/118" and r["status"] == "dates_ok|cost_ok"
    assert parse_cost("12.5 Lakh") == 1250000.0 and parse_cost("1.2 Cr") == 12000000.0
    assert parse_date("20/03/2026 15:00") == "2026-03-20"
    assert parse_detail(FAKE_CAPTCHA)["status"] == "captcha"
    e = parse_list(FAKE_EPROC_LIST, "eproc")
    assert len(e) == 1 and e[0]["nit_no"] == "2026_UITBH_606235_1" and e[0]["published_cost"] == 1175000.0
    assert e[0]["publication_date"] == "2026-11-20" and e[0]["bid_end_date"] == "2026-12-02", e
    sp = parse_list(FAKE_SPPP_LIST, "sppp")
    assert len(sp) == 1 and sp[0]["nit_no"] == "PWD2627WSOB15877" and sp[0]["alt_id"] == "PWD2627A3502", sp
    assert sp[0]["publication_date"] == "2026-11-20" and sp[0]["bid_end_date"] == "2026-12-07" and sp[0]["published_cost"] == ""
    fake_rows = [["1", "20/11/2026", "07/12/2026", "08/12/2026",
                  "Road work PWD2627WSOB15877 <br>NIB Code: PWD2627A3502", "CE PWD", "0/0",
                  "<a href='bidview.php?id=15877'>x</a>"]]
    cols = dict(pub=1, end=2, title=4, dept=5, details=7)
    ar = ajax_rows_to_records(fake_rows, "https://sppp.rajasthan.gov.in/bidlist.php", cols)
    assert ar[0]["nit_no"] == "PWD2627WSOB15877" and ar[0]["detail_url"].endswith("/bidview.php?id=15877"), ar
    assert ar[0]["bid_end_date"] == "2026-12-07"
    print("selftest passed (offline parsing only; real pages still need `probe`)")


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("probe"); x.add_argument("url"); x.set_defaults(f=cmd_probe)
    x = sp.add_parser("crawl")
    x.add_argument("--list-url"); x.add_argument("--list-urls", help="text file, one listing URL per line")
    x.add_argument("--detail-pattern", default=None, help="regex a detail link must match (from probe)")
    x.add_argument("--max-pages", type=int, default=50); x.add_argument("--limit", type=int, default=500)
    x.add_argument("--delay", type=float, default=1.5); x.set_defaults(f=cmd_crawl)
    x = sp.add_parser("list", help="parse list pages (live URL or saved .html files)")
    x.add_argument("--url"); x.add_argument("--html-dir"); x.set_defaults(f=cmd_list)
    x = sp.add_parser("ajax", help="page through a DataTables JSON URL (SPPP list)")
    x.add_argument("--url", required=True); x.add_argument("--post", action="store_true")
    x.add_argument("--length", type=int, default=100); x.add_argument("--max-rows", type=int, default=20000)
    x.add_argument("--param", action="append", help="extra key=value sent with every request")
    x.add_argument("--delay", type=float, default=1.0)
    x.add_argument("--columns", type=int, default=8, help="number of table columns (default 8)")
    x.add_argument("--page-url", help="page to open first for the session (default: bidlist.php next to --url)")
    for nm, dv in (("pub", 1), ("end", 2), ("title", 4), ("dept", 5), ("details", 7)):
        x.add_argument(f"--col-{nm}", type=int, default=dv)
    x.set_defaults(f=cmd_ajax)
    x = sp.add_parser("details", help="fetch each tender's detail page for cost")
    x.add_argument("--limit", type=int, default=100000); x.add_argument("--delay", type=float, default=1.5)
    x.set_defaults(f=cmd_details)
    x = sp.add_parser("export"); x.set_defaults(f=cmd_export)
    x = sp.add_parser("merge"); x.add_argument("--csv", default="data/districts/*.csv"); x.set_defaults(f=cmd_merge)
    x = sp.add_parser("selftest"); x.set_defaults(f=cmd_selftest)
    a = p.parse_args()
    if a.cmd == "crawl" and not (a.list_url or a.list_urls):
        sys.exit("give --list-url or --list-urls")
    a.f(a)


if __name__ == "__main__":
    main()
