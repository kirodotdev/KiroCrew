"""The ``store`` fixture the :mod:`kiro_crew.artifacts` data-layer tests share.

``test_artifacts.py`` and its ``_kind_inference``, ``_source_root`` and ``_content_token``
siblings each request ``store``, a fresh store rooted at a temp dir, so it lives in a module
they all import. It is declared ``name="store"`` on ``store_fixture`` so no test parameter
shadows the import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.artifacts import ArtifactStore

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> ArtifactStore:
    """Fresh store rooted at a tmp dir."""
    return ArtifactStore(root=tmp_path / "artifacts")
