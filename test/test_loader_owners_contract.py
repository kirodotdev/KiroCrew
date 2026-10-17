"""Contracts the ``config.loader`` facade keeps over its ``loader_*`` owners.

``test_facade_mirror_guards.py`` holds every mirrored facade to the generic contract.
This file pins what is particular to this one: its owner list, the provider factory that
reaches the ACP provider through the agent SDK, the function-local imports that break the
two call cycles between owners, and the one home of each ``global`` the snapshot owner
rebinds.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.agent_sdk.drivers.acp import build_acp_provider
from kiro_crew.config import loader, loader_config, loader_pipeline, loader_snapshots

_OWNERS = tuple(
    f"kiro_crew.config.loader_{name}"
    for name in (
        "credentials",
        "locations",
        "document",
        "builders",
        "snapshots",
        "agents",
        "writeback",
        "config",
        "pipeline",
    )
)

#: Every module-level name ``loader_snapshots`` rebinds with ``global``.
_REBOUND_STATE = (
    "_CONFIG_AGENT_ALIAS_SNAPSHOT",
    "_CONFIG_AUTOCOMPACT_ISSUED",
    "_CONFIG_AUTOCOMPACT_PCT",
    "_CONFIG_AUTOCOMPACT_TICKET",
    "_CONFIG_TIMEZONE",
    "_CONFIG_TIMEZONE_TICKET",
    "_MATERIALIZED_AGENTS",
    "_MATERIALIZED_AGENTS_GENERATION",
    "_MATERIALIZED_AGENTS_READY",
    "_MATERIALIZED_COMPLETE",
    "_MATERIALIZED_REFRESH_APPLIED",
    "_MATERIALIZED_REFRESH_ISSUED",
)
_STATE_NAMES = (*_REBOUND_STATE, "_MATERIALIZED_SEEN", "_MATERIALIZED_STEMS")


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(loader)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


@pytest.fixture
def restored_state(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Register every snapshot global for restoration; the two dicts the owner updates
    in place are swapped for copies, so the process-wide originals come back untouched."""
    for name in _STATE_NAMES:
        monkeypatch.setattr(loader, name, getattr(loader, name))
    for name in ("_MATERIALIZED_SEEN", "_MATERIALIZED_STEMS"):
        monkeypatch.setattr(loader, name, dict(getattr(loader, name)))
    return monkeypatch


def test_owners_are_listed_lowest_layer_first_and_nothing_is_excluded() -> None:
    index = _index()
    assert (index.owners, index.excluded, dict(index.forwards)) == (_OWNERS, (), {})
    assert index.forward_reads == "sys_modules_first"
    assert index.divergent() == ()


def test_the_package_surface_resolves_through_the_facade() -> None:
    import kiro_crew.config as config_pkg

    assert [n for n in config_pkg.__all__ if getattr(config_pkg, n) is not getattr(loader, n)] == []


class TestTheProviderFactory:
    def test_it_lives_with_its_class_in_one_owner(self) -> None:
        assert loader.KiroCrewConfig.__module__ == loader_config.__name__
        assert "create_provider_factory" in vars(loader_config.KiroCrewConfig)
        assert loader.KiroCrewConfig.create_provider_factory.__module__ == loader_config.__name__

    def test_no_loader_module_imports_the_acp_layer(self) -> None:
        family = sorted(Path(loader.__file__).parent.glob("loader*.py"))
        assert len(family) == 1 + len(_OWNERS)
        importers = set()
        for path in family:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    continue
                for module in modules:
                    for root in ("kiro_crew.acp", "kiro_crew.providers"):
                        if module == root or module.startswith(root + "."):
                            importers.add(path.name)
        assert importers == set()

    def test_a_patched_provider_class_reaches_the_factory(self) -> None:
        cfg = loader.KiroCrewConfig()
        with patch("kiro_crew.providers.acp.AcpProvider") as provider:
            # The SDK factory reads the class at call time, so a patch of the
            # providers-layer class reaches it (and config through it).
            assert build_acp_provider() is provider.return_value
            factory = cfg.create_provider_factory()
            assert factory(session_key="cron:contract", model_override="fixture-model") is (
                provider.return_value
            )
        assert provider.call_count == 2


class _FakeConfigClass:
    """Stands in for ``KiroCrewConfig`` where a function-local import reads it."""

    __dataclass_fields__ = {"default_workspace": SimpleNamespace(default="fake-default")}
    loads = 0

    @classmethod
    def load(cls) -> SimpleNamespace:
        cls.loads += 1
        return SimpleNamespace(degraded_sections=frozenset({"agents"}))


