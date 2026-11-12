#!/usr/bin/env python3
import base64
import csv
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

MANUAL_DIR = Path("manual_shpp")
DATA_DIR = Path("data")
DISTRICT_DIR = DATA_DIR / "districts"
SYNTHETIC_RE = re.compile(r"^NIT_\d{2,3}/[A-Z]{3}/202\d")

COLUMNS = [
    "tender_id", "nit_no", "division", "district", "tender_amount", "estimated_cost_lac",
    "actual_window_days", "statutory_min_days", "deficit_days", "is_violation", "risk_score",
]

DISTRICTS = [
    "Ajmer", "Alwar", "Banswara", "Baran", "Barmer", "Bharatpur", "Bhilwara", "Bikaner", "Bundi",
    "Chittorgarh", "Churu", "Dausa", "Dholpur", "Dungarpur", "Hanumangarh", "Jaipur", "Jaisalmer",
    "Jalore", "Jhalawar", "Jhunjhunu", "Jodhpur", "Karauli", "Kota", "Nagaur", "Pali",
    "Pratapgarh", "Rajsamand", "Sawai Madhopur", "Sikar", "Sirohi", "Sri Ganganagar", "Tonk",
    "Udaipur", "Balotra", "Beawar", "Deeg", "Didwana-Kuchaman", "Kotputli-Behror", "Khairthal-Tijara",
    "Phalodi", "Salumbar",
]

ALIASES = {
    "sawai madhopur": "Sawai Madhopur", "s.madhopur": "Sawai Madhopur", "gangapur city": "Sawai Madhopur",
    "sri ganganagar": "Sri Ganganagar",
    "sriganganagar": "Sri Ganganagar",
    "jawahar lal nehru medical college": "Ajmer", "jln medical college": "Ajmer",
    "rabindra nath tagore medical college": "Udaipur", "rnt medical college": "Udaipur",
    "director technical education": "Jodhpur",
    "jobner": "Jaipur", "ruhs": "Jaipur", "shri ganganagar": "Sri Ganganagar", "ganganagar": "Sri Ganganagar",
    "suratgarh": "Sri Ganganagar", "karanpur": "Sri Ganganagar", "raisinghnagar": "Sri Ganganagar",
    "anupgarh": "Sri Ganganagar", "gharsana": "Sri Ganganagar", "padampur": "Sri Ganganagar",
    "chittorgarh": "Chittorgarh", "chittaurgarh": "Chittorgarh", "rawatbhata": "Chittorgarh",
    "nimbahera": "Chittorgarh", "kapasan": "Chittorgarh", "begun": "Chittorgarh",
    "jhalawar": "Jhalawar", "jhalrapatan": "Jhalawar", "bhawani mandi": "Jhalawar", "aklera": "Jhalawar",
    "jalor": "Jalore", "bhinmal": "Jalore", "sanchore": "Jalore",
    "dholpur": "Dholpur", "bari": "Dholpur", "rajkhera": "Dholpur",
    "banswara": "Banswara", "kushalgarh": "Banswara",
    "dungarpur": "Dungarpur", "sagwara": "Dungarpur",
    "pratapgarh": "Pratapgarh", "chhoti sadri": "Pratapgarh",
    "pokaran": "Jaisalmer", "nachana": "Jaisalmer",
    "kekri": "Ajmer", "nasirabad": "Ajmer", "kishangarh": "Ajmer", "avvnl": "Ajmer",
    "kotputli": "Kotputli-Behror", "behror": "Kotputli-Behror", "neemrana": "Kotputli-Behror",
    "kotkasim": "Khairthal-Tijara", "bhiwadi": "Khairthal-Tijara", "tijara": "Khairthal-Tijara",
    "sultanpur": "Kota", "ramganj mandi": "Kota", "sangod": "Kota",
    "girwa": "Udaipur", "kherwara": "Udaipur", "mavli": "Udaipur", "rsmml": "Udaipur",
    "hindaun": "Karauli", "todabhim": "Karauli",
    "mahuwa": "Dausa", "mahwa": "Dausa", "lalsot": "Dausa", "bandikui": "Dausa",
    "deoli": "Tonk", "malpura": "Tonk", "niwai": "Tonk",
    "kuchaman": "Didwana-Kuchaman", "didwana": "Didwana-Kuchaman", "makrana": "Didwana-Kuchaman",
    "merta": "Nagaur", "ladnun": "Nagaur", "jayal": "Nagaur",
    "bali": "Pali", "sumerpur": "Pali", "sojat": "Pali", "jaitaran": "Pali",
    "bhadra": "Hanumangarh", "nohar": "Hanumangarh", "sangaria": "Hanumangarh", "pilibanga": "Hanumangarh",
    "ramgarh": "Alwar", "rajgarh": "Alwar", "laxmangarh": "Alwar", "thanagazi": "Alwar",
    "chomu": "Jaipur", "shahpura": "Jaipur", "sambhar": "Jaipur", "phulera": "Jaipur",
    "dudu": "Jaipur", "amber": "Jaipur", "sanganer": "Jaipur", "bassi": "Jaipur", "jvvnl": "Jaipur",
    "sujangarh": "Churu", "ratangarh": "Churu", "sardarshahar": "Churu", "taranagar": "Churu",
    "nokha": "Bikaner", "lunkaransar": "Bikaner", "kolayat": "Bikaner", "ignp": "Bikaner",
    "mandalgarh": "Bhilwara", "jahazpur": "Bhilwara", "asind": "Bhilwara", "gulabpura": "Bhilwara",
    "chhabra": "Baran", "chhipabarod": "Baran", "anta": "Baran",
    "keshoraipatan": "Bundi", "nainwa": "Bundi", "hindoli": "Bundi",
    "nathdwara": "Rajsamand", "kankroli": "Rajsamand", "amet": "Rajsamand",
    "abu road": "Sirohi", "mount abu": "Sirohi", "pindwara": "Sirohi",
    "kama": "Deeg", "kumher": "Deeg", "nagar": "Deeg",
    "bayana": "Bharatpur", "weir": "Bharatpur", "nadbai": "Bharatpur",
    "neem ka thana": "Sikar", "fatehpur": "Sikar", "srimadhopur": "Sikar",
    "chirawa": "Jhunjhunu", "pilani": "Jhunjhunu", "nawalgarh": "Jhunjhunu", "khetri": "Jhunjhunu",
    "jdvvnl": "Jodhpur", "phalodi": "Phalodi", "balotra": "Balotra", "beawar": "Beawar", "salumbar": "Salumbar",
}

