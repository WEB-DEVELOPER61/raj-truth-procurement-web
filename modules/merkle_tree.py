import hashlib
from typing import List, Tuple, Dict, Any

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

def hash_leaf(record: str) -> bytes:
    """Prepend 0x00 for RFC 6962 leaf domain separation."""
    return sha256(b"\x00" + record.encode("utf-8"))

def hash_internal(left: bytes, right: bytes) -> bytes:
    """Prepend 0x01 for RFC 6962 internal node domain separation."""
    return sha256(b"\x01" + left + right)

def compute_merkle_root_and_tree(leaf_hashes: List[bytes]):
    """Builds full tree levels to allow logarithmic audit path generation."""
    if not leaf_hashes:
        return hashlib.sha256(b"EMPTY_TREE").hexdigest(), []
    
    levels = [leaf_hashes]
    current = list(leaf_hashes)

    while len(current) > 1:
        next_level = []
        for i in range(0, len(current), 2):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else left
            next_level.append(hash_internal(left, right))
        levels.append(next_level)
        current = next_level

    return current[0].hex(), levels

def compute_merkle_root(leaf_hashes: List[bytes]) -> str:
    root, _ = compute_merkle_root_and_tree(leaf_hashes)
    return root

def generate_merkle_proof(leaf_index: int, levels: List[List[bytes]]) -> List[Dict[str, str]]:
    """Generates an O(log N) cryptographic sibling audit path."""
    proof = []
    idx = leaf_index

    for level in levels[:-1]:
        is_right = (idx % 2 == 1)
        sibling_idx = idx - 1 if is_right else idx + 1

        if sibling_idx < len(level):
            sibling_hash = level[sibling_idx].hex()
        else:
            sibling_hash = level[idx].hex()

        proof.append({
            "direction": "left" if is_right else "right",
            "sibling_hash": sibling_hash
        })
        idx = idx // 2

    return proof

def verify_merkle_proof(target_leaf_hash: str, proof: List[Dict[str, str]], expected_root: str) -> bool:
    """Verifies proof path matches the expected Merkle Root."""
    current = bytes.fromhex(target_leaf_hash)
    for p in proof:
        sibling = bytes.fromhex(p["sibling_hash"])
        if p["direction"] == "left":
            current = hash_internal(sibling, current)
        else:
            current = hash_internal(current, sibling)
    return current.hex() == expected_root

def build_hierarchical_state_root(district_roots: dict) -> Tuple[str, dict]:
    sorted_dists = sorted(district_roots.keys())
    intermediate = [hash_leaf(f"{d}:{district_roots[d]}") for d in sorted_dists]
    root = compute_merkle_root(intermediate)
    return root, district_roots
