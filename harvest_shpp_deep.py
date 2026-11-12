#!/usr/bin/env python3
import re
import ssl
import urllib.request
from collections import Counter
from playwright.sync_api import sync_playwright
from import_shpp_har import Store, find_record_lists, extract_from_records

TARGET_ROWS = 20000
SHPP_URL = "https://shpp.rajasthan.gov.in/advance-search"


def check_cppp():
    print("\n=== Checking CPPP Public Pages (Corrected URLs) ===", flush=True)
    ctx = ssl._create_unverified_context()
    for u in [
        "https://eprocure.gov.in/cppp/latestactivetendersnew",
        "https://eprocure.gov.in/cppp/resultoftendersnew",
    ]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, context=ctx, timeout=15).read().decode("utf-8", "ignore")
            cap = "captcha" in html.lower()
            raj = html.lower().count("rajasthan")
            print(f"  OK | captcha={str(cap):5s} | rajasthan_hits={raj:3d} | len={len(html):6d} | {u}", flush=True)
        except Exception as e:
            print(f"  ERR | {u} -> {e}", flush=True)


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting deep zero-CAPTCHA SHPP harvest | Existing verified rows: {start_total} | Target: {TARGET_ROWS}", flush=True)
    total_skips = Counter()
    batch_stats = {"raw": 0, "added": 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 950})

        def handle_response(resp):
            if "tenderuploadservice" not in resp.url or resp.status != 200:
                return
            try:
                data = resp.json()
            except Exception:
                return
            rec_lists = find_record_lists(data)
            for recs in rec_lists:
                batch_stats["raw"] += len(recs)
                rows, skips = extract_from_records(recs)
                total_skips.update(skips)
                for r in rows:
                    if store.real_count >= TARGET_ROWS:
                        break
                    if not store.has(r["tender_id"], r["nit_no"]):
                        store.add(r)
                        batch_stats["added"] += 1

        page.on("response", handle_response)

        try:
            page.goto(SHPP_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
            print(f"Initial load captured: +{batch_stats['added']} new (from {batch_stats['raw']} raw) | Total: {store.real_count}", flush=True)

            # 1. Discover and click all tabs/pills on the advance-search page
            tab_labels = page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('a, button, li, div[role="tab"], span'));
                return Array.from(new Set(
                    els.map(e => (e.innerText || '').trim())
                       .filter(t => t.length >= 4 && t.length <= 35 && /bid|tender|sppp|award|corrigendum|archive|cancel|result/i.test(t))
                ));
            }""")
            print("Discovered UI tab candidates:", tab_labels, flush=True)

            for label in tab_labels:
                if store.real_count >= TARGET_ROWS:
                    break
                batch_stats["raw"] = 0
                batch_stats["added"] = 0
                loc = page.locator(f"text='{label}'").first
                if not loc.count() or not loc.is_visible():
                    continue
                try:
                    loc.click(force=True)
                    page.wait_for_timeout(3000)
                    if batch_stats["raw"] > 0:
                        print(
                            f"  Tab '{label}': +{batch_stats['added']} new (from {batch_stats['raw']} raw) | Total: {store.real_count}",
                            flush=True,
                        )
                except Exception:
                    continue

            # 2. Iterate through Department / District dropdowns inside Archived Bids & Latest Bids
            #    to bypass SHPP's ~2,000-row cap per unfiltered search
            for target_tab in ["Archived Bids", "Latest Bids", "SPPP Bids"]:
                if store.real_count >= TARGET_ROWS:
                    break
                tab_loc = page.locator(f"text='{target_tab}'").first
                if not tab_loc.count():
                    continue
                tab_loc.click(force=True)
                page.wait_for_timeout(2000)

                selects = page.locator("select").all()
                print(f"\n=== Filtering '{target_tab}' across {len(selects)} dropdowns ===", flush=True)
                for s_idx, sel in enumerate(selects):
                    if store.real_count >= TARGET_ROWS:
                        break
                    opts = [
                        o.strip()
                        for o in sel.locator("option").all_inner_texts()
                        if o.strip() and not re.match(r"^(select|all|--)", o.strip(), re.I)
                    ]
                    if len(opts) < 5:
                        continue
                    print(f"  Dropdown #{s_idx + 1} has {len(opts)} filter options. Stepping through...", flush=True)
                    for o_idx, opt in enumerate(opts, 1):
                        if store.real_count >= TARGET_ROWS:
                            break
                        batch_stats["raw"] = 0
                        batch_stats["added"] = 0
                        try:
                            page.locator("select").nth(s_idx).select_option(label=opt)
                            search_btn = page.locator("button:has-text('Search'), input[type='submit'][value*='Search' i]").first
                            if search_btn.count():
                                search_btn.click(force=True)
                            page.wait_for_timeout(1800)
                        except Exception:
                            continue

                        if batch_stats["added"] > 0 or o_idx % 15 == 0:
                            print(
                                f"    [{target_tab} | {o_idx}/{len(opts)}] {opt[:32]}: +{batch_stats['added']} new | Total: {store.real_count}",
                                flush=True,
                            )
                    # Reset dropdown before next filter
                    try:
                        page.locator("select").nth(s_idx).select_option(index=0)
                    except Exception:
                        pass

        except KeyboardInterrupt:
            print("\nStopped early — all rows harvested so far are saved on disk.", flush=True)
        finally:
            browser.close()

    print(f"\nDeep SHPP harvest complete! Total verified real rows now on disk: {store.real_count} (+{store.real_count - start_total} new)", flush=True)
    if total_skips:
        print("Skip breakdown:", dict(total_skips), flush=True)
    check_cppp()


if __name__ == "__main__":
    main()
