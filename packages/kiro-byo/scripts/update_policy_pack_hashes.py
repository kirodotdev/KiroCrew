#!/usr/bin/env python3
"""Regenerate the policy-pack immutability manifest.

Run this whenever a policy pack (dyna.json / tiga.json / ...) is deliberately
changed. It recomputes each pack's sha256 and rewrites policy_pack_hashes.json,
so the release-audit test keeps guarding against *accidental* changes without
anyone hand-copying a hash into a test file.

Usage:
    python scripts/update_policy_pack_hashes.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parents[1] / "src" / "kiro_byo" / "policy_packs"
MANIFEST = PACK_DIR / "policy_pack_hashes.json"


def main() -> None:
    hashes: dict[str, str] = {
        "_comment": (
            "Immutability manifest for policy packs. Each value is the sha256 of "
            "the sibling .json file. A deliberate pack change is a reviewable "
            "one-line update here; an accidental change fails the release-audit "
            "test. Regenerate with: python scripts/update_policy_pack_hashes.py"
        ),
    }
    for pack in sorted(PACK_DIR.glob("*.json")):
        if pack.name == MANIFEST.name:
            continue
        hashes[pack.name] = hashlib.sha256(pack.read_bytes()).hexdigest()
    MANIFEST.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {MANIFEST} with {len(hashes) - 1} pack hash(es).")


if __name__ == "__main__":
    main()