HQ_JAIPUR_KEYWORDS = (
    "director medical education",
    "pccf", "forest department", "vidut utpadan", "vidyut utpadan",
    "vidut prasaran", "vidyut prasaran", "dairy federation", "housing board",
    "sanitation water", "urban drinking water", "agriculture mareketing",
    "agriculture marketing", "tourism development", "council of school education",
    "archaelogy", "archaeology", "national rural health mission",
    "economics and statistics", "college education", "elementary education",
    "medical and health services", "ce public works", "ce water resources", "directorate", "commissionerate", "secretariat",
    "rudsico", "ruidsico", "rsrdc", "ridcor", "risl", "rajcomp", "rhb", "jda",
    "rajasthan state", "chief engineer", "registrar cooperative",
)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def statutory_min_days(amount: float) -> int:
    if amount <= 1_000_000:
        return 7
    if amount <= 5_000_000:
        return 10
    if amount <= 20_000_000:
        return 15
    return 30


def compute_row(tender_id, nit_no, division, district, amount, window_days):
    min_days = statutory_min_days(amount)
    deficit = max(0.0, min_days - window_days)
    violation = deficit > 0
    if violation:
        score = round(min(100.0, max(0.0, 70.0 * (deficit / min_days)
                                     + 3.62 * math.log10(max(1000.0, amount)) + 0.82)), 1)
    else:
        score = 0.0
    return {
        "tender_id": tender_id, "nit_no": nit_no, "division": division, "district": district,
        "tender_amount": amount, "estimated_cost_lac": round(amount / 100000.0, 4),
        "actual_window_days": window_days, "statutory_min_days": min_days,
        "deficit_days": deficit, "is_violation": violation, "risk_score": score,
    }


DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M", "%d-%b-%Y",
]


def parse_date(val):
    if not val:
        return None
    s = str(val).strip()
    if len(s) >= 24 and s.endswith("Z") and "." in s:
        s = s.split(".")[0] + "Z"
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.search(r"(\d{4}-\d{2}-\d{2})|(\d{2}[-/]\d{2}[-/]\d{4})", s)
    if m:
        tok = m.group(0)
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(tok, fmt)
            except ValueError:
                continue
    return None


def parse_amount(val, key_hint=""):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
    else:
        s = str(val).strip()
        if not s or s.lower() in ("na", "n/a", "nil", "-", "null", "none"):
            return None
        t = s.replace("\u20b9", " ").replace("Rs.", " ").replace("Rs", " ").replace(",", "")
        m = re.search(r"\d+(?:\.\d+)?", t)
        if not m:
            return None
        num = float(m.group(0))
        key_hint = f"{t} {key_hint}"
    low = key_hint.lower()
    if "crore" in low or re.search(r"\bcr\b", low):
        num *= 1e7
    elif "lac" in low or "lakh" in low:
        num *= 1e5
    return num if num > 0 else None


def detect_district(*texts):
    blob = " ".join(str(t) for t in texts if t).lower()
    for alias, canon in ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", blob):
            return canon
    for d in DISTRICTS:
        if re.search(rf"\b{re.escape(d.lower())}\b", blob):
            return d
    if any(hq in blob for hq in HQ_JAIPUR_KEYWORDS):
        return "Jaipur"
    return None


class Store:
    def __init__(self):
        DISTRICT_DIR.mkdir(parents=True, exist_ok=True)
        self.seen = set()
        self.real_count = 0
        for f in DISTRICT_DIR.glob("*.csv"):
            with f.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    nit = (r.get("nit_no") or "").strip()
                    amt = str(r.get("tender_amount") or "").strip()
                    win = str(r.get("actual_window_days") or "").strip()
                    is_placeholder = (amt in ("2500000.0", "2500000") and win == "10")
                    if nit and not SYNTHETIC_RE.match(nit) and not is_placeholder:
                        self.real_count += 1
                        self.seen.add((r.get("tender_id") or "").strip())
                        self.seen.add(nit)

    def has(self, tender_id, nit_no):
        return tender_id in self.seen or nit_no in self.seen

    def add(self, row):
        path = DISTRICT_DIR / f"{slug(row['district'])}.csv"
        new = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
        self.seen.update([row["tender_id"], row["nit_no"]])
        self.real_count += 1


def unwrap_item(obj):
    if isinstance(obj, dict) and "attributes" in obj and isinstance(obj["attributes"], dict):
        merged = dict(obj["attributes"])
        if "id" in obj and "id" not in merged:
            merged["id"] = obj["id"]
        return merged
    return obj


def find_record_lists(node):
    found = []
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            found.append([unwrap_item(x) for x in node])
        for x in node:
            found.extend(find_record_lists(x))
    elif isinstance(node, dict):
        for v in node.values():
            found.extend(find_record_lists(v))
    return found


def pick_field(rec, candidates):
    lower_map = {k.lower(): k for k in rec.keys()}
    for c in candidates:
        if c in lower_map:
            val = rec[lower_map[c]]
            if isinstance(val, dict):
                val = unwrap_item(val)
                for sub in ("name", "title", "departmentname", "department_name", "districtname", "label"):
                    if sub in {k.lower() for k in val}:
                        real_k = {k.lower(): k for k in val}[sub]
                        return val[real_k]
            if val not in (None, "", "-"):
                return val
    for k, v in rec.items():
        lk = k.lower()
        if any(c in lk for c in candidates) and v not in (None, "", "-") and not isinstance(v, (dict, list)):
            return v
    return None


