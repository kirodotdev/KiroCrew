"""The minimal valid manifest dict the ``AppManifest`` tests build from.

``test_app_manifest.py`` and ``test_app_manifest_contributes.py`` both start manifests
from ``_valid_manifest``, which carries the required fields plus an author and merges
keyword overrides on top, so each test spells out only the fields it changes.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_manifest(**overrides) -> dict:
    """Return a minimal valid manifest dict with optional overrides."""
    base = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
    }
    base.update(overrides)
    return base
