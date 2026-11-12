#!/usr/bin/env python3
from collections import Counter
from playwright.sync_api import sync_playwright
from import_shpp_har import Store, find_record_lists, extract_from_records

TARGET_ROWS = 20000
TABS = ["Archived Bids", "Cancelled Bids", "Latest Bids"]


def go_to_page(page, target_page: int) -> bool:
    clicked = page.evaluate("""(targetPage) => {
        const targetStr = String(targetPage);
        // 1. Try clicking the exact page number button/link in pagination
        const candidates = Array.from(document.querySelectorAll('a, button, li, span'));
        for (const el of candidates) {
            const txt = (el.textContent || '').trim();
            if (txt === targetStr) {
                const parent = el.closest('ul, nav, [class*="paginat"]');
                if (parent) {
                    const disabled = el.hasAttribute('disabled') ||
                        (el.className && String(el.className).toLowerCase().includes('disabled')) ||
                        (el.parentElement && el.parentElement.className && String(el.parentElement.className).toLowerCase().includes('disabled'));
                    if (!disabled) {
                        el.click();
                        return 'page-' + targetStr;
                    }
                }
            }
        }
        // 2. Fallback: click Next / › button
        for (const el of candidates) {
            const txt = (el.textContent || '').trim().toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            if (txt === 'next' || txt === '›' || aria.includes('next')) {
                const disabled = el.hasAttribute('disabled') ||
                    (el.className && String(el.className).toLowerCase().includes('disabled')) ||
                    (el.parentElement && el.parentElement.className && String(el.parentElement.className).toLowerCase().includes('disabled'));
                if (!disabled) {
                    el.click();
                    return 'next';
                }
            }
        }
        return null;
    }""", target_page)
    return clicked is not None


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting paginated SHPP harvest | Existing verified rows: {start_total} | Target: {TARGET_ROWS}")

    stats = {"added": 0, "dup": 0, "skipped": 0, "reasons": Counter()}
    batch_added = 0
    batch_raw = 0

    def handle_response(resp):
        nonlocal batch_added, batch_raw
        if "tenderuploadservice" not in resp.url or resp.status != 200:
            return
        try:
            payload = resp.json()
        except Exception:
            return
        before = store.real_count
        for rec_list in find_record_lists(payload):
            batch_raw += len(rec_list)
            extract_from_records(rec_list, store, stats)
        batch_added += (store.real_count - before)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("response", handle_response)

        try:
            print("Loading SHPP Advance Search...")
            page.goto("https://shpp.rajasthan.gov.in/advance-search", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            for tab_name in TABS:
                if store.real_count >= TARGET_ROWS:
                    break
                tab = page.locator(f"text='{tab_name}'").first
                if not tab.count():
                    continue

                print(f"\n=== Harvesting Tab: {tab_name} ===")
                batch_added = 0
                batch_raw = 0
                tab.click(force=True)
                page.wait_for_timeout(5000)
                print(f"  Page 1: +{batch_added} new (from {batch_raw} raw) | Total verified: {store.real_count}")

                for pno in range(2, 26):
                    if store.real_count >= TARGET_ROWS:
                        break
                    batch_added = 0
                    batch_raw = 0
                    if not go_to_page(page, pno):
                        print(f"  No more pages after page {pno - 1}.")
                        break
                    page.wait_for_timeout(5000)
                    print(f"  Page {pno}: +{batch_added} new (from {batch_raw} raw) | Total verified: {store.real_count}")
                    if batch_raw == 0:
                        print("  Reached end of records for this tab.")
                        break
        except KeyboardInterrupt:
            print("\nStopped early — all collected rows are already saved on disk.")
        finally:
            browser.close()

    print(f"\nHarvest complete! Total verified real rows now on disk: {store.real_count} (+{store.real_count - start_total} new)")
    if stats["reasons"]:
        print("Skip breakdown:", dict(stats["reasons"]))


if __name__ == "__main__":
    main()
