"""Guard: the frontend copy of model_registry.json must match the Python source.

model_tokens.json already drifted between the two packages (the frontend copy was
missing claude-opus-4.8); this test prevents the same drift for the registry.
"""

from __future__ import annotations

import json
from pathlib import Path


def test_frontend_registry_matches_python_source():
    root = Path(__file__).resolve().parent.parent
    py = root / "src" / "kiro_crew" / "model_registry.json"
    # The public fork keeps the React SPA under website/ (not a separate package).
    fe = root / "website" / "src" / "model_registry.json"
    if not fe.is_file():
        # Frontend not present in this checkout (backend-only build) — skip.
        import pytest

        pytest.skip(f"frontend registry copy not found at {fe}")
    assert json.loads(py.read_text()) == json.loads(fe.read_text()), (
        "model_registry.json drift: src/kiro_crew and website/src copies differ. "
        "Re-copy the Python source to the frontend."
    )
