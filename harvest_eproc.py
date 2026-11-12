#!/usr/bin/env python3
import hashlib
import html as html_lib
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


def parse_detail_html(raw_html: str) -> tuple[float | None, str]:
    clean = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw_html))
    clean = re.sub(r"\s+", " ", clean)
    m = re.search(
        r"(?:Tender Value in\s*₹?|Estimated Cost|Cost of Work|Tender Value)\s*[:\-]?\s*(NA|[0-9,]+(?:\.[0-9]+)?(?:\s*(?:Cr|Crore|Lac|Lakh))?)",
        clean,
        re.I,
    )
    amt = parse_amount(m.group(1)) if (m and m.group(1).upper() != "NA") else None
    return amt, clean[:3500]


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting live eProc harvest | Existing verified rows: {start_total}", flush=True)
    stats = {"added": 0, "dup": 0, "skipped": 0, "reasons": Counter()}
    checked = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        page = context.new_page()

        try:
            page.goto(ORG_INDEX_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)

            orgs = []
            for r in page.locator("tr:not(:has(tr))").all():
                cells = [c.strip() for c in r.locator("td").all_inner_texts()]
                link = r.locator("a[href]").last
                if len(cells) == 3 and cells[0].rstrip(".").isdigit() and link.count():
                    cnt = int(re.sub(r"\D", "", cells[2]) or 0)
                    if cnt > 0:
                        orgs.append((cells[1], cnt))

            orgs.sort(key=lambda x: x[1], reverse=True)
            print(f"Found {len(orgs)} organisations ({sum(c for _, c in orgs)} active tenders). Top 5: {orgs[:5]}", flush=True)

            for org_idx, (org_name, expected_cnt) in enumerate(orgs, 1):
                if store.real_count >= TARGET_ROWS:
                    break

                page.goto(ORG_INDEX_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(800)

                clicked = False
                for r in page.locator("tr:not(:has(tr))").all():
                    cells = [c.strip() for c in r.locator("td").all_inner_texts()]
                    if len(cells) == 3 and cells[1] == org_name:
                        link = r.locator("a[href]").last
                        if link.count():
                            link.click()
                            clicked = True
                            break
                if not clicked:
                    continue

                page.wait_for_load_state("domcontentloaded", timeout=45000)
                page.wait_for_timeout(800)

                org_added = 0
                while store.real_count < TARGET_ROWS:
                    data_rows = []
                    for r in page.locator("tr:not(:has(tr))").all():
                        tds = r.locator("td")
                        if tds.count() >= 6 and r.locator("a[href]").count() >= 1:
                            c0 = tds.nth(0).inner_text().strip().rstrip(".")
                            if c0.isdigit():
                                data_rows.append(r)

                    if not data_rows:
                        break

                    for row in data_rows:
                        if store.real_count >= TARGET_ROWS:
                            break
                        checked += 1
                        cells = [c.strip() for c in row.locator("td").all_inner_texts()]
                        pub = parse_date(cells[1])
                        close = parse_date(cells[2])
                        title_cell = cells[4]
                        org_chain = cells[5]

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

                        href = row.locator("a[href]").first.get_attribute("href")
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
                            resp = context.request.get(full_url, timeout=10000)
                            amount, detail_txt = parse_detail_html(resp.text())
                        except Exception:
                            stats["reasons"]["detail_timeout"] += 1
                            stats["skipped"] += 1
                            continue

                        if amount is None:
                            stats["reasons"]["na_or_zero_amount"] += 1
                            stats["skipped"] += 1
                        else:
                            district = detect_district(org_chain, title_cell, detail_txt)
                            if not district:
                                stats["reasons"]["no_district"] += 1
                                stats["skipped"] += 1
                            else:
                                store.add(compute_row(tender_id, nit_no[:120], org_chain or org_name, district, amount, window))
                                stats["added"] += 1
                                org_added += 1

                        if checked % 15 == 0:
                            print(
                                f"  [{org_name[:22]}] checked={checked} | +{stats['added']} added | Total={store.real_count} | skips={dict(stats['reasons'])}",
                                flush=True,
                            )

                    nxt = page.locator("a[id^='linkFwd'], a:has-text('Next')").first
                    if not nxt.count():
                        break
                    nxt.click()
                    page.wait_for_load_state("domcontentloaded", timeout=45000)
                    page.wait_for_timeout(800)

                print(f"[{org_idx}/{len(orgs)}] {org_name[:35]} ({expected_cnt} listed): +{org_added} new | Total: {store.real_count}", flush=True)

        except KeyboardInterrupt:
            print("\nStopped early — all rows harvested so far are saved on disk.", flush=True)
        finally:
            browser.close()

    print(f"\neProc harvest finished! Total verified real rows now on disk: {store.real_count} (+{store.real_count - start_total} new)", flush=True)
    if stats["reasons"]:
        print("Final skip breakdown:", dict(stats["reasons"]), flush=True)


if __name__ == "__main__":
    main()
