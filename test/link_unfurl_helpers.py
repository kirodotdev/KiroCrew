"""The resolver stub and the autouse isolation the link-preview test modules share.

``test_link_unfurl.py`` and ``test_link_unfurl_vet_and_extraction.py`` import
``_isolate`` by name, so every test gets a fresh link-meta cache, the feature on, and
DNS stubbed to one public address (``_public``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

from kiro_crew import link_unfurl as lu
from kiro_crew.dashboard.handlers import link_meta as lm


def _public(_host: str, _port: int) -> List[str]:
    """Resolver stub: everything resolves to one public address."""
    return ["93.184.216.34"]


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the link-preview modules' autouse isolation (fresh cache, feature on, public DNS stub), moved verbatim; tracked for a de-flake pass
def _isolate(monkeypatch):
    """Fresh cache per test, feature ON, DNS stubbed to one public address."""
    lm.reset_link_meta_cache()
    monkeypatch.setattr(lu, "_default_resolve", _public)
    _set_enabled(monkeypatch, True)
    yield
    lm.reset_link_meta_cache()


def _set_enabled(monkeypatch, enabled: bool) -> None:
    """Patch the config loader the handler imports at call time."""
    from kiro_crew.config import loader as loader_mod

    cfg = SimpleNamespace(dashboard=SimpleNamespace(link_previews=enabled))
    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
