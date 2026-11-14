import hashlib
import json
import math
from datetime import datetime, timezone
import duckdb


def sha256_leaf(raw_text: str) -> bytes:
    """RFC 6962 Floor 0 Leaf Digest: prepends 0x00 byte before hashing."""
    return hashlib.sha256(b"\x00" + raw_text.encode("utf-8")).digest()


def sha256_node(left: bytes, right: bytes) -> bytes:
    """RFC 6962 Internal Node Digest: prepends 0x01 byte before combining children."""
    return hashlib.sha256(b"\x01" + left + right).digest()


def build_rfc6962_merkle_root(records: list[str]) -> tuple[str, int]:
    """Builds the bottom-up binary Merkle tree and returns (hex_root, tree_floors)."""
    if not records:
        empty_hash = hashlib.sha256(b"").hexdigest()
        return empty_hash, 0

    current_level = [sha256_leaf(rec) for rec in records]
    floors = 0

    while len(current_level) > 1:
        next_level = []
        for i in range(0, len(current_level), 2):
            if i + 1 < len(current_level):
                next_level.append(sha256_node(current_level[i], current_level[i + 1]))
            else:
                # RFC 6962 rule: carry an unpaired odd node up without duplicating it
                next_level.append(current_level[i])
        current_level = next_level
        floors += 1

    return current_level[0].hex(), floors


def main():
    # 1. Connect to DuckDB and pull canonical tender strings in deterministic order
    con = duckdb.connect("truth_tenders.duckdb", read_only=True)
    rows = con.execute("""
        SELECT
            tender_id || '|' ||
            COALESCE(CAST(tender_value AS VARCHAR), '0') || '|' ||
            COALESCE(CAST(publish_date AS VARCHAR), '') || '|' ||
            COALESCE(CAST(bid_close_date AS VARCHAR), '') AS canonical_record
        FROM tenders
        ORDER BY tender_id ASC
    """).fetchall()
    con.close()

    canonical_records = [row[0] for row in rows]

    # 2. Compute the tamper-evident RFC 6962 Master Merkle Root
    master_root, tree_floors = build_rfc6962_merkle_root(canonical_records)

    # 3. Write public cryptographic receipt for GitHub Actions to commit
    receipt = {
        "standard": "RFC 6962 Certificate Transparency Merkle Tree",
        "hash_algorithm": "SHA-256",
        "domain_separation": {"leaf_prefix": "0x00", "node_prefix": "0x01"},
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_records": len(canonical_records),
        "tree_floors": tree_floors,
        "master_merkle_root": master_root,
    }

    with open("merkle_root.json", "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)

    print(f"Sealed {len(canonical_records)} records across {tree_floors} floors.")
    print(f"Master Merkle Root: {master_root}")


if __name__ == "__main__":
    main()