class TestTheCycleBreakingImports:
    def test_the_healer_loads_the_class_a_facade_patch_installed(
        self, restored_state: pytest.MonkeyPatch
    ) -> None:
        _FakeConfigClass.loads = 0
        restored_state.setattr(loader, "KiroCrewConfig", _FakeConfigClass)
        for name, value in {
            "_MATERIALIZED_AGENTS": frozenset({"kept"}),
            "_MATERIALIZED_AGENTS_READY": True,
            "_MATERIALIZED_COMPLETE": True,
            "_MATERIALIZED_SEEN": {"kept": None, "gone": None},
            "_MATERIALIZED_STEMS": {},
        }.items():
            restored_state.setattr(loader, name, value)
        assert loader.reset_dangling_default_agent() is False
        assert _FakeConfigClass.loads == 1

    def test_the_locked_workspace_read_uses_the_patched_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setattr(loader, "KiroCrewConfig", _FakeConfigClass)
        monkeypatch.setattr(loader, "_alias_workspace_dir", lambda _e, _n, d: seen.append(d))
        loader._locked_workspace_dir({"workspaces": {}}, {"workspace": "w"})
        assert seen == ["fake-default"]

    def test_the_resolve_step_runs_the_patched_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Reached(Exception):
            pass

        def refuse() -> None:
            raise Reached

        monkeypatch.setattr(loader, "read_config_document", refuse)
        with pytest.raises(Reached):
            loader.KiroCrewConfig._load_resolved()

    @pytest.mark.parametrize(
        ("target", "higher", "imported"),
        [
            (loader_snapshots.reset_dangling_default_agent, loader_config, {"KiroCrewConfig"}),
            (loader_snapshots._locked_workspace_dir, loader_config, {"KiroCrewConfig"}),
            (
                loader_config.KiroCrewConfig._load_resolved,
                loader_pipeline,
                {"build_config", "persist_write_back", "read_config_document"},
            ),
        ],
    )
    def test_each_cycle_break_is_one_local_import_of_the_higher_owner(
        self, target, higher, imported: set[str]
    ) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
        local = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("kiro_crew.config")
        ]
        assert [(n.module, {a.name for a in n.names}) for n in local] == [
            (higher.__name__, imported)
        ]


class TestTheSnapshotStateHasOneHome:
    @pytest.mark.parametrize("name", _STATE_NAMES)
    def test_the_owner_binds_it_and_the_facade_serves_it(self, name: str) -> None:
        assert name not in vars(loader)
        assert _index().holders[name] == ("kiro_crew.config.loader_snapshots",)
        assert getattr(loader, name) is vars(loader_snapshots)[name]

    @pytest.mark.parametrize("name", _REBOUND_STATE)
    def test_a_facade_write_is_what_the_owner_code_reads(
        self, name: str, restored_state: pytest.MonkeyPatch
    ) -> None:
        sentinel = object()
        restored_state.setattr(loader, name, sentinel)
        assert vars(loader_snapshots)[name] is sentinel
        assert name not in vars(loader)
        assert loader_snapshots.publish_materialized_agents.__globals__ is vars(loader_snapshots)

    @pytest.mark.parametrize("name", _REBOUND_STATE)
    def test_an_owner_rebind_is_what_the_facade_reads(
        self, name: str, restored_state: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = loader.KiroCrewConfig()
        cfg.timezone = "Europe/Paris"
        cfg.session.autocompact_pct = 0.42
        for preset, value in {
            "_CONFIG_AGENT_ALIAS_SNAPSHOT": (frozenset({"preset"}), "preset", False),
            "_CONFIG_AUTOCOMPACT_ISSUED": 1000,
            "_CONFIG_AUTOCOMPACT_PCT": 0.99,
            "_CONFIG_AUTOCOMPACT_TICKET": 0,
            "_CONFIG_TIMEZONE": "preset/zone",
            "_CONFIG_TIMEZONE_TICKET": 0,
            "_MATERIALIZED_AGENTS": frozenset({"preset"}),
            "_MATERIALIZED_AGENTS_GENERATION": 1000,
            "_MATERIALIZED_AGENTS_READY": False,
            "_MATERIALIZED_COMPLETE": False,
            "_MATERIALIZED_REFRESH_APPLIED": 0,
            "_MATERIALIZED_REFRESH_ISSUED": 1000,
        }.items():
            restored_state.setattr(loader, preset, value)
        restored_state.setattr(loader, "kiro_agents_dir", lambda: tmp_path)
        publish = (loader.publish_materialized_agents, [["fresh-agent"]])
        # name -> (a call that rebinds it in the owner's own code, its args, the value after)
        rebinds = {
            "_CONFIG_AGENT_ALIAS_SNAPSHOT": (
                loader.publish_agent_alias_snapshot,
                [cfg],
                (frozenset(), loader.default_template(cfg), True),
            ),
            "_CONFIG_AUTOCOMPACT_ISSUED": (loader.next_config_load_ticket, [], 1001),
            "_CONFIG_AUTOCOMPACT_PCT": (loader.publish_autocompact_pct, [cfg], 0.42),
            "_CONFIG_AUTOCOMPACT_TICKET": (loader.publish_autocompact_pct, [cfg], 1001),
            "_CONFIG_TIMEZONE": (loader.publish_config_timezone, [cfg], "Europe/Paris"),
            "_CONFIG_TIMEZONE_TICKET": (loader.publish_config_timezone, [cfg], 1001),
            "_MATERIALIZED_AGENTS": (*publish, frozenset({"preset", "fresh-agent"})),
            "_MATERIALIZED_AGENTS_GENERATION": (*publish, 1001),
            "_MATERIALIZED_AGENTS_READY": (*publish, True),
            "_MATERIALIZED_COMPLETE": (loader.refresh_materialized_agents, [], True),
            "_MATERIALIZED_REFRESH_APPLIED": (loader.refresh_materialized_agents, [], 1001),
            "_MATERIALIZED_REFRESH_ISSUED": (loader.refresh_materialized_agents, [], 1001),
        }
        rebind, args, expected = rebinds[name]
        rebind(*args)
        assert getattr(loader, name) == expected
        assert getattr(loader, name) is vars(loader_snapshots)[name]
        assert name not in vars(loader)
