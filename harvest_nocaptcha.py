#!/usr/bin/env python3
import hashlib
import html as html_lib
import re
import ssl
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright
from import_shpp_har import (
    Store,
    compute_row,
    detect_district,
    parse_amount,
    parse_date,
    SYNTHETIC_RE,
)

TARGET_ROWS = 20000
BASE_URL = "https://eproc.rajasthan.gov.in/nicgep/app"
ZERO_CAPTCHA_PAGES = [
    ("AwardedTenders", f"{BASE_URL}?page=FrontEndAwardedTenders&service=page"),
    ("CancelledTenders", f"{BASE_URL}?page=FrontEndCancelledTenders&service=page"),
    ("ClosingDateTenders", f"{BASE_URL}?page=FrontEndTendersByClosingDate&service=page"),
]

SSL_CTX = ssl._create_unverified_context()


def parse_detail_html(raw_html: str) -> tuple[float | None, str]:
    clean = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw_html))
    clean = re.sub(r"\s+", " ", clean)
    m = re.search(
        r"(?:Tender Value in\s*₹?|Estimated Cost|Cost of Work|Tender Value|Contract Value)\s*[:\-]?\s*(NA|[0-9,]+(?:\.[0-9]+)?(?:\s*(?:Cr|Crore|Lac|Lakh))?)",
        clean,
        re.I,
    )
    amt = parse_amount(m.group(1)) if (m and m.group(1).upper() != "NA") else None
    return amt, clean[:3500]


def fetch_detail(url: str, cookie_header: str):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_header},
        )
        raw = urllib.request.urlopen(req, context=SSL_CTX, timeout=12).read().decode("utf-8", "ignore")
        return parse_detail_html(raw)
    except Exception:
        return None, ""


def find_data_rows(page):
    out = []
    for r in page.locator("tr:not(:has(tr))").all():
        tds = r.locator("td")
        if tds.count() >= 5 and r.locator("a[href]").count() >= 1:
            c0 = tds.nth(0).inner_text().strip().rstrip(".")
            if c0.isdigit():
                out.append(r)
    return out