def extract_from_records(records, store, stats):
    for rec in records:
        if not isinstance(rec, dict) or "codeTypeId" in rec:
            continue
        ubn = pick_field(rec, ["ubn", "ubn_no", "ubnno", "nit_no", "nitno", "nib_no", "tenderno", "tender_no", "bid_no"])
        title = pick_field(rec, ["title", "tender_title", "tendertitle", "work_name", "name", "description"])
        nit_no = str(ubn or title or "").strip()
        if not nit_no or SYNTHETIC_RE.match(nit_no):
            continue

        dept = str(pick_field(rec, [
            "department", "department_name", "departmentname", "org_name", "organisation",
            "organization", "procuring_entity", "entity_name", "officename", "office_name",
        ]) or "Rajasthan Dept").strip()

        raw_id = pick_field(rec, ["id", "tender_id", "tenderid", "bid_id", "bidid"])
        tender_id = f"SHPP-{raw_id}" if raw_id else "SHPP-" + hashlib.md5(
            f"{nit_no}|{dept}".encode()).hexdigest()[:12]

        if store.has(tender_id, nit_no):
            stats["dup"] += 1
            continue

        amt_key = next((k for k in rec if any(w in k.lower() for w in (
            "bidamount", "bid_amount", "estimated", "tender_value", "tendervalue",
            "bid_value", "bidvalue", "amount", "cost", "value"
        ))), "")
        amount = parse_amount(rec.get(amt_key), amt_key) if amt_key else None

        pub_raw = pick_field(rec, [
            "publish_date", "publishdate", "published_date", "publisheddate",
            "published_at", "publishedat", "start_date", "startdate", "nib_date",
        ])
        close_raw = pick_field(rec, [
            "submission_end_date", "submissionenddate", "bidsubmissionenddate",
            "end_date", "enddate", "closing_date", "closingdate", "bid_end_date", "last_date",
        ])
        pub = parse_date(pub_raw)
        close = parse_date(close_raw)

        if amount is None:
            stats["reasons"]["zero_or_missing_amount"] += 1
            stats["skipped"] += 1
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

        loc_raw = pick_field(rec, [
            "district", "district_name", "districtname", "officeaddress", "office_address",
            "officename", "office_name", "location", "city", "place",
        ])
        all_strings = " ".join(str(v) for v in rec.values() if isinstance(v, (str, int, float)))
        district = detect_district(loc_raw, dept, title, nit_no, all_strings)
        if not district:
            stats["reasons"]["no_district"] += 1
            stats["skipped"] += 1
            continue

        store.add(compute_row(tender_id, nit_no[:120], dept, district, amount, window))
        stats["added"] += 1


def main():
    har_files = sorted(MANUAL_DIR.glob("*.har"))
    if not har_files:
        print("NO HAR FILE YET: Drag shpp.rajasthan.gov.in_2.har from your Downloads folder onto the Codespaces sidebar, then run this again.")
        return

    store = Store()
    start_count = store.real_count
    print(f"Existing verified real rows: {start_count}")
    stats = {"added": 0, "dup": 0, "skipped": 0, "reasons": Counter()}

    for hf in har_files:
        data = json.loads(hf.read_text(encoding="utf-8-sig", errors="replace"))
        entries = data.get("log", {}).get("entries", [])
        print(f"Scanning {hf.name} ({len(entries)} requests)...")
        for ent in entries:
            content = ent.get("response", {}).get("content", {})
            txt = content.get("text", "")
            if not txt:
                continue
            if content.get("encoding") == "base64":
                try:
                    txt = base64.b64decode(txt).decode("utf-8", errors="replace")
                except Exception:
                    continue
            txt_strip = txt.lstrip()
            if not txt_strip.startswith(("{", "[")):
                continue
            try:
                payload = json.loads(txt_strip)
            except Exception:
                continue
            for rec_list in find_record_lists(payload):
                extract_from_records(rec_list, store, stats)

    print(f"\nResult: +{stats['added']} added | {stats['dup']} duplicates | {stats['skipped']} skipped")
    if stats["reasons"]:
        print("Skip breakdown:", dict(stats["reasons"]))
    print(f"Total verified real rows now on disk: {store.real_count} (+{store.real_count - start_count} new)")


if __name__ == "__main__":
    main()
