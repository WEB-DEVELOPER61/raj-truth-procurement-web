#!/usr/bin/env python3
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from huggingface_hub import hf_hub_download
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
        if any(t in k for t in ("contract value", "awarded value", "tender value", "estimated cost", "cost of work")):
            if "fee" not in k and "emd" not in k:
                amt = parse_amount(v)
                if amt is not None:
                    return amt
    for col in ("contract_value", "awarded_value", "tender_value", "amount"):
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
            if any(t in k for t in ("bid opening date", "contract date", "award date")):
                close = parse_date(v)
                if close:
                    break
    return pub, close


def process_aoc_db(db_path: str, store: Store, stats: Counter):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cols = {r[1] for r in cur.execute("PRAGMA table_info(aoc_tenders)").fetchall()}
    print(f"Detected aoc_tenders columns: {sorted(cols)}", flush=True)

    where_parts = []
    if "portal_type" in cols:
        where_parts.append("m.portal_type = 'state'")
    if "detail_url" in cols:
        where_parts.append("(m.detail_url LIKE '%eproc.rajasthan%' OR d.details_json LIKE '%rajasthan%')")
    else:
        where_parts.append("d.details_json LIKE '%rajasthan%'")

    base_where = " AND ".join(where_parts)

    # Query newest years first (2026 -> 2025 -> 2024) so rows stream immediately without buffering
    year_filters = [f"m.year = {yr}" for yr in (2026, 2025, 2024)] if "year" in cols else ["1=1"]

    for yf in year_filters:
        if store.real_count >= TARGET_ROWS:
            break
        print(f"Streaming Rajasthan state AOC records ({yf})...", flush=True)
        sql = f"""
            SELECT m.*, d.details_json
            FROM aoc_tenders m
            JOIN aoc_details d ON m.internal_id = d.internal_id
            WHERE {base_where} AND {yf}
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

            flat = flatten_dict(det)
            row_dict = dict(row)

            org = pick_field(flat, ("organisation name", "organisation chain", "department")) or clean_str(row_dict.get("organisation_name"))
            title = pick_field(flat, ("tender title", "work description", "tender description", "title"))
            loc = pick_field(flat, ("location", "address"))
            detail_url = clean_str(row_dict.get("detail_url"))

            haystack = f"{detail_url} {org} {loc}".lower()
            if "eproc.rajasthan" not in haystack and not any(h in haystack for h in RAJ_ORG_HINTS):
                stats["non_raj"] += 1
                continue

            nit_no = pick_field(flat, ("tender ref. no.", "tender reference number", "tender ref.", "nit no")) or clean_str(row_dict.get("tender_ref_no"))
            ext_tid = clean_str(row_dict.get("tender_id")) or pick_field(flat, ("tender id",))
            if not nit_no:
                nit_no = ext_tid or title[:80]
            if not nit_no or SYNTHETIC_RE.match(nit_no) or TEST_NOISE_RE.search(f"{nit_no} {title}"):
                stats["noise_skip"] += 1
                continue

            tender_id = f"HF-AOC-{ext_tid}" if ext_tid else "HF-AOC-" + hashlib.md5(
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

            store.add(compute_row(tender_id, nit_no[:120], org, district, amount, window))
            stats["added"] += 1
            stats[f"yr_{pub.year}"] += 1

            if stats["added"] % 500 == 0:
                print(
                    f"  +{stats['added']:5d} added | Total verified: {store.real_count:5d} / {TARGET_ROWS} (scanned={stats['scanned']})",
                    flush=True,
                )

    con.close()


def main():
    store = Store()
    start_total = store.real_count
    print(f"Starting AOC state-portal Rajasthan import | Existing rows: {start_total} | Target: {TARGET_ROWS}", flush=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    stats = Counter()

    try:
        print("Downloading aoc_tenders.db to /tmp/hf_tenders...", flush=True)
        local_db = hf_hub_download(
            repo_id=REPO_ID,
            filename="aoc_tenders.db",
            repo_type="dataset",
            local_dir=TMP_DIR,
        )
        process_aoc_db(local_db, store, stats)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\nImport complete! Total verified real rows: {store.real_count} (+{store.real_count - start_total} new)", flush=True)
    print("Breakdown:", dict(stats), flush=True)


if __name__ == "__main__":
    main()
