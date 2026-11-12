#!/usr/bin/env python3
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from huggingface_hub import HfApi, hf_hub_download
from import_shpp_har import (
    Store,
    compute_row,
    detect_district,
    parse_amount,
    parse_date,
    SYNTHETIC_RE,
)

REPO_ID = "fireboy21/tenders_aoc"
TARGET_ROWS = 20000
TMP_DIR = "/tmp/hf_tenders"

RAJ_ORG_HINTS = (
    "rajasthan", "jaipur", "jodhpur", "udaipur", "kota", "ajmer", "bikaner",
    "bharatpur", "alwar", "bhilwara", "sikar", "pali", "barmer", "nagaur",
    "chittorgarh", "jhunjhunu", "jhalawar", "banswara", "bundi", "churu",
    "dausa", "dholpur", "dungarpur", "hanumangarh", "jaisalmer", "jalore",
    "karauli", "pratapgarh", "rajsamand", "sawai madhopur", "sirohi",
    "ganganagar", "tonk", "rsrdc", "jvvnl", "jdvvnl", "avvnl",
    "phed", "rsmml", "rsamb", "ruhs", "risl", "rcdf", "rmscl", "riico"
)


def clean_str(val) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(str(val))).strip()


def first_field(d: dict, keys: list[str]) -> str:
    low_map = {k.lower().strip(): v for k, v in d.items()}
    for k in keys:
        v = low_map.get(k.lower())
        if v not in (None, "", "NA", "N/A", "null"):
            return clean_str(v)
    return ""


def process_tenders_db(db_path: str, store: Store, stats: Counter, target_year: int):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not {"tenders", "tender_details"}.issubset(tables):
        con.close()
        return

    yr_str = str(target_year)
    print(f"Querying tenders + tender_details for year {target_year} Rajasthan notices...", flush=True)
    sql = f"""
        SELECT m.*, d.details_json
        FROM tenders m
        JOIN tender_details d ON m.internal_id = d.internal_id
        WHERE (m.e_published_date LIKE '%{yr_str}%' OR d.details_json LIKE '%{yr_str}%')
          AND (
              lower(COALESCE(m.detail_url, '')) LIKE '%eproc.rajasthan%'
              OR lower(COALESCE(m.organisation_name, '')) LIKE '%rajasthan%'
              OR lower(COALESCE(d.details_json, '')) LIKE '%rajasthan%'
          )
    """
    for row in con.execute(sql):
        if store.real_count >= TARGET_ROWS:
            break
        stats["scanned"] += 1
        raw_json = row["details_json"]
        if not raw_json:
            continue
        try:
            det = json.loads(raw_json)
            if isinstance(det, str):
                det = json.loads(det)
        except Exception:
            continue
        if not isinstance(det, dict):
            continue

        org = first_field(det, ["Organisation Name", "Organisation Chain"]) or clean_str(row["organisation_name"])
        title = first_field(det, ["Tender Title", "Work Description", "Tender Description"])
        loc = first_field(det, ["Location", "Address"])
        detail_url = clean_str(row["detail_url"]) if "detail_url" in row.keys() else ""

        haystack = f"{detail_url} {org} {loc}".lower()
        if "eproc.rajasthan" not in haystack and not any(h in haystack for h in RAJ_ORG_HINTS):
            continue

        nit_no = first_field(det, ["Tender Ref. No.", "Tender Reference Number"]) or (
            clean_str(row["tender_ref_no"]) if "tender_ref_no" in row.keys() else ""
        )
        ext_tid = clean_str(row["tender_id"]) if "tender_id" in row.keys() else ""
        if not nit_no:
            nit_no = ext_tid or title[:80]
        if not nit_no or SYNTHETIC_RE.match(nit_no):
            continue

        tender_id = f"HF-TND-{ext_tid}" if ext_tid else "HF-TND-" + hashlib.md5(
            f"{nit_no}|{org}".encode()
        ).hexdigest()[:12]

        if store.has(tender_id, nit_no):
            stats["dup"] += 1
            continue

        amt_str = first_field(det, ["Tender Value in ₹", "Tender Value", "Estimated Cost"])
        amount = parse_amount(amt_str)
        if amount is None:
            stats["no_amount"] += 1
            continue

        pub_str = first_field(det, ["ePublished Date", "Published Date"]) or (
            clean_str(row["e_published_date"]) if "e_published_date" in row.keys() else ""
        )
        close_str = first_field(det, ["Bid Submission End Date", "Bid Opening Date"])
        pub = parse_date(pub_str)
        close = parse_date(close_str)
        if pub is None or close is None or pub.year != target_year:
            stats["date_skip"] += 1
            continue

        window = (close.date() - pub.date()).days
        if window < 0 or window > 365:
            stats["bad_window"] += 1
            continue

        district = detect_district(org, loc, title)
        if not district:
            stats["no_district"] += 1
            continue

        store.add(compute_row(tender_id, nit_no[:120], org, district, amount, window))
        stats["added"] += 1
        stats[f"yr_{target_year}"] += 1

        if stats["added"] % 500 == 0:
            print(
                f"  [{target_year}] +{stats['added']:5d} added | Total verified: {store.real_count:5d} / {TARGET_ROWS}",
                flush=True,
            )

    con.close()


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting 2026->2025 Rajasthan import | Existing rows: {start_total} | Target: {TARGET_ROWS}", flush=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    stats = Counter()

    try:
        api = HfApi()
        db_files = [
            item.rfilename
            for item in api.list_repo_tree(REPO_ID, repo_type="dataset", recursive=True)
            if item.rfilename.endswith("tenders_vps.db") or item.rfilename.endswith(".db")
        ]
        # Prefer tenders_vps.db first since it holds true Bid Submission End Dates
        db_files.sort(key=lambda n: (0 if "tenders_vps" in n else 1, n))

        for fname in db_files:
            if store.real_count >= TARGET_ROWS:
                break
            if "aoc" in fname:
                continue
            print(f"\nDownloading {fname} to {TMP_DIR}...", flush=True)
            local_db = hf_hub_download(
                repo_id=REPO_ID,
                filename=fname,
                repo_type="dataset",
                local_dir=TMP_DIR,
            )
            for yr in (2026, 2025, 2024):
                if store.real_count >= TARGET_ROWS:
                    break
                process_tenders_db(local_db, store, stats, yr)
            try:
                os.remove(local_db)
            except OSError:
                pass
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\nImport complete! Total verified real rows: {store.real_count} (+{store.real_count - start_total} new)", flush=True)
    print("Breakdown:", dict(stats), flush=True)


if __name__ == "__main__":
    main()
