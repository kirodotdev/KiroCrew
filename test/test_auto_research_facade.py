"""The ``handlers`` compatibility facade over the Research Lab campaign engine.

``handlers`` keeps the HTTP adapters and forwards every other historic name to
the ``campaign`` component that binds it. These tests pin the three properties
that make that forwarding safe:

* identity -- a forwarded name is the owner's object, and is never also bound
  in ``handlers`` (a second binding would shadow the owner for some callers);
* patch reach -- a write or delete through ``handlers`` lands on the owner, and
  both ``monkeypatch`` and ``unittest.mock.patch`` restore it exactly;
* layering -- the components form one acyclic stack that never imports the
  facade, reach each other through the module (never a from-import of a
  function) and log under the historic logger name.

The behavioural contract the facade preserves is pinned in
``test_auto_research_campaign_contract.py``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.apps.builtins.auto_research import handlers as h
from kiro_crew.apps.builtins.auto_research.campaign import LOGGER_NAME, storage, watchdog

_CAMPAIGN = "kiro_crew.apps.builtins.auto_research.campaign"
_CAMPAIGN_DIR = Path(inspect.getsourcefile(storage) or "").resolve().parent

# Bottom to top: a component may import only components listed before it.
_LAYERS = [
    "untrusted",
    "storage",
    "lifecycle",
    "publication",
    "exploration",
    "agent_mode",
    "workflow_mode",
    "watchdog",
    "grill",
]


def _component(name: str):
    return importlib.import_module(f"{_CAMPAIGN}.{name}")


def _component_trees() -> dict[str, ast.Module]:
    return {
        p.stem: ast.parse(p.read_text(encoding="utf-8"))
        for p in sorted(_CAMPAIGN_DIR.glob("*.py"))
        if p.stem != "__init__"
    }


class TestIdentity:
    def test_the_facade_module_class_is_installed(self):
        assert type(h).__name__ == "_ReExportModule"

    def test_every_forwarded_name_is_the_owners_object(self):
        wrong = []
        for name, owner_name in h._EXPORTS.items():
            owner = vars(importlib.import_module(owner_name))
            if name not in owner or getattr(h, name) is not owner[name]:
                wrong.append(f"{name} -> {owner_name}")
        assert wrong == []

    def test_each_forwarded_name_has_one_binding(self):
        """A second binding in another component would be missed by a patch
        through the facade, which reaches only the owner."""
        extra = []
        for stem in _LAYERS:
            bound = vars(_component(stem))
            for name, owner_name in h._EXPORTS.items():
                if name in bound and owner_name != f"{_CAMPAIGN}.{stem}":
                    extra.append(f"{name} also bound in {stem}")
        # The status value type is the one sanctioned by-name import.
        assert sorted(e for e in extra if not e.startswith("CampaignStatus ")) == []

    def test_no_forwarded_name_is_also_bound_in_the_facade(self):
        assert sorted(set(h._EXPORTS) & set(vars(h))) == []

    def test_every_owner_is_a_listed_component(self):
        assert set(h._EXPORTS.values()) == {f"{_CAMPAIGN}.{stem}" for stem in _LAYERS}
        assert sorted(p.stem for p in _CAMPAIGN_DIR.glob("*.py")) == sorted(["__init__", *_LAYERS])

    def test_forwarded_names_are_listed_by_dir(self):
        assert set(h._EXPORTS) <= set(dir(h))

    def test_an_unknown_name_is_still_an_attribute_error(self):
        with pytest.raises(AttributeError, match="has no attribute 'no_such_name'"):
            getattr(h, "no_such_name")
        assert getattr(h, "no_such_name", None) is None


class TestPatchReach:
    def test_monkeypatch_writes_land_on_the_owner_and_restore(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        original = watchdog.POLL_INTERVAL
        with monkeypatch.context() as m:
            m.setattr(h, "POLL_INTERVAL", 0.25)
            assert watchdog.POLL_INTERVAL == 0.25
            assert h.POLL_INTERVAL == 0.25
            assert "POLL_INTERVAL" not in vars(h)
        assert watchdog.POLL_INTERVAL == original

    def test_mock_patch_writes_land_on_the_owner_and_restore(self, tmp_path: Path):
        original = storage.DB_PATH
        with patch.object(h, "DB_PATH", tmp_path / "x.db"):
            assert storage.DB_PATH == tmp_path / "x.db"
            assert storage.db_path() == tmp_path / "x.db"
        assert storage.DB_PATH == original
        with patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            assert storage.research_dir() == tmp_path
        assert "RESEARCH_DIR" not in vars(h)

    @pytest.mark.asyncio
    async def test_a_patched_collaborator_reaches_callers_in_other_components(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The slot-key helper is used by ``agent_mode`` and ``watchdog``; one
        patch through the facade must reach both callers."""
        looked_up: list[str] = []
        svc = SimpleNamespace(
            get_by_slot=lambda key: looked_up.append(key), remove=AsyncMock(), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "research_slot_key", lambda cid: f"patched-{cid}")
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: False)
        await h._stop_loop("0123abcd", remove=True)
        await h._settle_campaign_from_watchdog("0123abcd", [], {}, {}, observed_started_at=1.0)
        assert looked_up == ["patched-0123abcd", "patched-0123abcd"]

    def test_a_delete_is_forwarded_too(self, monkeypatch: pytest.MonkeyPatch):
        original = watchdog.POLL_INTERVAL
        with monkeypatch.context() as m:
            m.delattr(h, "POLL_INTERVAL")
            assert not hasattr(watchdog, "POLL_INTERVAL")
            assert not hasattr(h, "POLL_INTERVAL")
        assert watchdog.POLL_INTERVAL == original

    def test_facade_owned_names_bind_normally(self, monkeypatch: pytest.MonkeyPatch):
        marker = object()
        monkeypatch.setattr(h, "LLMPool", marker)
        assert vars(h)["LLMPool"] is marker


