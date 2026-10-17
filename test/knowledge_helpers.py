"""The ``store`` fixtures shared by the ``test_knowledge*.py`` modules.

``store`` and ``store_factory`` are defined as ``store_fixture`` and
``store_factory_fixture`` under ``@pytest.fixture(name=...)`` so a test module
can import them without each test's parameter shadowing the import.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s._close_all_for_tests()


@pytest.fixture(name="store_factory")
def store_factory_fixture(tmp_path):
    """Return a callable that creates a new store at a given path."""
    stores = []

    def _make(name="test.db"):
        s = KnowledgeStore(str(tmp_path / name))
        stores.append(s)
        return s

    yield _make
    for s in stores:
        s._close_all_for_tests()
