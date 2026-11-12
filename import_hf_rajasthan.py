#!/usr/bin/env python3
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
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

TEST_NOISE_RE = re.compile(r"\b(test\s*\d*|dummy|sample\s*tender)\b", re.I)


def clean_str(val) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(str(val))).strip()


def flatten_dict(obj, out=None) -> dict[str, str]:
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                flatten_dict(v, out)
            elif v not in (None, "", "NA", "N/A", "null", "None"):
                lk = str(k).lower().strip()
                if lk not in out:
                    out[lk] = clean_str(v)
    elif isinstance(obj, list):
        for item in obj:
            flatten_dict(item, out)
    return out


def pick_field(flat: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in flat and flat[k]:
            return flat[k]
    return ""


def extract_amount(flat: dict[str, str], row_dict: dict) -> float | None:
    for k, v in flat.items():
        if any(t in k for t in ("tender value", "contract value", "estimated cost", "awarded value", "cost of work")):
            if "fee" not in k and "emd" not in k:
                amt = parse_amount(v)
                if amt is not None:
                    return amt
    for col in ("tender_value", "contract_value", "awarded_value", "amount"):
        if col in row_dict and row_dict[col] not in (None, "", 0):
            amt = parse_amount(str(row_dict[col]))
            if amt is not None:
                return amt
    return None


def extract_dates(flat: dict[str, str], row_dict: dict):
    pub = None
    close = None
    for k, v in flat.items():
        if pub is None and ("published date" in k or "epublished" in k):
            pub = parse_date(v)
        if close is None and ("submission end date" in k or "closing date" in k):
            close = parse_date(v)
    if pub is None:
        for col in ("e_published_date", "published_date"):
            if col in row_dict and row_dict[col]:
                pub = parse_date(str(row_dict[col]))
                if pub:
                    break
    if close is None:
        for k, v in flat.items():
            if "bid opening date" in k:
                close = parse_date(v)
                if close:
                    break
    return pub, close


def process_db(db_path: str, store: Store, stats: Counter):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    pairs = []
    if {"tenders", "tender_details"}.issubset(tables):
        pairs.append(("tenders", "tender_details", "HF-TND"))
    if {"aoc_tenders", "aoc_details"}.issubset(tables):
        pairs.append(("aoc_tenders", "aoc_details", "HF-AOC"))

    buckets: dict[int, list[dict]] = defaultdict(list)

    for main_tbl, det_tbl, prefix in pairs:
        print(f"Scanning {main_tbl} + {det_tbl} in single pass...", flush=True)
        sql = f"""
            SELECT m.*, d.details_json
            FROM {main_tbl} m
            JOIN {det_tbl} d ON m.internal_id = d.internal_id
            WHERE lower(COALESCE(m.detail_url, '')) LIKE '%eproc.rajasthan%'
               OR lower(COALESCE(m.organisation_name, '')) LIKE '%rajasthan%'
               OR lower(COALESCE(d.details_json, '')) LIKE '%rajasthan%'
        """
        for row in con.execute(sql):
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

            flat = flatten_dict(det)
            row_dict = dict(row)

            org = pick_field(flat, ("organisation name", "organisation chain")) or clean_str(row_dict.get("organisation_name"))
            title = pick_field(flat, ("tender title", "work description", "tender description", "title"))
            loc = pick_field(flat, ("location", "address"))
            detail_url = clean_str(row_dict.get("detail_url"))

            haystack = f"{detail_url} {org} {loc}".lower()
            if "eproc.rajasthan" not in haystack and not any(h in haystack for h in RAJ_ORG_HINTS):
                stats["non_raj"] += 1
                continue

            nit_no = pick_field(flat, ("tender ref. no.", "tender reference number", "nit no")) or clean_str(row_dict.get("tender_ref_no"))
            ext_tid = clean_str(row_dict.get("tender_id")) or pick_field(flat, ("tender id",))
            if not nit_no:
                nit_no = ext_tid or title[:80]
            if not nit_no or SYNTHETIC_RE.match(nit_no) or TEST_NOISE_RE.search(f"{nit_no} {title}"):
                stats["noise_skip"] += 1
                continue

            tender_id = f"{prefix}-{ext_tid}" if ext_tid else f"{prefix}-" + hashlib.md5(
                f"{nit_no}|{org}".encode()
            ).hexdigest()[:12]

            if store.has(tender_id, nit_no):
                stats["dup"] += 1
                continue

            amount = extract_amount(flat, row_dict)
            if amount is None:
                stats["no_amount"] += 1
                continue

            pub, close = extract_dates(flat, row_dict)
            if pub is None or close is None:
                stats["no_dates"] += 1
                continue

            window = (close.date() - pub.date()).days
            if window < 0 or window > 365:
                stats["bad_window"] += 1
                continue

            district = detect_district(org, loc, title)
            if not district:
                stats["no_district"] += 1
                continue

            buckets[pub.year].append(compute_row(tender_id, nit_no[:120], org, district, amount, window))

    con.close()

    # Insert newest years first (2026 -> 2025 -> 2024 -> older) until TARGET_ROWS is reached
    for yr in sorted(buckets.keys(), reverse=True):
        if store.real_count >= TARGET_ROWS:
            break
        added_yr = 0
        for rec in buckets[yr]:
            if store.real_count >= TARGET_ROWS:
                break
            if not store.has(rec["tender_id"], rec["nit_no"]):
                store.add(rec)
                stats["added"] += 1
                stats[f"yr_{yr}"] += 1
                added_yr += 1
        print(f"  Year {yr}: +{added_yr:5d} verified rows added | Total on disk: {store.real_count:5d} / {TARGET_ROWS}", flush=True)


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting recursive-JSON Rajasthan import | Existing rows: {start_total} | Target: {TARGET_ROWS}", flush=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    stats = Counter()

    try:
        api = HfApi()
        db_files = [
            item.rfilename
            for item in api.list_repo_tree(REPO_ID, repo_type="dataset", recursive=True)
            if item.rfilename.endswith(".db")
        ]
        # Run tenders_vps.db first, then aoc_tenders.db if needed to cross 20,000
        db_files.sort(key=lambda n: (0 if "tenders_vps" in n else 1, n))

        for fname in db_files:
            if store.real_count >= TARGET_ROWS:
                break
            print(f"\nDownloading {fname} to {TMP_DIR}...", flush=True)
            local_db = hf_hub_download(
                repo_id=REPO_ID,
                filename=fname,
                repo_type="dataset",
                local_dir=TMP_DIR,
            )
            process_db(local_db, store, stats)
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