class TestLayering:
    def test_components_never_import_the_facade(self):
        offenders = []
        for stem, tree in _component_trees().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = {a.name for a in node.names}
                    if node.module.endswith("auto_research.handlers") or (
                        node.module.endswith("auto_research") and "handlers" in names
                    ):
                        offenders.append(stem)
                elif isinstance(node, ast.Import):
                    if any(a.name.endswith("auto_research.handlers") for a in node.names):
                        offenders.append(stem)
        assert offenders == []

    def test_components_form_one_acyclic_stack(self):
        edges = {}
        for stem, tree in _component_trees().items():
            deps = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == _CAMPAIGN:
                    deps |= {a.name for a in node.names if a.name in _LAYERS}
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    f"{_CAMPAIGN}."
                ):
                    deps.add((node.module or "").rsplit(".", 1)[-1])
            edges[stem] = deps
        upward = sorted(
            f"{stem} -> {dep}"
            for stem, deps in edges.items()
            for dep in deps
            if _LAYERS.index(dep) >= _LAYERS.index(stem)
        )
        assert upward == []

    def test_cross_component_references_go_through_the_module(self):
        """Only the ``CampaignStatus`` value type is imported by name; every
        function and mutable module state is reached as ``component.name``."""
        by_name = []
        for stem, tree in _component_trees().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    f"{_CAMPAIGN}."
                ):
                    names = [a.name for a in node.names]
                    if names != ["CampaignStatus"] or node.module != f"{_CAMPAIGN}.storage":
                        by_name.append(f"{stem}: from {node.module} import {names}")
        assert by_name == []

    @pytest.mark.parametrize("stem", _LAYERS)
    def test_components_log_under_the_historic_logger(self, stem: str):
        logger = vars(_component(stem)).get("logger")
        if logger is not None:
            assert isinstance(logger, logging.Logger)
            assert logger.name == LOGGER_NAME == h.logger.name
