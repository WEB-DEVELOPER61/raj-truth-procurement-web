import hashlib
from typing import List, Tuple

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

def hash_leaf(record: str) -> bytes:
    return sha256(b"\x00" + record.encode("utf-8"))

def hash_internal(left: bytes, right: bytes) -> bytes:
    return sha256(b"\x01" + left + right)

def compute_merkle_root(leaf_hashes: List[bytes]) -> str:
    if not leaf_hashes:
        return hashlib.sha256(b"EMPTY_TREE").hexdigest()
    current = list(leaf_hashes)
    while len(current) > 1:
        nxt = []
        for i in range(0, len(current), 2):
            l = current[i]
            r = current[i + 1] if i + 1 < len(current) else l
            nxt.append(hash_internal(l, r))
        current = nxt
    return current[0].hex()

def build_hierarchical_state_root(district_roots: dict) -> Tuple[str, dict]:
    sorted_dists = sorted(district_roots.keys())
    intermediate = [hash_leaf(f"{d}:{district_roots[d]}") for d in sorted_dists]
    return compute_merkle_root(intermediate), district_roots
