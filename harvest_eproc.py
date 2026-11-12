#!/usr/bin/env python3
import hashlib
import re
from collections import Counter
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
ORG_INDEX_URL = "https://eproc.rajasthan.gov.in/nicgep/app?page=FrontEndTendersByOrganisation&service=page"


def extract_detail_amount_and_text(detail_page) -> tuple[float | None, str]:
    body = detail_page.inner_text("body")
    m = re.search(
        r"(?:Tender Value in\s*₹|Estimated Cost|Tender Value)\s*[:\-]?\s*([^\n\r]{1,60})",
        body,
        re.I,
    )
    amt = parse_amount(m.group(1)) if m else None
    return amt, body[:2500]


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting automated eProc harvest | Existing verified rows: {start_total} | Target: {TARGET_ROWS}")
    stats = {"added": 0, "dup": 0, "skipped": 0, "reasons": Counter()}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        detail_page = context.new_page()

        try:
            page.goto(ORG_INDEX_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            # Collect all organisation links and counts from the index table
            org_rows = page.locator("table#table tbody tr, table.list_table tbody tr").all()
            orgs = []
            for r in org_rows:
                cells = [c.strip() for c in r.locator("td").all_inner_texts()]
                link = r.locator("a[href]").first
                if len(cells) >= 3 and link.count():
                    name = cells[1]
                    cnt_txt = re.sub(r"\D", "", cells[-1])
                    cnt = int(cnt_txt) if cnt_txt else 0
                    if cnt > 0:
                        orgs.append((name, cnt))

            print(f"Found {len(orgs)} active Rajasthan organisations on eProc (total listed: {sum(c for _, c in orgs)}).")

            for org_idx, (org_name, expected_cnt) in enumerate(orgs, 1):
                if store.real_count >= TARGET_ROWS:
                    break

                # Reload org index cleanly so Tapestry session links stay fresh
                page.goto(ORG_INDEX_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1200)

                target_row = page.locator(f"tr:has-text('{org_name[:35]}')").first
                link = target_row.locator("a[href]").first if target_row.count() else None
                if not link or not link.count():
                    continue

                print(f"\n[{org_idx}/{len(orgs)}] {org_name} ({expected_cnt} tenders)...")
                link.click()
                page.wait_for_load_state("domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)

                pageno = 0
                while store.real_count < TARGET_ROWS:
                    pageno += 1
                    rows = page.locator("table#table tbody tr, table.list_table tbody tr").all()
                    if not rows:
                        break

                    page_added = 0
                    for row in rows:
                        if store.real_count >= TARGET_ROWS:
                            break
                        cells = [c.strip() for c in row.locator("td").all_inner_texts()]
                        if len(cells) < 6:
                            continue

                        pub = parse_date(cells[1])
                        close = parse_date(cells[2])
                        title_cell = cells[4]
                        org_chain = cells[5]

                        # Extract bracketed Tender ID like [2026_RSRDC_605754_1]
                        brackets = re.findall(r"\[([^\]]+)\]", title_cell)
                        ext_id = brackets[-1].strip() if brackets else ""
                        nit_no = brackets[-2].strip() if len(brackets) >= 2 else (ext_id or title_cell[:80].strip())
                        if not nit_no or SYNTHETIC_RE.match(nit_no):
                            continue

                        tender_id = f"EPROC-{ext_id}" if ext_id else "EPROC-" + hashlib.md5(
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

                        # Open detail link to get exact Tender Value in ₹ and location
                        a_tag = row.locator("a[href]").first
                        href = a_tag.get_attribute("href") if a_tag.count() else None
                        if not href:
                            stats["reasons"]["no_detail_link"] += 1
                            stats["skipped"] += 1
                            continue

                        full_url = (
                            href
                            if href.startswith("http")
                            else f"https://eproc.rajasthan.gov.in{href if href.startswith('/') else '/nicgep/' + href}"
                        )
                        try:
                            detail_page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
                            amount, detail_txt = extract_detail_amount_and_text(detail_page)
                        except Exception:
                            stats["reasons"]["detail_timeout"] += 1
                            stats["skipped"] += 1
                            continue

                        if amount is None:
                            stats["reasons"]["zero_or_missing_amount"] += 1
                            stats["skipped"] += 1
                            continue

                        district = detect_district(org_chain, title_cell, detail_txt)
                        if not district:
                            stats["reasons"]["no_district"] += 1
                            stats["skipped"] += 1
                            continue

                        store.add(compute_row(tender_id, nit_no[:120], org_chain or org_name, district, amount, window))
                        stats["added"] += 1
                        page_added += 1

                    print(f"  Page {pageno}: +{page_added} new | Total verified: {store.real_count}")

                    # Click Next page if available
                    nxt = page.locator("a#linkFwd, a:has-text('Next')").first
                    if not nxt.count():
                        break
                    nxt.click()
                    page.wait_for_load_state("domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1200)

        except KeyboardInterrupt:
            print("\nStopped early — all rows harvested so far are already saved on disk.")
        finally:
            browser.close()

    print(f"\neProc harvest finished! Total verified real rows now on disk: {store.real_count} (+{store.real_count - start_total} new)")
    if stats["reasons"]:
        print("Skip breakdown:", dict(stats["reasons"]))


if __name__ == "__main__":
    main()