def harvest_current_table(page, context, pool, store, stats, tag: str):
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies())
    page_num = 0
    last_first_sno = None
    total_added_here = 0

    while store.real_count < TARGET_ROWS:
        page_num += 1
        data_rows = find_data_rows(page)
        if not data_rows:
            break

        first_sno = data_rows[0].locator("td").nth(0).inner_text().strip()
        if first_sno == last_first_sno:
            break
        last_first_sno = first_sno

        candidates = []
        for row in data_rows:
            cells = [c.strip() for c in row.locator("td").all_inner_texts()]
            if len(cells) < 5:
                continue

            # Find publish & closing dates across columns
            dates = [parse_date(c) for c in cells[1:5]]
            valid_dates = [d for d in dates if d is not None]
            pub = valid_dates[0] if len(valid_dates) >= 1 else None
            close = valid_dates[1] if len(valid_dates) >= 2 else None

            title_cell = cells[4] if len(cells) > 4 else cells[-2]
            org_chain = cells[5] if len(cells) > 5 else cells[-1]

            brackets = re.findall(r"\[([^\]]+)\]", title_cell)
            ext_id = brackets[-1].strip() if brackets else ""
            nit_no = brackets[-2].strip() if len(brackets) >= 2 else (ext_id or title_cell[:80].strip())
            if not nit_no or SYNTHETIC_RE.match(nit_no):
                continue

            tender_id = f"EPROC-{tag[:4].upper()}-{ext_id}" if ext_id else f"EPROC-{tag[:4].upper()}-" + hashlib.md5(
                f"{nit_no}|{org_chain}".encode()
            ).hexdigest()[:12]

            if store.has(tender_id, nit_no):
                stats["dup"] += 1
                continue

            if pub is None or close is None:
                stats["reasons"]["missing_dates"] += 1
                stats["skipped"] += 1
                continue

            window = (close.date() - pub.date()).days
            if window < 0 or window > 365:
                stats["reasons"]["implausible_window"] += 1
                stats["skipped"] += 1
                continue

            href = row.locator("a[href]").first.get_attribute("href")
            if not href:
                continue
            full_url = (
                href
                if href.startswith("http")
                else f"https://eproc.rajasthan.gov.in{href if href.startswith('/') else '/nicgep/' + href}"
            )
            candidates.append((tender_id, nit_no[:120], org_chain, title_cell, window, full_url))

        fetched = list(pool.map(lambda c: fetch_detail(c[5], cookie_header), candidates))
        page_added = 0
        for (tender_id, nit_no, org_chain, title_cell, window, _), (amount, detail_txt) in zip(candidates, fetched):
            if store.real_count >= TARGET_ROWS:
                break
            if amount is None:
                stats["reasons"]["na_or_zero_amount"] += 1
                stats["skipped"] += 1
                continue
            district = detect_district(org_chain, title_cell, detail_txt)
            if not district:
                stats["reasons"]["no_district"] += 1
                stats["skipped"] += 1
                continue
            store.add(compute_row(tender_id, nit_no, org_chain, district, amount, window))
            stats["added"] += 1
            page_added += 1
            total_added_here += 1

        if page_added > 0 or page_num == 1:
            print(
                f"  [{tag}] p{page_num}: +{page_added} new | Total verified: {store.real_count}",
                flush=True,
            )

        nxt = page.locator("a[id^='linkFwd'], a:has-text('Next')").first
        if not nxt.count():
            break
        nxt.click()
        page.wait_for_load_state("domcontentloaded", timeout=45000)
        page.wait_for_timeout(600)

    return total_added_here


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting zero-CAPTCHA eProc harvest | Existing verified rows: {start_total}", flush=True)
    stats = {"added": 0, "dup": 0, "skipped": 0, "reasons": Counter()}
    dialogs = []

    with sync_playwright() as pw, ThreadPoolExecutor(max_workers=8) as pool:
        browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

        try:
            for label, url in ZERO_CAPTCHA_PAGES:
                if store.real_count >= TARGET_ROWS:
                    break
                print(f"\n=== Checking {label} (no CAPTCHA) ===", flush=True)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1200)

                # Case 1: Page directly shows a table of tenders
                if find_data_rows(page):
                    harvest_current_table(page, context, pool, store, stats, label)
                    continue

                # Case 2: Page has sub-links (e.g. Closing Today / Within 7 days)
                sub_links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href]'))
                        .filter(a => a.href.includes('service=direct') || a.href.includes('page='))
                        .map(a => ({ text: (a.innerText || '').trim(), href: a.href }))
                        .filter(x => x.text.length > 3 && !/home|login|help|faq|contact|site|download/i.test(x.text));
                }""")

                # Case 3: Page has a Search form with dropdowns (e.g. Awarded / Cancelled by Organisation)
                selects = page.locator("select").all()
                search_btn = page.locator("input[type='submit'][value*='Search' i], input[name='Search'], button:has-text('Search')").first

                if search_btn.count() and selects:
                    # Find the largest dropdown (usually Organisation Name)
                    best_sel_idx = 0
                    best_options = []
                    for idx, sel in enumerate(selects):
                        opts = [o.strip() for o in sel.locator("option").all_inner_texts() if o.strip() and "select" not in o.lower()]
                        if len(opts) > len(best_options):
                            best_options = opts
                            best_sel_idx = idx

                    print(f"  Found filter dropdown with {len(best_options)} options. Iterating automatically...", flush=True)
                    for opt_idx, opt_label in enumerate(best_options, 1):
                        if store.real_count >= TARGET_ROWS:
                            break
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(700)
                        dialogs.clear()
                        try:
                            page.locator("select").nth(best_sel_idx).select_option(label=opt_label)
                            page.locator("input[type='submit'][value*='Search' i], input[name='Search'], button:has-text('Search')").first.click()
                            page.wait_for_load_state("domcontentloaded", timeout=45000)
                            page.wait_for_timeout(900)
                        except Exception:
                            continue

                        if find_data_rows(page):
                            added_org = harvest_current_table(page, context, pool, store, stats, f"{label}:{opt_label[:22]}")
                            if added_org > 0 or opt_idx % 15 == 0:
                                print(f"  [{opt_idx}/{len(best_options)}] {opt_label[:32]}: +{added_org} | Total: {store.real_count}", flush=True)
                elif sub_links:
                    seen_hrefs = set()
                    for sl in sub_links:
                        if store.real_count >= TARGET_ROWS or sl["href"] in seen_hrefs:
                            continue
                        seen_hrefs.add(sl["href"])
                        try:
                            page.goto(sl["href"], wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(900)
                            if find_data_rows(page):
                                harvest_current_table(page, context, pool, store, stats, f"{label}:{sl['text'][:22]}")
                        except Exception:
                            continue
                else:
                    body_preview = " | ".join(l.strip() for l in page.inner_text("body").splitlines() if l.strip())[:220]
                    print(f"  No direct table or filter form found. Page text: {body_preview}", flush=True)

        except KeyboardInterrupt:
            print("\nStopped early — all harvested rows are already saved on disk.", flush=True)
        finally:
            browser.close()

    print(f"\nZero-CAPTCHA harvest complete! Total verified real rows on disk: {store.real_count} (+{store.real_count - start_total} new)", flush=True)
    if stats["reasons"]:
        print("Skip breakdown:", dict(stats["reasons"]), flush=True)


if __name__ == "__main__":
    main()
