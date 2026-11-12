#!/usr/bin/env python3
from collections import Counter
from playwright.sync_api import sync_playwright
from import_shpp_har import Store, find_record_lists, extract_from_records

TARGET_ROWS = 20000
TABS = ["Archived Bids", "Cancelled Bids", "Latest Bids"]

DATE_SLICES = [
    ("2026-09-01", "2026-11-12"),
    ("2026-07-01", "2026-08-31"),
    ("2026-05-01", "2026-06-30"),
    ("2026-03-01", "2026-04-30"),
    ("2026-01-01", "2026-02-28"),
    ("2025-10-01", "2025-12-31"),
    ("2025-07-01", "2025-09-30"),
    ("2025-04-01", "2025-06-30"),
    ("2025-01-01", "2025-03-31"),
    ("2024-07-01", "2024-12-31"),
    ("2024-01-01", "2024-06-30"),
]


def open_publish_date_and_search(page, start_iso: str, end_iso: str) -> bool:
    # Click the Publish Date field box to open the From/To date popover
    if page.locator("input[type='date']").count() < 2:
        clicked = page.evaluate("""() => {
            const labels = Array.from(document.querySelectorAll('label, span, p, div'));
            const lbl = labels.find(el => el.textContent && el.textContent.trim() === 'Publish Date');
            if (lbl && lbl.parentElement) {
                const target = lbl.parentElement.querySelector('input, [role="button"], div[class*="date"], div[class*="select"], div[class*="input"]') || lbl.parentElement;
                target.click();
                return true;
            }
            return false;
        }""")
        if clicked:
            page.wait_for_timeout(500)

    date_inputs = page.locator("input[type='date']")
    if date_inputs.count() >= 2:
        date_inputs.nth(0).fill(start_iso)
        date_inputs.nth(1).fill(end_iso)
        done_btn = page.locator("button:has-text('Done')").first
        if done_btn.count() and done_btn.is_visible():
            done_btn.click(force=True)
            page.wait_for_timeout(300)

    search_btn = page.locator("button:has-text('Search')").first
    if search_btn.count():
        search_btn.click(force=True)
        return True
    return False


def click_next_page(page) -> bool:
    for sel in [
        "button[aria-label*='Next' i]",
        "a[aria-label*='Next' i]",
        "li.pagination-next:not(.disabled) a",
        "button:has-text('Next')",
        "a:has-text('Next')",
        "button:has-text('›')",
        "a:has-text('›')",
    ]:
        btn = page.locator(sel).first
        try:
            if not btn.count() or not btn.is_visible():
                continue
            disabled = btn.get_attribute("disabled") is not None
            cls = (btn.get_attribute("class") or "").lower()
            parent_cls = btn.evaluate("e => (e.parentElement ? e.parentElement.className : '').toLowerCase()")
            if disabled or "disabled" in cls or "disabled" in parent_cls:
                return False
            btn.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting automated SHPP harvest | Existing verified rows: {start_total} | Target: {TARGET_ROWS}")

    stats = {"added": 0, "dup": 0, "skipped": 0, "reasons": Counter()}
    batch_added = 0
    seen_endpoints = set()

    def handle_response(resp):
        nonlocal batch_added
        if "tenderuploadservice" not in resp.url or resp.status != 200:
            return
        endpoint = resp.url.split("?")[0].split("/")[-1]
        if endpoint not in seen_endpoints:
            seen_endpoints.add(endpoint)
            print(f"  [API] Discovered endpoint: {endpoint}")
        try:
            payload = resp.json()
        except Exception:
            return
        before = store.real_count
        for rec_list in find_record_lists(payload):
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
            print(f"Initial load captured: +{batch_added} | Total verified: {store.real_count}")

            for tab_name in TABS:
                if store.real_count >= TARGET_ROWS:
                    break
                tab = page.locator(f"text='{tab_name}'").first
                if tab.count():
                    print(f"\n=== Switching tab: {tab_name} ===")
                    batch_added = 0
                    tab.click(force=True)
                    page.wait_for_timeout(4500)
                    print(f"  Tab default view: +{batch_added} | Total: {store.real_count}")

                for start_iso, end_iso in DATE_SLICES:
                    if store.real_count >= TARGET_ROWS:
                        break
                    batch_added = 0
                    if not open_publish_date_and_search(page, start_iso, end_iso):
                        continue
                    page.wait_for_timeout(4500)
                    print(f"  [{tab_name}] {start_iso} -> {end_iso} (p1): +{batch_added} | Total: {store.real_count}")

                    for pno in range(2, 26):
                        if store.real_count >= TARGET_ROWS:
                            break
                        prev_batch = batch_added
                        if not click_next_page(page):
                            break
                        page.wait_for_timeout(3500)
                        gained = batch_added - prev_batch
                        print(f"    page {pno}: +{gained} | Total: {store.real_count}")
                        if gained == 0:
                            break
        except KeyboardInterrupt:
            print("\nStopped early — all rows collected so far are saved on disk.")
        finally:
            browser.close()

    print(f"\nHarvest complete! Total verified real rows now on disk: {store.real_count} (+{store.real_count - start_total} new)")
    if stats["reasons"]:
        print("Skip breakdown:", dict(stats["reasons"]))


if __name__ == "__main__":
    main()
