#!/usr/bin/env python3
"""Generate config-baseline.json from the Python dataclass schema registry.

Usage:
    python scripts/generate_config_baseline.py

Writes ``config-baseline.json`` to the repository root with the full flat
entry list derived from the dataclass hierarchy.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys

# Ensure the package is importable when running from the repo root.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src_dir = os.path.join(_repo_root, "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict  # noqa: E402


class _SafeEncoder(json.JSONEncoder):
    """Encode dataclass instances as dicts so defaultValue is serializable."""

    def default(self, o: object) -> object:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


def main() -> None:
    baseline = {
        "generatedBy": "scripts/generate_config_baseline.py",
        "entries": [config_entry_to_dict(e) for e in SCHEMA_REGISTRY],
    }

    out_path = os.environ.get(
        "KIROCREW_BASELINE_OUTPUT",
        os.path.join(_repo_root, "config-baseline.json"),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False, cls=_SafeEncoder)
        f.write("\n")

    print(f"Wrote {len(SCHEMA_REGISTRY)} entries to {out_path}")


if __name__ == "__main__":
    main()
