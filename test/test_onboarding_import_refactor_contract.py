"""Compatibility contract of the :mod:`kiro_crew.onboarding_import` facade.

The engine is composed of cohesive owners (``onboarding_scan``,
``onboarding_plan``, ``onboarding_sources``, ``onboarding_apply``) behind the
``onboarding_import`` facade. These tests pin what the facade promises to the
code that imports it -- the dashboard handler, ``mcp_cleanup``, the managed-MCP
registration tests and the persistence ratchets -- independently of where each
rule is implemented:

* the importable names and the public signatures;
* that every re-exported name IS the canonical owner's object, so a caller of
  the facade and a caller of the owner can never observe two implementations;
* that every engine warning is still emitted on the ``kiro_crew.onboarding_import``
  logger, which operators and tests filter on;
* that the deferred imports stay deferred, so importing the facade does not pull
  the dashboard MCP handler, MCP discovery or the cron service into a process;
* that every owner module imports cleanly on its own, in any order;
* that each mirrored seam (``onboarding_import._EXPORTS``) lives in ONE module:
  the facade reads it there and forwards a write there, every other reader
  resolves it through that module at call time, and every supported way of
  undoing a patch leaves the owner's original in place;
* that the facade's own code reads no mirrored name as a bare global, that
  each mirrored name stays visible to type checkers through a ``TYPE_CHECKING``
  import from its owner, and that an owner already in ``sys.modules`` is read,
  written and restored without a call to ``importlib.import_module``;
* that no test file passes ``mock.patch`` a ``create`` other than the literal
  ``False`` for a mirrored name on the facade, the one spelling whose undo loses
  the owner's binding. An AST guard enforces it, with an explicit
  ``(path, test)`` allowlist that is empty.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import importlib
import importlib.util
import inspect
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import source_corpus

from kiro_crew import onboarding_import, onboarding_scan, onboarding_sources, platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.interfaces import ImportSource

_FACADE_LOGGER = "kiro_crew.onboarding_import"

#: Names reached through the facade by code this engine does not own -- imported,
#: or cited by their ``onboarding_import.<name>`` path in specs, docstrings and
#: comments that must keep resolving. The list is a contract, not an inventory.
_CONSUMER_NAMES = (
    # Public API: the dashboard handler (``_backend()``) and the specs.
    "detect_sources",
    "preview_import",
    "apply_import",
    "CATEGORY_IDS",
    "CONFLICT_STRATEGIES",
    "STRATEGY_SKIP",
    "STRATEGY_RENAME",
    "STRATEGY_OVERWRITE",
    "STRATEGY_CATEGORIES",
    # mcp_cleanup's deferred imports.
    "predecessor_mcp_names",
    "stale_mcp_binaries",
    # Imported by the managed-MCP registration, lesson-outcome and frontmatter tests.
    "_managed_mcp_names",
    "_Item",
    "_write_instruction",
    "_frontmatter",
    "_column0_activation_declared",
    # Located by ``onboarding_import.py::<name>`` in the persistence and cron ratchets.
    "_write_memory",
    "_write_schedule",
    # Cited by facade path: platform-context.md / interfaces.py (``_sources()``),
    # the handler's ``_SOURCE_ID_SHAPE_RE`` note, test_yaml_safe_loading's docstring
    # (``_load_no_alias_yaml``), the managed-name and writer-outcome vocabulary.
    "_CORE_MANAGED_MCP_NAMES",
    "_WriteOutcome",
    "_load_no_alias_yaml",
    "_sources",
    "_Source",
    "_SOURCE_ID_RE",
    "_scan_source",
    "_preview",
)

#: Consumer names the facade DEFINES rather than re-exports.
_FACADE_DEFINED = (
    "detect_sources",
    "preview_import",
    "apply_import",
    "_write_instruction",
    "_write_memory",
    "_write_schedule",
    "_preview",
)

#: The canonical owner of every re-exported consumer name.
_OWNERS = {
    "CATEGORY_IDS": "kiro_crew.onboarding_scan",
    "_Item": "kiro_crew.onboarding_scan",
    "_frontmatter": "kiro_crew.onboarding_scan",
    "_column0_activation_declared": "kiro_crew.onboarding_scan",
    "_load_no_alias_yaml": "kiro_crew.onboarding_scan",
    "CONFLICT_STRATEGIES": "kiro_crew.onboarding_apply",
    "STRATEGY_SKIP": "kiro_crew.onboarding_apply",
    "STRATEGY_RENAME": "kiro_crew.onboarding_apply",
    "STRATEGY_OVERWRITE": "kiro_crew.onboarding_apply",
    "STRATEGY_CATEGORIES": "kiro_crew.onboarding_apply",
    "_WriteOutcome": "kiro_crew.onboarding_apply",
    "predecessor_mcp_names": "kiro_crew.onboarding_sources",
    "stale_mcp_binaries": "kiro_crew.onboarding_sources",
    "_managed_mcp_names": "kiro_crew.onboarding_sources",
    "_sources": "kiro_crew.onboarding_sources",
    "_Source": "kiro_crew.onboarding_sources",
    "_SOURCE_ID_RE": "kiro_crew.onboarding_sources",
    "_CORE_MANAGED_MCP_NAMES": "kiro_crew.onboarding_sources",
    "_scan_source": "kiro_crew.onboarding_sources",
}

#: Every module of the engine, facade included.
_ENGINE_MODULES = (
    "kiro_crew.onboarding_import",
    "kiro_crew.onboarding_scan",
    "kiro_crew.onboarding_plan",
    "kiro_crew.onboarding_apply",
    "kiro_crew.onboarding_sources",
    "kiro_crew.onboarding_sources.claude_code",
    "kiro_crew.onboarding_sources.codex",
    "kiro_crew.onboarding_sources.gemini",
    "kiro_crew.onboarding_sources.hermes",
    "kiro_crew.onboarding_sources.lineage",
    "kiro_crew.onboarding_sources.openclaw",
)

#: Modules the facade must NOT load at import time. Each is imported lazily at
#: the one call site that needs it (the MCP sidecar lock, the alias census, the
#: cron service), because loading them eagerly either inverts an import order
#: (the dashboard handler imports this engine during gateway startup) or adds
#: boot weight every importer pays.
_DEFERRED_MODULES = (
    "kiro_crew.dashboard.handlers.mcp",
    "kiro_crew.mcp_discovery",
    "kiro_crew.cron",
)

#: ``(seam, owner module)`` for every name the facade mirrors, read off the facade's
#: own table so the cases below follow it. ``test_every_mirrored_seam_has_a_scenario``
#: is what notices a seam leaving the table, because the scenarios name each one.
_MIRRORED = sorted(onboarding_import._EXPORTS.items())
_MIRRORED_IDS = [name for name, _owner_name in _MIRRORED]

#: The dashboard MCP handler's host paths, bound from ``Path.home()`` at import.
_MCP_HOST_PATHS = ("_GLOBAL_MCP_JSON", "_MCP_LOCK_PATH")


@pytest.fixture(autouse=True)
def _isolate_mcp_host_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``apply_import``'s MCP sidecar lock off the real ``~/.kiro/settings``.

    ``_write_mcp`` takes the dashboard handler's lock, whose paths the handler
    binds from ``Path.home()`` when it is imported. The host floor rebinds them
    only when that module is already loaded, so without this the first MCP write
    in a worker reaches the operator's real files. A test that patches these names
    itself still wins, because its own patch runs after this one.
    """
    mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
    global_mcp = tmp_path / "host-kiro-settings" / "mcp.json"
    monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_mcp)
    monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))


def _lineage_source(**overrides) -> ImportSource:
    fields: dict = {
        "id": "predecessor",
        "display_name": "Predecessor",
        "env_vars": ("PREDECESSOR_HOME",),
        "home_dir": ".predecessor",
    }
    fields.update(overrides)
    return ImportSource(**fields)


@pytest.fixture
def install_sources():
    """Compose a context whose edition contributes the given sources."""

    def _install(*sources: ImportSource) -> None:
        class _Provider:
            def import_sources(self) -> list[ImportSource]:
                return list(sources)

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Provider()))

    yield _install
    reset_context()


class TestFacadeSurface:
    @pytest.mark.parametrize("name", _CONSUMER_NAMES)
    def test_every_consumer_name_is_importable(self, name: str) -> None:
        assert hasattr(onboarding_import, name), f"onboarding_import.{name} is gone"

    def test_a_star_import_exposes_every_public_engine_name(self, tmp_path: Path) -> None:
        """``__all__`` is derived, so ``import *`` carries a mirrored public name too.

        A star import consults ``__all__`` and never the module ``__getattr__`` that
        answers a mirrored name, so ``url2pathname`` reaches a star-importer only
        because the derived list names it. The star import runs in a real module
        loaded from ``tmp_path``, the one place a star import is legal.
        """
        probe_path = tmp_path / "onboarding_star_probe.py"
        probe_path.write_text("from kiro_crew.onboarding_import import *\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("onboarding_star_probe", probe_path)
        assert spec and spec.loader
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        namespace = vars(probe)
        public = {
            "CATEGORY_IDS",
            "CONFLICT_STRATEGIES",
            "STRATEGY_CATEGORIES",
            "STRATEGY_OVERWRITE",
            "STRATEGY_RENAME",
            "STRATEGY_SKIP",
            "apply_import",
            "detect_sources",
            "logger",
            "predecessor_mcp_names",
            "preview_import",
            "stale_mcp_binaries",
            "url2pathname",
        }
        assert public <= set(onboarding_import.__all__), sorted(
            public - set(onboarding_import.__all__)
        )
        assert public <= set(namespace), sorted(public - set(namespace))
        gemini = importlib.import_module("kiro_crew.onboarding_sources.gemini")
        assert namespace["url2pathname"] is gemini.url2pathname
        assert callable(vars(onboarding_import)["__getattr__"])

    def test_all_is_derived_from_the_bindings_and_the_table(self) -> None:
        """A projection of what the facade binds plus the table, never a third list."""
        derived = sorted(
            name
            for name in set(vars(onboarding_import)) | set(onboarding_import._EXPORTS)
            if not name.startswith("_")
        )
        assert onboarding_import.__all__ == derived

    def test_public_signatures_are_unchanged(self) -> None:
        assert str(inspect.signature(onboarding_import.detect_sources)) == (
            "(home: 'Path | None' = None, env: 'Mapping[str, str] | None' = None)"
            " -> 'dict[str, Any]'"
        )
        assert str(inspect.signature(onboarding_import.preview_import)) == (
            "(source_ids: 'list[str] | None' = None, home: 'Path | None' = None, "
            "env: 'Mapping[str, str] | None' = None) -> 'dict[str, Any]'"
        )
        assert str(inspect.signature(onboarding_import.apply_import)) == (
            "(plan: 'dict[str, Any]', *, data_home: 'Path | None' = None, "
            "cron_service: 'Any' = None, vector_store: 'VectorMemoryStore | None' = None, "
            "lesson_store: 'Any' = None, conflict_strategy: 'str' = 'skip') -> 'dict[str, Any]'"
        )

    def test_the_facade_logger_keeps_its_name(self) -> None:
        assert onboarding_import.logger.name == _FACADE_LOGGER

    @pytest.mark.parametrize("name", sorted(set(_CONSUMER_NAMES) - set(_FACADE_DEFINED)))
    def test_a_re_exported_name_is_its_owners_object(self, name: str) -> None:
        owner = importlib.import_module(_OWNERS[name])
        assert getattr(onboarding_import, name) is getattr(owner, name)

    @pytest.mark.parametrize("name", _FACADE_DEFINED)
    def test_a_facade_defined_name_is_defined_here(self, name: str) -> None:
        # The persistence-switch and cron-probe ratchets locate these writers by
        # ``onboarding_import.py::<name>``; the plan/apply entry points are here too.
        assert getattr(onboarding_import, name).__module__ == "kiro_crew.onboarding_import"


class TestLoggerIdentity:
    """Every engine warning lands on the facade's logger, whichever owner emits it."""

    @staticmethod
    def _engine_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if record.name.startswith("kiro_crew.onboarding") and record.levelno >= logging.WARNING
        ]

    def test_registry_warnings(self, caplog, install_sources) -> None:
        install_sources(_lineage_source(id="../escape"))
        with caplog.at_level(logging.WARNING):
            onboarding_import._sources()
        records = self._engine_records(caplog)
        assert any("becomes a path segment" in record.getMessage() for record in records)
        assert {record.name for record in records} == {_FACADE_LOGGER}

    def test_scanner_failure_warning(self, caplog, tmp_path: Path) -> None:
        def _explode(scan) -> None:
            raise RuntimeError("reader died")

        source = onboarding_import._Source(
            id="predecessor",
            display_name="Predecessor",
            scan=_explode,
            env_vars=(),
            home_dir=".predecessor",
            managed_mcp_names=frozenset(),
            superseded=False,
            stale_mcp_binaries=frozenset(),
        )
        root = tmp_path / ".predecessor"
        root.mkdir()
        with caplog.at_level(logging.WARNING):
            onboarding_import._scan_source("predecessor", root, tmp_path, source=source)
        records = self._engine_records(caplog)
        assert any("import scanner" in record.getMessage() for record in records)
        assert {record.name for record in records} == {_FACADE_LOGGER}

    def test_writer_warnings(self, caplog, tmp_path: Path, monkeypatch) -> None:
        """Restore-copy failures (MCP and skill) and a failed write, end to end."""
        # The MCP writer takes the dashboard's sidecar lock, which resolves under
        # the real ~/.kiro unless redirected: the host floor only rebinds it when
        # the handler module was already imported, which is not yet the case when
        # this test is the first one a process runs.
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        global_mcp = tmp_path / "kiro" / "settings" / "mcp.json"
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_mcp)
        monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))
        home = tmp_path / "home"
        codex = home / ".codex"
        (codex / "skills" / "demo").mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        project_toml = str(project).replace("\\", "\\\\")

        def _write_source(command: str, body: str) -> None:
            (codex / "config.toml").write_text(
                f'[mcp_servers.helper]\ncommand = "{command}"\n'
                f'[projects."{project_toml}"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            (codex / "skills" / "demo" / "SKILL.md").write_text(
                f"---\nname: demo\n---\n{body}\n", encoding="utf-8"
            )

        destination = tmp_path / "destination"
        destination.mkdir()
        # An unreadable destination config makes the workspace write fail.
        (destination / "config.json").write_text("{not json", encoding="utf-8")
        _write_source("first-helper", "First body.")
        with caplog.at_level(logging.WARNING):
            first = onboarding_import.apply_import(
                onboarding_import.preview_import(home=home, env={}), data_home=destination
            )
        assert any(entry["reason"] == "write_failed" for entry in first["skipped"])

        # Change both definitions upstream, then block the restore directory so
        # the pre-overwrite copy cannot be written: the overwrite must refuse.
        _write_source("second-helper", "Second body.")
        (destination / "imports" / "replaced").write_text("not a directory", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            second = onboarding_import.apply_import(
                onboarding_import.preview_import(home=home, env={}),
                data_home=destination,
                conflict_strategy="overwrite",
            )
        assert {entry["category_id"] for entry in second["conflicts"]} >= {
            "mcp_servers",
            "skills",
        }

        messages = [record.getMessage() for record in self._engine_records(caplog)]
        assert any("Foreign-agent import failed" in message for message in messages)
        assert any("MCP server being replaced" in message for message in messages)
        assert any("skill being replaced" in message for message in messages)
        assert {record.name for record in self._engine_records(caplog)} == {_FACADE_LOGGER}


class TestManagedNamesDuringAScan:
    """A contributed managed name is excluded while another source is scanned."""

    def test_an_unwired_scan_refuses_to_guess_managed_names(self, tmp_path: Path) -> None:
        """A scan built outside ``_scan_source`` fails closed at the MCP projection."""
        from kiro_crew.onboarding_scan import _Scan

        scan = _Scan(source_id="codex", root=tmp_path, user_home=tmp_path)
        with pytest.raises(RuntimeError, match="no registry"):
            scan.managed_mcp_names()

    def test_the_scan_consults_the_live_registry(self, tmp_path: Path, install_sources):
        """The lookup is the registry's own function, evaluated when the projection asks."""
        install_sources(_lineage_source(managed_mcp_names=("Predecessor-Core",)))
        seen: list = []
        source = dataclasses.replace(
            onboarding_import._sources()["codex"],
            scan=lambda scan: seen.append(scan.managed_mcp_names),
        )
        root = tmp_path / ".codex"
        root.mkdir()
        onboarding_import._scan_source("codex", root, tmp_path, source=source)
        assert seen == [onboarding_import._managed_mcp_names]
        assert "predecessor-core" in seen[0]()

    def test_a_contributed_managed_server_is_not_imported(self, tmp_path: Path, install_sources):
        install_sources(_lineage_source(managed_mcp_names=("Predecessor-Core",)))
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[mcp_servers.predecessor-core]\ncommand = "pc"\n'
            '[mcp_servers.kept]\ncommand = "kept"\n',
            encoding="utf-8",
        )

        plan = onboarding_import.preview_import(["codex"], home=home, env={})

        codex_plan = next(source for source in plan["sources"] if source["id"] == "codex")
        counts = {category["id"]: category["count"] for category in codex_plan["categories"]}
        assert counts["mcp_servers"] == 1
        assert any(
            entry["source_id"] == "codex" and entry["reason"] == "managed_server_excluded"
            for entry in plan["skipped"]
        )


def _engine_source(module: str) -> Path:
    """The source file of one engine module, facade included."""
    src = Path(onboarding_import.__file__).resolve().parent
    path = src / f"{module.split('.', 1)[1].replace('.', '/')}.py"
    return path if path.is_file() else path.with_suffix("") / "__init__.py"


class TestMirroredSeams:
    """A mirrored seam lives in ONE module; the facade reads it there and writes it there."""

    @pytest.fixture(autouse=True)
    def _restore_every_owner_binding(self):
        """Put each owner's original back directly, whatever a failing test left.

        Written through the owners' namespaces rather than through the facade, so a
        defect in the forwarding under test cannot also break its own cleanup.
        """
        saved = [
            (
                importlib.import_module(owner_name),
                name,
                vars(importlib.import_module(owner_name))[name],
            )
            for name, owner_name in _MIRRORED
        ]
        yield
        for owner, name, original in saved:
            vars(owner)[name] = original
            vars(onboarding_import).pop(name, None)

    @pytest.mark.parametrize(("name", "owner_name"), _MIRRORED, ids=_MIRRORED_IDS)
    def test_a_read_is_the_owners_object_and_the_facade_binds_no_copy(
        self, name: str, owner_name: str
    ) -> None:
        owner = importlib.import_module(owner_name)
        assert getattr(onboarding_import, name) is vars(owner)[name]
        assert name not in vars(onboarding_import)

    @pytest.mark.parametrize(("name", "owner_name"), _MIRRORED, ids=_MIRRORED_IDS)
    def test_the_owner_is_the_only_engine_module_that_binds_it(
        self, name: str, owner_name: str
    ) -> None:
        """One storage location: a second binding is one a patch on the owner misses."""
        holders = [
            module for module in _ENGINE_MODULES if name in vars(importlib.import_module(module))
        ]
        assert holders == [owner_name]

    @pytest.mark.parametrize(("name", "owner_name"), _MIRRORED, ids=_MIRRORED_IDS)
    def test_monkeypatch_sets_the_owner_and_undo_restores_its_original(
        self, name: str, owner_name: str
    ) -> None:
        owner = importlib.import_module(owner_name)
        original = vars(owner)[name]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(onboarding_import, name, sentinel)
            assert vars(owner)[name] is sentinel
            assert getattr(onboarding_import, name) is sentinel
            assert name not in vars(onboarding_import)
        assert vars(owner)[name] is original
        assert name not in vars(onboarding_import)

    @pytest.mark.parametrize(
        "patcher",
        [
            lambda name, new: mock.patch(f"kiro_crew.onboarding_import.{name}", new),
            lambda name, new: mock.patch.object(onboarding_import, name, new),
        ],
        ids=["mock.patch", "mock.patch.object"],
    )
    @pytest.mark.parametrize(("name", "owner_name"), _MIRRORED, ids=_MIRRORED_IDS)
    def test_mock_patch_round_trips_to_the_owners_original(
        self, name: str, owner_name: str, patcher: Callable[[str, object], Any]
    ) -> None:
        owner = importlib.import_module(owner_name)
        original = vars(owner)[name]
        sentinel = object()
        with patcher(name, sentinel):
            assert vars(owner)[name] is sentinel
            assert getattr(onboarding_import, name) is sentinel
        assert vars(owner)[name] is original
        assert name not in vars(onboarding_import)

    @pytest.mark.parametrize("inner_tool", ["monkeypatch", "mock.patch.object"])
    @pytest.mark.parametrize(
        "order", ["owner-then-facade", "facade-then-owner", "facade-then-facade"]
    )
    @pytest.mark.parametrize(("name", "owner_name"), _MIRRORED, ids=_MIRRORED_IDS)
    def test_nested_patches_undo_in_lifo_order(
        self, name: str, owner_name: str, order: str, inner_tool: str
    ) -> None:
        owner = importlib.import_module(owner_name)
        outer_target, inner_target = {
            "owner-then-facade": (owner, onboarding_import),
            "facade-then-owner": (onboarding_import, owner),
            "facade-then-facade": (onboarding_import, onboarding_import),
        }[order]
        original = vars(owner)[name]
        first, second = object(), object()
        with pytest.MonkeyPatch.context() as outer:
            outer.setattr(outer_target, name, first)
            with contextlib.ExitStack() as stack:
                if inner_tool == "monkeypatch":
                    stack.enter_context(pytest.MonkeyPatch.context()).setattr(
                        inner_target, name, second
                    )
                else:
                    stack.enter_context(mock.patch.object(inner_target, name, second))
                assert vars(owner)[name] is second
                assert getattr(onboarding_import, name) is second
            assert vars(owner)[name] is first
            assert getattr(onboarding_import, name) is first
        assert vars(owner)[name] is original
        assert name not in vars(onboarding_import)

    def test_a_name_outside_the_table_keeps_plain_module_behaviour(self) -> None:
        with mock.patch("kiro_crew.onboarding_import._not_a_seam", "set", create=True):
            assert vars(onboarding_import)["_not_a_seam"] == "set"
        assert "_not_a_seam" not in vars(onboarding_import)
        assert not hasattr(onboarding_import, "_not_a_seam")
        with pytest.raises(AttributeError, match="_not_a_seam"):
            getattr(onboarding_import, "_not_a_seam")


def _bare_loads(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for every bare-global read of a mirrored name in *source*.

    Any ``ast.Name`` in Load context whose id is a key of ``_EXPORTS``, at any depth,
    outside an ``import`` / ``from ... import`` node -- so the ``TYPE_CHECKING``
    imports are allowed and every other bare use is not.
    """

    class _Loads(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: list[tuple[int, str]] = []

        def visit_Import(self, node: ast.Import) -> None:
            return

        visit_ImportFrom = visit_Import  # type: ignore[assignment]

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id in onboarding_import._EXPORTS:
                self.found.append((node.lineno, node.id))

    loads = _Loads()
    loads.visit(ast.parse(source))
    return loads.found


class TestTheFacadesOwnCodeReadsTheOwner:
    """A function defined in the facade cannot reach ``__getattr__``.

    Module ``__getattr__`` answers an attribute access from OUTSIDE. A function
    defined in the facade resolves a bare global through the facade's own
    namespace, which the resolver never sees, so a bare reference would need the
    facade to bind the name -- the second storage location this removes -- and
    otherwise raises ``NameError`` on whatever path reaches it. Those functions
    ask for the owner and read the name off it instead.
    """

    def test_the_facade_reads_no_mirrored_name_as_a_bare_global(self) -> None:
        source = _engine_source("kiro_crew.onboarding_import").read_text(encoding="utf-8")
        assert _bare_loads(source) == [], (
            "the facade reads these mirrored names from its own namespace, which only "
            "resolves if it binds them; read them off _owner(<name>) instead"
        )

    def test_the_enumeration_can_fail(self) -> None:
        """The condition is discriminating, so an empty result means absence."""
        # Must flag: one bare Load of a mirrored name.
        assert _bare_loads("def f():\n    return _write_json\n") == [(2, "_write_json")]
        # Must ignore: the TYPE_CHECKING import and a read off the owner.
        assert (
            _bare_loads(
                "if TYPE_CHECKING:\n"
                "    from kiro_crew.onboarding_apply import _write_json\n"
                "def f(path, data):\n"
                '    _owner("_write_json")._write_json(path, data)\n'
            )
            == []
        )


def _type_checking_imports(source: str) -> dict[str, str]:
    """``name -> module`` for each ``from ... import`` under a module-level type guard.

    The guard is ``if TYPE_CHECKING:`` or ``if typing.TYPE_CHECKING:``; an import
    anywhere else, or under any other condition, is not collected.
    """
    found: dict[str, str] = {}
    for statement in ast.parse(source).body:
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        guarded = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        )
        if not guarded:
            continue
        for node in statement.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                found.update(dict.fromkeys((a.asname or a.name for a in node.names), node.module))
    return found


class TestTheMirroredNamesStayVisibleToTypeCheckers:
    """The facade binds no mirrored name at run time, so a type checker or an IDE
    sees one only through the ``TYPE_CHECKING`` imports, each from its owner."""

    def test_every_mirrored_name_is_imported_from_its_owner(self) -> None:
        source = _engine_source("kiro_crew.onboarding_import").read_text(encoding="utf-8")
        imported = _type_checking_imports(source)
        assert {name: imported.get(name) for name in onboarding_import._EXPORTS} == dict(
            onboarding_import._EXPORTS
        )

    def test_the_check_can_fail(self) -> None:
        """An import outside the guard, or under another condition, is not counted."""
        source = _engine_source("kiro_crew.onboarding_import").read_text(encoding="utf-8")
        # Must flag: the facade's own guard with one name dropped from it.
        assert source.count("        _toml,\n") == 1
        assert "_toml" not in _type_checking_imports(source.replace("        _toml,\n", ""))
        assert (
            _type_checking_imports(
                "from kiro_crew.onboarding_scan import _toml\n"
                "if DEBUG:\n"
                "    from kiro_crew.onboarding_scan import _MAX_FILES\n"
            )
            == {}
        )
        # Must ignore (count): both spellings of the guard.
        assert _type_checking_imports(
            "if TYPE_CHECKING:\n"
            "    from a import x\n"
            "if typing.TYPE_CHECKING:\n"
            "    from b import y as z\n"
        ) == {"x": "a", "z": "b"}


class TestALoadedOwnerIsReadWithoutImportModule:
    """The store answers; the import only fills it.

    ``importlib.import_module`` is an attribute of a module any caller can rebind,
    and tests do. If a loaded owner were fetched by calling it, every read and
    write of a seam here would be answered by whatever that patch returns for as
    long as it is installed.
    """

    @pytest.mark.parametrize(("name", "owner_name"), _MIRRORED, ids=_MIRRORED_IDS)
    def test_read_write_and_undo_never_call_import_module(self, name: str, owner_name: str) -> None:
        for module in {module for _name, module in _MIRRORED}:
            importlib.import_module(module)
        owner = sys.modules[owner_name]
        original = vars(owner)[name]
        sentinel = object()
        # ``_submodule`` calls ``importlib.import_module`` through the facade's own
        # ``importlib`` global, so that is the binding the refusal replaces.
        assert onboarding_import.importlib is importlib
        with mock.patch.object(
            onboarding_import.importlib,
            "import_module",
            side_effect=AssertionError("resolution called import_module"),
        ) as refused:
            assert getattr(onboarding_import, name) is original
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(onboarding_import, name, sentinel)
                assert vars(owner)[name] is sentinel
                assert getattr(onboarding_import, name) is sentinel
            assert vars(owner)[name] is original
            assert getattr(onboarding_import, name) is original
            assert refused.call_count == 0
            # The refusal sits on the binding the miss path calls: a module that is
            # not loaded yet reaches it.
            with pytest.raises(AssertionError, match="resolution called import_module"):
                onboarding_import._submodule("kiro_crew._onboarding_import_absent_probe")
            assert refused.call_count == 1


# ── A ``create`` on a mirrored name is refused ─────────────────────────────────
#
# ``mock.patch`` finds a mirrored name through the facade's ``__getattr__``, so it
# treats it as not local and restores it on exit by DELETING it, which the facade
# forwards to the owner, and then setting it back -- unless ``create`` is truthy,
# in which case it skips the set because it believes it created the name. The owner
# is left without its cap or helper for the rest of the worker. The forwarding
# cannot tell that delete from any other, so the guard below refuses, in every file
# under ``test/`` and every ``tests/`` package under ``src/`` that names the facade,
# each ``mock.patch`` spelling it resolves (listed on ``mirrored_create_patches``)
# whose target is a mirrored name on the facade and whose ``create`` is anything but
# the literal ``False``. The one exemption is ``_CREATE_PATCH_ALLOWLIST``, which the
# scan must equal exactly.

_FACADE = "kiro_crew.onboarding_import"

#: Reported in place of a mirrored name the source only computes at runtime.
_DYNAMIC = "<dynamic>"

#: Stands in for the part of a string the source only computes at runtime.
_HOLE = "\x00"

#: Where ``create`` sits positionally in each spelling. ``patch.dict`` has no such
#: parameter, so only a ``create=`` keyword counts there.
_CREATE_POSITION = {"": 3, "object": 4, "multiple": 2}

_MOCK_MODULES = ("unittest.mock", "mock")


@dataclasses.dataclass
class _Bindings:
    """What the names in one file are bound to, read off its import and assignment nodes."""

    roots: set[str] = dataclasses.field(default_factory=set)
    patchers: set[str] = dataclasses.field(default_factory=set)
    #: A name bound to ``patch.object``, ``patch.dict`` or ``patch.multiple`` -> which.
    methods: dict[str, str] = dataclasses.field(default_factory=dict)
    mock_modules: set[str] = dataclasses.field(default_factory=set)
    importlibs: set[str] = dataclasses.field(default_factory=set)
    importers: set[str] = dataclasses.field(default_factory=set)
    facades: set[str] = dataclasses.field(default_factory=set)
    helpers: set[str] = dataclasses.field(default_factory=set)
    strings: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _Hit:
    """One refused patch: its line, its enclosing test, and the name it addresses."""

    line: int
    scope: str
    name: str


def _dotted(node: ast.expr | None, bindings: _Bindings) -> str | None:
    """``a.b.c`` for an attribute chain rooted at a name a plain ``import`` bound."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bindings.roots:
        return None
    return ".".join([node.id, *reversed(parts)])


def _static_text(node: ast.expr | None, bindings: _Bindings) -> str:
    """The text a string expression evaluates to, with ``_HOLE`` for each runtime part.

    Resolves a literal, a module-level string constant, the facade's ``__name__``, an
    f-string and a ``+`` concatenation, each part by its own node.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _HOLE
    if isinstance(node, ast.Name):
        return bindings.strings.get(node.id, _HOLE)
    if isinstance(node, ast.Attribute) and node.attr == "__name__":
        return _FACADE if _is_facade(node.value, bindings) else _HOLE
    if isinstance(node, ast.JoinedStr):
        return "".join(_fstring_part(part, bindings) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_text(node.left, bindings) + _static_text(node.right, bindings)
    return _HOLE


def _fstring_part(part: ast.expr, bindings: _Bindings) -> str:
    if isinstance(part, ast.Constant) and isinstance(part.value, str):
        return part.value
    if isinstance(part, ast.FormattedValue) and part.conversion == -1 and not part.format_spec:
        return _static_text(part.value, bindings)
    return _HOLE


def _is_import_module(node: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(node, ast.Name):
        return node.id in bindings.importers
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "import_module"
        and isinstance(node.value, ast.Name)
        and node.value.id in bindings.importlibs
    ):
        return True
    return _dotted(node, bindings) == "importlib.import_module"


def _is_loaded_module(node: ast.expr | None, module: str, bindings: _Bindings) -> bool:
    """An ``import_module`` result or ``sys.modules`` entry for the module named *module*."""
    if isinstance(node, ast.Call):
        return (
            _is_import_module(node.func, bindings)
            and bool(node.args)
            and _static_text(node.args[0], bindings) == module
        )
    if isinstance(node, ast.Subscript):
        return (
            _dotted(node.value, bindings) == "sys.modules"
            and _static_text(node.slice, bindings) == module
        )
    return False


def _is_mock_module(node: ast.expr | None, bindings: _Bindings) -> bool:
    """Whether *node* evaluates to ``unittest.mock`` or the ``mock`` backport."""
    if isinstance(node, ast.Name):
        return node.id in bindings.mock_modules
    if isinstance(node, ast.Attribute):
        return _dotted(node, bindings) == "unittest.mock"
    return any(_is_loaded_module(node, module, bindings) for module in _MOCK_MODULES)


def _is_facade(node: ast.expr | None, bindings: _Bindings) -> bool:
    """Whether *node* evaluates to the facade module, read off the node's own shape."""
    if isinstance(node, ast.Name):
        return node.id in bindings.facades
    if isinstance(node, ast.Attribute):
        return _dotted(node, bindings) == _FACADE
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in bindings.helpers:
            return True
    return _is_loaded_module(node, _FACADE, bindings)


def _bindings(tree: ast.Module) -> _Bindings:
    """Read one file's bindings: imports, module-level strings, then assigned aliases.

    Assigned aliases are resolved to a fixed point, in whatever order the file binds
    them. A name assigned the facade, ``unittest.mock`` / ``mock``, ``patch``, or
    ``patch.object`` / ``.dict`` / ``.multiple`` -- directly, through an earlier
    alias, as an ``import_module`` result or a ``sys.modules`` entry -- is bound to
    it, and so is a function that returns the facade.
    """
    bindings = _Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    bindings.roots.add(alias.name.split(".")[0])
                    if alias.name == "mock":
                        bindings.mock_modules.add("mock")
                elif alias.name in _MOCK_MODULES:
                    bindings.mock_modules.add(alias.asname)
                elif alias.name == "importlib":
                    bindings.importlibs.add(alias.asname)
                elif alias.name == _FACADE:
                    bindings.facades.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module in _MOCK_MODULES and alias.name in ("patch", "*"):
                    bindings.patchers.add("patch" if alias.name == "*" else bound)
                elif node.module == "unittest" and alias.name == "mock":
                    bindings.mock_modules.add(bound)
                elif node.module == "importlib" and alias.name == "import_module":
                    bindings.importers.add(bound)
                elif node.module == "kiro_crew" and alias.name == "onboarding_import":
                    bindings.facades.add(bound)

    # Module-level string constants, each bound exactly once, resolved in order.
    assignments: list[tuple[str, ast.expr]] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            assignments.extend(
                (target.id, statement.value)
                for target in statement.targets
                if isinstance(target, ast.Name)
            )
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            assignments.append((statement.target.id, statement.value))
    bound_once = {name for name, count in Counter(n for n, _v in assignments).items() if count == 1}
    for name, value in assignments:
        text = _static_text(value, bindings)
        if name in bound_once and _HOLE not in text:
            bindings.strings[name] = text

    def size() -> tuple[int, ...]:
        return (
            len(bindings.facades),
            len(bindings.helpers),
            len(bindings.mock_modules),
            len(bindings.patchers),
            len(bindings.methods),
            len(bindings.importers),
        )

    while True:
        known = size()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(
                    isinstance(inner, ast.Return) and _is_facade(inner.value, bindings)
                    for inner in ast.walk(node)
                ):
                    bindings.helpers.add(node.name)
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            method = _patch_method(node.value, bindings)
            if _is_facade(node.value, bindings):
                bindings.facades.update(names)
            elif _is_mock_module(node.value, bindings):
                bindings.mock_modules.update(names)
            elif method == "":
                bindings.patchers.update(names)
            elif method is not None:
                bindings.methods.update(dict.fromkeys(names, method))
            elif _is_import_module(node.value, bindings):
                bindings.importers.update(names)
        if size() == known:
            return bindings


def _is_patcher(node: ast.expr, bindings: _Bindings) -> bool:
    """``patch`` under any binding the file makes: a name, or ``<mock module>.patch``."""
    if isinstance(node, ast.Name):
        return node.id in bindings.patchers
    if isinstance(node, ast.Attribute) and node.attr == "patch":
        return _is_mock_module(node.value, bindings)
    return False


def _patch_method(func: ast.expr, bindings: _Bindings) -> str | None:
    """``""`` for ``patch(...)``, the method name for ``patch.object/dict/multiple``."""
    if isinstance(func, ast.Name) and func.id in bindings.methods:
        return bindings.methods[func.id]
    if _is_patcher(func, bindings):
        return ""
    if (
        isinstance(func, ast.Attribute)
        and func.attr in ("object", "dict", "multiple")
        and _is_patcher(func.value, bindings)
    ):
        return func.attr
    return None


def _argument(call: ast.Call, index: int | None, keyword: str) -> ast.expr | None:
    if (
        index is not None
        and len(call.args) > index
        and not any(isinstance(arg, ast.Starred) for arg in call.args[: index + 1])
    ):
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def _create_may_be_set(call: ast.Call, method: str) -> bool:
    """Anything but a literal ``False``, a ``*`` / ``**`` splat included, may set it."""
    value = _argument(call, _CREATE_POSITION.get(method), "create")
    if value is None:
        return any(kw.arg is None for kw in call.keywords) or any(
            isinstance(arg, ast.Starred) for arg in call.args
        )
    return not (isinstance(value, ast.Constant) and value.value is False)


def _addressed(text: str, exports: Collection[str]) -> list[str]:
    """The mirrored name a dotted target addresses on the facade, if it names one."""
    if text.startswith(_FACADE + _HOLE):
        return [_DYNAMIC]
    prefix = f"{_FACADE}."
    if not text.startswith(prefix):
        return []
    name = text[len(prefix) :]
    if _HOLE in name:
        return [_DYNAMIC]
    return [name] if name in exports else []


def _patched_names(
    call: ast.Call, method: str, bindings: _Bindings, exports: Collection[str]
) -> list[str]:
    if method == "":
        return _addressed(_static_text(_argument(call, 0, "target"), bindings), exports)
    if method == "dict":
        in_dict = _argument(call, 0, "in_dict")
        if isinstance(in_dict, ast.Attribute) and _is_facade(in_dict.value, bindings):
            return [in_dict.attr] if in_dict.attr in exports else []
        return _addressed(_static_text(in_dict, bindings), exports)
    target = _argument(call, 0, "target")
    if not (_is_facade(target, bindings) or _static_text(target, bindings) == _FACADE):
        return []
    if method == "object":
        attribute = _static_text(_argument(call, 1, "attribute"), bindings)
        if _HOLE in attribute:
            return [_DYNAMIC]
        return [attribute] if attribute in exports else []
    names = [str(kw.arg) for kw in call.keywords if kw.arg in exports]
    if any(kw.arg is None for kw in call.keywords):
        names.append(_DYNAMIC)
    return names


class _CreatePatchFinder(ast.NodeVisitor):
    """Collect every refused patch, attributed to the def or class that encloses it.

    A decorator belongs to the def it decorates, so ``@patch(...)`` on a test is
    reported under that test's name.
    """

    def __init__(self, bindings: _Bindings, exports: Collection[str]) -> None:
        self.bindings = bindings
        self.exports = exports
        self.scope: list[str] = []
        self.hits: list[_Hit] = []

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def visit_Call(self, node: ast.Call) -> None:
        method = _patch_method(node.func, self.bindings)
        if method is not None and _create_may_be_set(node, method):
            scope = ".".join(self.scope) or "<module>"
            self.hits.extend(
                _Hit(node.lineno, scope, name)
                for name in _patched_names(node, method, self.bindings, self.exports)
            )
        self.generic_visit(node)


def mirrored_create_patches(tree: ast.Module, exports: Collection[str]) -> list[_Hit]:
    """Every ``mock.patch`` in *tree* that may pass ``create`` for a mirrored facade name.

    A hit is a call -- or a decorator -- to ``patch``, ``patch.object``,
    ``patch.dict`` or ``patch.multiple`` whose target resolves to the facade and
    names a mirrored seam, and whose ``create`` (keyword, positional or a splat) is
    anything but the literal ``False``. ``patch`` is recognised through
    ``unittest.mock`` or ``mock`` bound by an import, an assignment, an
    ``import_module`` result or a ``sys.modules`` entry, and as a name that a
    ``from ... import`` or an assignment binds to ``patch`` or one of its methods.
    The facade is recognised through a dotted string or a name bound to it (see
    ``_bindings``). A facade or ``patch`` received as a fixture or parameter, or
    fetched with ``getattr``, is not resolved. Once the target is the facade, a
    name the source only computes at runtime -- a variable, an unresolved f-string
    or concatenation, a ``**`` splat of ``patch.multiple`` names -- is reported as
    ``<dynamic>``.
    """
    finder = _CreatePatchFinder(_bindings(tree), exports)
    finder.visit(tree)
    return sorted(finder.hits, key=lambda hit: (hit.line, hit.name))


def _allowlist_mismatch(
    raw: set[tuple[str, str]], allowlist: frozenset[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """``(hits the allowlist does not name, allowlist entries nothing hits)``."""
    return raw - allowlist, set(allowlist) - raw


#: ``(path relative to the repository, qualified test name)`` of a deliberate
#: ``create`` premise test on a mirrored name. The scan must produce exactly this
#: set: an unlisted hit fails, and so does an entry nothing hits any more. Empty,
#: because no test needs one.
_CREATE_PATCH_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def _alias_case(import_line: str, patcher: str) -> str:
    """The three call spellings of one patch binding, each passing ``create=True``."""
    return (
        f"{import_line}\n"
        "from kiro_crew import onboarding_import\n"
        f'{patcher}("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        f'{patcher}.object(onboarding_import, "_toml", None, create=True)\n'
        f"{patcher}.multiple(onboarding_import, create=True, _MAX_DB_ROWS=1)\n"
    )


_ALIAS_HITS = [(3, "_MAX_FILES"), (4, "_toml"), (5, "_MAX_DB_ROWS")]

_MOCK = "from unittest import mock\nfrom kiro_crew import onboarding_import, onboarding_scan\n"

#: id -> (synthetic source, the ``(line, name)`` hits it must produce). Each rule the
#: guard enforces has a case it must flag and a case it must ignore.
_GUARD_CASES: dict[str, tuple[str, list[tuple[int, str]]]] = {
    # 1. Every binding of ``patch``: three aliases and the three plain spellings.
    "alias/flag: from unittest.mock import patch as X": (
        _alias_case("from unittest.mock import patch as X", "X"),
        _ALIAS_HITS,
    ),
    "alias/flag: from unittest import mock as X": (
        _alias_case("from unittest import mock as X", "X.patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: import unittest.mock as X": (
        _alias_case("import unittest.mock as X", "X.patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: from unittest.mock import patch": (
        _alias_case("from unittest.mock import patch", "patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: from unittest import mock": (
        _alias_case("from unittest import mock", "mock.patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: import unittest.mock": (
        _alias_case("import unittest.mock", "unittest.mock.patch"),
        _ALIAS_HITS,
    ),
    "alias/ignore: a patch that is not unittest.mock's": (
        _alias_case("from requests import patch as X", "X"),
        [],
    ),
    "alias/ignore: a module named mock that is not unittest.mock": (
        _alias_case("import requests as mock", "mock.patch"),
        [],
    ),
    # 2. Decorators, attributed to the test they decorate.
    "decorator/flag": (
        "from unittest.mock import patch as X\n"
        + _MOCK
        + '@X("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        '@X.object(onboarding_import, "_toml", None, create=True)\n'
        "def test_decorated():\n    pass\n",
        [(4, "_MAX_FILES"), (5, "_toml")],
    ),
    "decorator/ignore: no create": (
        "from unittest.mock import patch as X\n"
        + _MOCK
        + '@X("kiro_crew.onboarding_import._MAX_FILES", 1)\n'
        '@X.object(onboarding_import, "_toml", None)\n'
        "def test_decorated():\n    pass\n",
        [],
    ),
    # 3. f-string targets built from the facade's __name__ or a module constant.
    "f-string/flag": (
        _MOCK + 'FACADE = "kiro_crew.onboarding_import"\n'
        'mock.patch(f"{onboarding_import.__name__}._MAX_FILES", 1, create=True)\n'
        'mock.patch(f"{FACADE}._is_link_like", None, create=True)\n',
        [(4, "_MAX_FILES"), (5, "_is_link_like")],
    ),
    "f-string/ignore: the owner's name, not the facade's": (
        _MOCK + 'OWNER = "kiro_crew.onboarding_scan"\n'
        'mock.patch(f"{onboarding_scan.__name__}._MAX_FILES", 1, create=True)\n'
        'mock.patch(f"{OWNER}._MAX_FILES", 1, create=True)\n',
        [],
    ),
    # 4. Concatenation onto a module constant.
    "concatenation/flag": (
        _MOCK + 'FACADE = "kiro_crew.onboarding_import"\n'
        'mock.patch(FACADE + "._MAX_FILES", 1, create=True)\n',
        [(4, "_MAX_FILES")],
    ),
    "concatenation/ignore: onto the owner's name": (
        _MOCK + 'OWNER = "kiro_crew.onboarding_scan"\n'
        'mock.patch(OWNER + "._MAX_FILES", 1, create=True)\n',
        [],
    ),
    # 5. The keyword spellings of target and attribute.
    "keywords/flag": (
        _MOCK + "mock.patch.object(\n"
        '    target=onboarding_import, attribute="_MAX_FILES", new=1, create=True\n'
        ")\n"
        'mock.patch(target="kiro_crew.onboarding_import._toml", new=None, create=True)\n'
        "mock.patch.multiple(target=onboarding_import, create=True, _MAX_DB_ROWS=1)\n",
        [(3, "_MAX_FILES"), (6, "_toml"), (7, "_MAX_DB_ROWS")],
    ),
    "keywords/ignore: the owner as target": (
        _MOCK + "mock.patch.object(\n"
        '    target=onboarding_scan, attribute="_MAX_FILES", new=1, create=True\n'
        ")\n",
        [],
    ),
    # 6. A create that is not a literal: a number, a name, None, positional, a splat.
    "create/flag: any value but the literal False": (
        _MOCK + 'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=1)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=flag)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=None)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, None, True)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, **options)\n'
        "mock.patch.multiple(onboarding_import, create=flag, _MAX_DB_ROWS=1)\n",
        [
            (3, "_MAX_FILES"),
            (4, "_MAX_FILES"),
            (5, "_MAX_FILES"),
            (6, "_MAX_FILES"),
            (7, "_MAX_FILES"),
            (8, "_MAX_DB_ROWS"),
        ],
    ),
    "create/ignore: a non-literal create on a name outside the table": (
        _MOCK + 'mock.patch.object(onboarding_import, "_not_mirrored", 1, create=flag)\n'
        "mock.patch.multiple(onboarding_import, create=flag, _not_mirrored=1)\n",
        [],
    ),
    # 7. create=False, and no create at all, are the safe spellings.
    "create-false/flag: the same patches with create=True": (
        _MOCK + 'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        'mock.patch("kiro_crew.onboarding_import._toml", None, create=True)\n',
        [(3, "_MAX_FILES"), (4, "_toml")],
    ),
    "create-false/ignore": (
        _MOCK + 'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=False)\n'
        'mock.patch("kiro_crew.onboarding_import._toml", None, create=False)\n'
        "mock.patch.multiple(onboarding_import, create=False, _MAX_DB_ROWS=1)\n"
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1)\n',
        [],
    ),
    # 8. On the facade, a name the source only computes at runtime is a hit.
    "dynamic/flag": (
        _MOCK + 'FACADE = "kiro_crew.onboarding_import"\n'
        "mock.patch.object(onboarding_import, name, 1, create=True)\n"
        'mock.patch(f"kiro_crew.onboarding_import.{name}", 1, create=True)\n'
        'mock.patch(FACADE + "." + name, 1, create=True)\n'
        "mock.patch.multiple(onboarding_import, create=True, **names)\n",
        [(4, _DYNAMIC), (5, _DYNAMIC), (6, _DYNAMIC), (7, _DYNAMIC)],
    ),
    "dynamic/ignore: off the facade, or with create=False": (
        _MOCK + "mock.patch.object(onboarding_scan, name, 1, create=True)\n"
        "mock.patch(target_name, 1, create=True)\n"
        "mock.patch.object(onboarding_import, name, 1, create=False)\n",
        [],
    ),
    # 9. Assigned aliases of the mock module, ``patch`` and its methods, in any order.
    "mock-alias/flag: assigned mock modules and patch callables": (
        "import importlib as il\n"
        "import sys\n"
        "import unittest.mock\n"
        "from unittest import mock\n"
        "from kiro_crew import onboarding_import\n"
        "m = unittest.mock\n"
        "m2 = m\n"
        "p = mock.patch\n"
        "patcher = mock.patch.object\n"
        "multi = p.multiple\n"
        'lm = il.import_module("unittest.mock")\n'
        'sm = sys.modules["unittest.mock"]\n'
        'm2.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        'p("kiro_crew.onboarding_import._toml", None, create=True)\n'
        'patcher(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        "multi(onboarding_import, create=True, _MAX_DB_ROWS=1)\n"
        'lm.patch.object(onboarding_import, "_toml", None, create=True)\n'
        'sm.patch.object(onboarding_import, "_toml", None, create=True)\n'
        'late(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        "late = patcher\n"
        "load = il.import_module\n"
        'load("unittest.mock").patch("kiro_crew.onboarding_import._toml", None, create=True)\n',
        [
            (13, "_MAX_FILES"),
            (14, "_toml"),
            (15, "_MAX_FILES"),
            (16, "_MAX_DB_ROWS"),
            (17, "_toml"),
            (18, "_toml"),
            (19, "_MAX_FILES"),
            (22, "_toml"),
        ],
    ),
    "mock-alias/ignore: assigned names that are not mock's patch": (
        "import importlib as il\n"
        "import requests\n"
        "from unittest import mock\n"
        "from kiro_crew import onboarding_import\n"
        "m = requests\n"
        "p = requests.patch\n"
        "patcher = mock.MagicMock\n"
        'lm = il.import_module("requests")\n'
        'm.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        'p("kiro_crew.onboarding_import._toml", None, create=True)\n'
        'patcher(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        'lm.patch.object(onboarding_import, "_toml", None, create=True)\n',
        [],
    ),
    # 10. Facade aliases, resolved to a fixed point.
    "facade-alias/flag": (
        _MOCK + "import importlib\n"
        "import sys\n"
        "from importlib import import_module as load\n"
        "def _api():\n"
        "    return m\n"
        'mock.patch.object(m, "_toml", None, create=True)\n'
        "m = oi\n"
        "oi = onboarding_import\n"
        'loaded = importlib.import_module("kiro_crew.onboarding_import")\n'
        'mock.patch.object(loaded, "_toml", None, create=True)\n'
        'mock.patch.object(load(onboarding_import.__name__), "_toml", None, create=True)\n'
        'mock.patch.object(_api(), "_toml", None, create=True)\n'
        'mock.patch.object(sys.modules["kiro_crew.onboarding_import"], "_toml", create=True)\n',
        [(8, "_toml"), (12, "_toml"), (13, "_toml"), (14, "_toml"), (15, "_toml")],
    ),
    "facade-alias/ignore: aliases of the owner": (
        _MOCK + "import importlib\n"
        "oi = onboarding_scan\n"
        "m = oi\n"
        'mock.patch.object(m, "_MAX_FILES", 1, create=True)\n'
        'loaded = importlib.import_module("kiro_crew.onboarding_scan")\n'
        'mock.patch.object(loaded, "_MAX_FILES", 1, create=True)\n',
        [],
    ),
}


@pytest.mark.parametrize("case", sorted(_GUARD_CASES))
def test_the_create_detector_answers_both_ways(case: str) -> None:
    """The detector the guard below rests on, pinned on synthetic sources.

    A detector that matches nothing would make the guard pass while checking
    nothing, so every rule it enforces is measured on a source it must flag and a
    source it must ignore.
    """
    source, expected = _GUARD_CASES[case]
    hits = mirrored_create_patches(ast.parse(source), onboarding_import._EXPORTS)
    assert [(hit.line, hit.name) for hit in hits] == expected


def test_a_hit_is_attributed_to_the_test_that_holds_it() -> None:
    """The allowlist key is the enclosing test, a decorator's included."""
    source = (
        _MOCK + "class TestSeam:\n"
        '    @mock.patch.object(onboarding_import, "_toml", None, create=True)\n'
        "    def test_decorated(self):\n"
        "        pass\n"
        "    def test_inline(self):\n"
        '        mock.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        'mock.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
    )
    hits = mirrored_create_patches(ast.parse(source), onboarding_import._EXPORTS)
    assert [hit.scope for hit in hits] == [
        "TestSeam.test_decorated",
        "TestSeam.test_inline",
        "<module>",
    ]


def test_the_allowlist_must_equal_the_scan() -> None:
    """An unlisted hit and a stale entry both fail; an exact match is the only pass."""
    hit = ("test/test_example.py", "TestSeam.test_premise")
    # Must flag: a hit the allowlist does not name, and an entry nothing hits.
    assert _allowlist_mismatch({hit}, frozenset()) == ({hit}, set())
    assert _allowlist_mismatch(set(), frozenset({hit})) == (set(), {hit})
    # Must ignore: a hit the allowlist names exactly.
    assert _allowlist_mismatch({hit}, frozenset({hit})) == (set(), set())


def _patching_test_sources() -> list[tuple[Path, str]]:
    """Every test file whose text contains ``onboarding_import``, from the files git sees.

    ``test/`` and every ``tests/`` package under ``src/``. Read through
    ``source_corpus.repo_files`` rather than a walk, so another checkout under the
    repository is never mistaken for this one. The text filter keeps the scan off
    the thousands of files that never mention the facade (parsing all of them takes
    about a minute). Every import, alias, ``__name__`` and dotted-string spelling
    the guard resolves contains that text, so the one spelling it skips is the
    module name split across separate string literals.
    """
    root = source_corpus.repo_root()
    rows: list[tuple[Path, str]] = []
    for path in source_corpus.repo_files():
        if path.suffix != ".py":
            continue
        parts = path.relative_to(root).parts
        if not (parts[0] == "test" or (parts[0] == "src" and "tests" in parts[:-1])):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "onboarding_import" in text:
            rows.append((path, text))
    return rows


def test_no_test_patches_a_mirrored_name_with_create() -> None:
    """The raw scan of every test file equals the allowlist, which is empty."""
    root = source_corpus.repo_root()
    sources = _patching_test_sources()
    assert Path(__file__).resolve() in {path.resolve() for path, _text in sources}, (
        "the scan did not reach this file, which names the facade, so it reads "
        "nothing and its empty result means nothing"
    )
    found: dict[tuple[str, str], list[_Hit]] = {}
    for path, text in sources:
        relative = path.relative_to(root).as_posix()
        for hit in mirrored_create_patches(ast.parse(text), onboarding_import._EXPORTS):
            found.setdefault((relative, hit.scope), []).append(hit)
    unexpected, stale = _allowlist_mismatch(set(found), _CREATE_PATCH_ALLOWLIST)
    assert set(found) == _CREATE_PATCH_ALLOWLIST, (
        "mock.patch with a create other than the literal False on a mirrored "
        "onboarding_import name deletes the owner's binding on exit and never puts it "
        f"back. Unlisted: {sorted((key, found[key]) for key in unexpected)}. Allowlist "
        f"entries nothing hits: {sorted(stale)}. Drop the create; the name exists."
    )


def _counts(plan: dict, source_id: str) -> dict[str, int]:
    source = next(source for source in plan["sources"] if source["id"] == source_id)
    return {category["id"]: category["count"] for category in source["categories"]}


def _reasons(plan: dict, source_id: str) -> set[tuple[str, str]]:
    return {
        (entry["category_id"], entry["reason"])
        for entry in plan["skipped"]
        if entry["source_id"] == source_id
    }


def _raise_oserror(*_args: object, **_kwargs: object) -> Any:
    raise OSError("refused by the test")


class _Spy:
    """Record each call, then delegate to the real helper."""

    def __init__(self, target: Callable[..., Any]) -> None:
        self.target = target
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        return self.target(*args, **kwargs)


def _claude_skill(home: Path, body: str = "Review the diff.") -> Path:
    """One ``~/.claude`` skill package: a manifest and an asset."""
    skill = home / ".claude" / "skills" / "review"
    (skill / "assets").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: review\n---\n{body}\n", encoding="utf-8")
    (skill / "assets" / "checklist.txt").write_text("Read every hunk.\n", encoding="utf-8")
    return skill


def _claude_home(home: Path) -> None:
    """The skill package plus three memory notes."""
    _claude_skill(home)
    memory = home / ".claude" / "memory"
    memory.mkdir()
    for index in range(3):
        (memory / f"note{index}.md").write_text(f"Memory note {index}.\n", encoding="utf-8")


def _codex_home(home: Path, command: str = "helper-mcp") -> None:
    """Two MCP servers in ``~/.codex/config.toml``."""
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(
        f'[mcp_servers.helper]\ncommand = "{command}"\n'
        '[mcp_servers.other]\ncommand = "other-mcp"\n',
        encoding="utf-8",
    )


def _lineage_home(home: Path) -> None:
    """A predecessor install whose ``memory.db`` holds two directives."""
    root = home / ".predecessor"
    root.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(root / "memory.db")) as connection:
        with connection:
            connection.execute(
                "CREATE TABLE semantic_memory "
                "(key TEXT, value_json TEXT, confidence REAL, is_deleted INTEGER, kind TEXT)"
            )
            connection.executemany(
                "INSERT INTO semantic_memory VALUES (?, ?, 0.9, 0, 'directive')",
                [
                    ("lesson.squash", json.dumps("Always squash before pushing a branch.")),
                    ("lesson.history", json.dumps("Never rewrite a shared branch history.")),
                ],
            )


def _openclaw_home(home: Path, agents: int = 1) -> Path:
    """An OpenClaw state root whose ``agents/`` directory holds *agents* agents."""
    agents_root = home / ".openclaw" / "agents"
    for index in range(agents):
        (agents_root / f"agent{index}").mkdir(parents=True)
    return agents_root


def _hermes_profile_home(home: Path) -> Path:
    """A Hermes root whose one ``profiles/`` entry holds a memory note."""
    profiles = home / ".hermes" / "profiles"
    memories = profiles / "work" / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("Profile memory note.\n", encoding="utf-8")
    return profiles


def _codex_automations_home(home: Path) -> None:
    """A Codex automations database with an empty ``automations`` table."""
    database = home / ".codex" / "sqlite" / "codex-dev.db"
    database.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute("CREATE TABLE automations (id TEXT, rrule TEXT)")


def _gemini_home(home: Path) -> None:
    """A Gemini project file naming one workspace by ``file://`` URI."""
    workspace = home / "project"
    workspace.mkdir(parents=True)
    projects = home / ".gemini" / "config" / "projects"
    projects.mkdir(parents=True)
    (projects / "p.json").write_text(
        json.dumps({"projectResources": {"resources": [{"folderUri": workspace.as_uri()}]}}),
        encoding="utf-8",
    )


#: seam -> (replacement, fixture, source id, the (category, reason) only the patch causes)
_DIAGNOSTIC_SEAMS: dict[str, tuple[Any, Callable[[Path], None], str, tuple[str, str]]] = {
    "_MAX_FILES": (1, _claude_home, "claude_code", ("memories", "file_count_limit")),
    "_MAX_WALK_ENTRIES": (1, _claude_home, "claude_code", ("memories", "walk_entry_limit")),
    "_MAX_SKILL_BYTES": (2, _claude_home, "claude_code", ("skills", "file_too_large")),
    "_MAX_SKILL_PACKAGE_BYTES": (
        4,
        _claude_home,
        "claude_code",
        ("skills", "skill_package_too_large"),
    ),
    "_MAX_MCP_SERVERS": (1, _codex_home, "codex", ("mcp_servers", "item_count_limit")),
    "_toml": (None, _codex_home, "codex", ("settings", "toml_parser_unavailable")),
    "_MAX_DB_ROWS": (1, _lineage_home, "predecessor", ("memories", "row_count_limit")),
    "_MAX_DB_BYTES": (1, _lineage_home, "predecessor", ("memories", "database_too_large")),
    "_MAX_IMPORTED_LESSONS": (
        1,
        _lineage_home,
        "predecessor",
        ("instructions", "instruction_count_limit"),
    ),
    "url2pathname": (
        _raise_oserror,
        _gemini_home,
        "gemini",
        ("workspaces", "workspace_uri_invalid"),
    ),
}


def _preview(source_id: str, home: Path) -> dict:
    return onboarding_import.preview_import([source_id], home=home, env={})


def _tables(spy: _Spy) -> list[str]:
    return [str(table) for _connection, table in spy.calls]


def _sqlite_columns_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    _lineage_home(home)
    install_sources(_lineage_source())
    spy = patch_seam(_Spy(original))
    plan = _preview("predecessor", home)
    assert "semantic_memory" in _tables(spy), "the lineage reader missed the patch"
    assert _counts(plan, "predecessor") == {"instructions": 2}


def _scan_lineage_install_scenario(
    tmp_path: Path, original: Any, patch_seam, install_sources
) -> None:
    home = tmp_path / "home"
    _lineage_home(home)
    # Before composing the context: the registry binds a registered source's
    # reader when it normalizes the descriptor.
    spy = patch_seam(_Spy(original))
    install_sources(_lineage_source())
    _preview("predecessor", home)
    assert [scan.source_id for (scan,) in spy.calls] == ["predecessor"]


def _skill_install_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    _claude_skill(home)
    spy = patch_seam(_Spy(original))
    result = onboarding_import.apply_import(
        _preview("claude_code", home), data_home=tmp_path / "destination"
    )
    assert spy.calls, "the skill writer did not call the patched helper"
    assert result["imported"]["skills"] == 1


def _write_json_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    _codex_home(home)
    destination = tmp_path / "destination"
    spy = patch_seam(_Spy(original))
    onboarding_import.apply_import(_preview("codex", home), data_home=destination)
    # The facade's own ledger flush calls it too; the MCP file is the owner's write.
    assert destination / "mcp.json" in [call[0] for call in spy.calls]


def _is_link_like_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    skill = _claude_skill(home)
    assert _counts(_preview("claude_code", home), "claude_code") == {"skills": 1}
    patch_seam(lambda path, file_stat=None: path == skill or original(path, file_stat))
    plan = _preview("claude_code", home)
    assert "skills" not in _counts(plan, "claude_code")
    assert ("skills", "symlink_rejected") in _reasons(plan, "claude_code")


def _facade_probe_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    """``_source_exists`` and ``_preview`` read ``_is_link_like`` in the facade itself."""
    home = tmp_path / "home"
    _claude_skill(home)
    claude_root = home / ".claude"
    codex_root = home / ".codex"  # never created: only the link probe can admit it
    plan = _preview("claude_code", home)
    patch_seam(
        lambda path, file_stat=None: path in (claude_root, codex_root) or original(path, file_stat)
    )
    result = onboarding_import.apply_import(plan, data_home=tmp_path / "destination")
    # ``_source_exists`` refuses the link-like root, so apply never rescans it.
    assert ("claude_code", "skills", "source_unavailable") in {
        (entry["source_id"], entry["category_id"], entry["reason"]) for entry in result["skipped"]
    }
    # ``_preview`` scans an absent root only when the probe calls it link-like.
    assert ("settings", "symlink_rejected") in _reasons(_preview("codex", home), "codex")


def _scan_source_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    """The registry's dispatch refuses a link-like root before any adapter reads it."""
    home = tmp_path / "home"
    _codex_home(home)
    root = home / ".codex"
    patch_seam(lambda path, file_stat=None: path == root or original(path, file_stat))
    plan = _preview("codex", home)
    assert _counts(plan, "codex") == {}
    assert _reasons(plan, "codex") == {("settings", "symlink_rejected")}


def _apply_ancestor_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    """The skill writer re-checks each destination component just before writing."""
    home = tmp_path / "home"
    _claude_skill(home)
    destination = tmp_path / "destination"
    ancestor = destination / "skills" / "imported" / "claude_code"
    ancestor.mkdir(parents=True)
    plan = _preview("claude_code", home)
    patch_seam(lambda path, file_stat=None: path == ancestor or original(path, file_stat))
    result = onboarding_import.apply_import(plan, data_home=destination)
    assert result["imported"]["skills"] == 0
    assert not (ancestor / "review").exists()


def _openclaw_agents_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    agents_root = _openclaw_home(home)
    diagnostic = ("workspaces", "symlink_rejected")
    assert diagnostic not in _reasons(_preview("openclaw", home), "openclaw")
    patch_seam(lambda path, file_stat=None: path == agents_root or original(path, file_stat))
    assert diagnostic in _reasons(_preview("openclaw", home), "openclaw")


def _openclaw_agent_cap_scenario(
    tmp_path: Path, original: Any, patch_seam, install_sources
) -> None:
    home = tmp_path / "home"
    _openclaw_home(home, agents=3)
    diagnostic = ("workspaces", "agent_count_limit")
    assert diagnostic not in _reasons(_preview("openclaw", home), "openclaw")
    patch_seam(1)
    assert diagnostic in _reasons(_preview("openclaw", home), "openclaw")


def _hermes_profiles_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    profiles = _hermes_profile_home(home)
    assert _counts(_preview("hermes", home), "hermes") == {"memories": 1}
    patch_seam(lambda path, file_stat=None: path == profiles or original(path, file_stat))
    assert "memories" not in _counts(_preview("hermes", home), "hermes")


def _codex_automations_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    home = tmp_path / "home"
    _codex_automations_home(home)
    spy = patch_seam(_Spy(original))
    _preview("codex", home)
    assert _tables(spy) == ["automations"]


def _hermes_projects_db_scenario(
    tmp_path: Path, original: Any, patch_seam, install_sources
) -> None:
    """``preview_import`` does not reach this reader, so it is driven directly."""
    root = tmp_path / ".hermes"
    root.mkdir()
    with contextlib.closing(sqlite3.connect(root / "projects.db")) as connection:
        with connection:
            connection.execute("CREATE TABLE projects (path TEXT)")
    spy = patch_seam(_Spy(original))
    hermes = importlib.import_module("kiro_crew.onboarding_sources.hermes")
    scan = onboarding_scan._Scan(source_id="hermes", root=root, user_home=tmp_path)
    hermes._scan_hermes_projects_db(scan, root)
    assert _tables(spy) == ["projects"]


def _ledger_flush_scenario(tmp_path: Path, original: Any, patch_seam, install_sources) -> None:
    """The facade's own ledger flush writes through the apply owner's binding."""
    home = tmp_path / "home"
    _claude_skill(home)
    destination = tmp_path / "destination"
    ledger = (
        destination / importlib.import_module("kiro_crew.onboarding_apply")._LEDGER_RELATIVE_PATH
    )
    spy = patch_seam(_Spy(original))
    onboarding_import.apply_import(_preview("claude_code", home), data_home=destination)
    assert ledger in [call[0] for call in spy.calls]


def _preserve_replaced_tree_scenario(
    tmp_path: Path, original: Any, patch_seam, install_sources
) -> None:
    home = tmp_path / "home"
    destination = tmp_path / "destination"
    _claude_skill(home, "First body.")
    first = onboarding_import.apply_import(_preview("claude_code", home), data_home=destination)
    assert first["imported"]["skills"] == 1
    _claude_skill(home, "Second body.")
    patch_seam(_raise_oserror)
    second = onboarding_import.apply_import(
        _preview("claude_code", home), data_home=destination, conflict_strategy="overwrite"
    )
    assert [(entry["category_id"], entry["reason"]) for entry in second["conflicts"]] == [
        ("skills", "destination_conflict")
    ]


def _preserve_replaced_json_scenario(
    tmp_path: Path, original: Any, patch_seam, install_sources
) -> None:
    home = tmp_path / "home"
    destination = tmp_path / "destination"
    _codex_home(home, "first-helper")
    first = onboarding_import.apply_import(_preview("codex", home), data_home=destination)
    assert first["imported"]["mcp_servers"] == 2
    _codex_home(home, "second-helper")
    patch_seam(_raise_oserror)
    second = onboarding_import.apply_import(
        _preview("codex", home), data_home=destination, conflict_strategy="overwrite"
    )
    assert [(entry["category_id"], entry["reason"]) for entry in second["conflicts"]] == [
        ("mcp_servers", "destination_conflict")
    ]


#: seam -> a scenario that patches it through ``patch_seam`` and proves the owner saw it.
_CALL_SITE_SEAMS: dict[str, Callable[..., None]] = {
    "_sqlite_columns": _sqlite_columns_scenario,
    "_scan_lineage_install": _scan_lineage_install_scenario,
    "_has_symlink_component": _skill_install_scenario,
    "_install_skill_tree": _skill_install_scenario,
    "_write_json": _write_json_scenario,
    "_is_link_like": _is_link_like_scenario,
    "_preserve_replaced_tree": _preserve_replaced_tree_scenario,
    "_preserve_replaced_json": _preserve_replaced_json_scenario,
}


#: ``(seam, a module other than its owner that reads it)`` -> a scenario that patches
#: ONLY the facade and proves that module's call site saw the patch.
_CROSS_MODULE_READERS: dict[tuple[str, str], Callable[..., None]] = {
    ("_MAX_FILES", "kiro_crew.onboarding_sources.openclaw"): _openclaw_agent_cap_scenario,
    ("_is_link_like", "kiro_crew.onboarding_apply"): _apply_ancestor_scenario,
    ("_is_link_like", "kiro_crew.onboarding_import"): _facade_probe_scenario,
    ("_is_link_like", "kiro_crew.onboarding_sources"): _scan_source_scenario,
    ("_is_link_like", "kiro_crew.onboarding_sources.hermes"): _hermes_profiles_scenario,
    ("_is_link_like", "kiro_crew.onboarding_sources.openclaw"): _openclaw_agents_scenario,
    ("_scan_lineage_install", "kiro_crew.onboarding_sources"): _scan_lineage_install_scenario,
    ("_sqlite_columns", "kiro_crew.onboarding_sources.codex"): _codex_automations_scenario,
    ("_sqlite_columns", "kiro_crew.onboarding_sources.hermes"): _hermes_projects_db_scenario,
    ("_sqlite_columns", "kiro_crew.onboarding_sources.lineage"): _sqlite_columns_scenario,
    ("_write_json", "kiro_crew.onboarding_import"): _ledger_flush_scenario,
}


def _cross_module_reads() -> set[tuple[str, str]]:
    """``(seam, module)`` for every attribute read of a seam outside its owner.

    Read off the engine's source, so a new reader is a new pair here on the commit
    that adds it, and it needs a scenario before this passes.
    """
    exports = onboarding_import._EXPORTS
    reads: set[tuple[str, str]] = set()
    for module in _ENGINE_MODULES:
        tree = ast.parse(_engine_source(module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and exports.get(node.attr, module) != module:
                reads.add((node.attr, module))
    return reads


class TestAFacadePatchReachesTheMovedCallSite:
    """Patch ONLY ``onboarding_import.<name>``, then drive the public entry points."""

    def test_every_mirrored_seam_has_a_scenario(self) -> None:
        assert sorted({*_DIAGNOSTIC_SEAMS, *_CALL_SITE_SEAMS}) == sorted(onboarding_import._EXPORTS)

    def test_every_cross_module_reader_has_a_scenario(self) -> None:
        assert sorted(_cross_module_reads()) == sorted(_CROSS_MODULE_READERS)

    @pytest.mark.parametrize("name", sorted(_DIAGNOSTIC_SEAMS))
    def test_a_patched_cap_or_parser_changes_the_plan(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, install_sources
    ) -> None:
        replacement, write_fixture, source_id, diagnostic = _DIAGNOSTIC_SEAMS[name]
        home = tmp_path / "home"
        write_fixture(home)
        if source_id == "predecessor":
            install_sources(_lineage_source())
        assert diagnostic not in _reasons(_preview(source_id, home), source_id)

        monkeypatch.setattr(onboarding_import, name, replacement)

        assert diagnostic in _reasons(_preview(source_id, home), source_id)

    @pytest.mark.parametrize("name", sorted(_CALL_SITE_SEAMS))
    def test_a_patched_helper_is_the_one_the_owner_calls(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, install_sources
    ) -> None:
        def patch_seam(replacement: Any) -> Any:
            monkeypatch.setattr(onboarding_import, name, replacement)
            return replacement

        _CALL_SITE_SEAMS[name](
            tmp_path, getattr(onboarding_import, name), patch_seam, install_sources
        )

    @pytest.mark.parametrize(
        ("name", "reader"),
        sorted(_CROSS_MODULE_READERS),
        ids=[f"{reader.split('.', 1)[1]}:{name}" for name, reader in sorted(_CROSS_MODULE_READERS)],
    )
    def test_a_facade_patch_reaches_the_cross_module_reader(
        self,
        name: str,
        reader: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        install_sources,
    ) -> None:
        def patch_seam(replacement: Any) -> Any:
            monkeypatch.setattr(onboarding_import, name, replacement)
            return replacement

        _CROSS_MODULE_READERS[(name, reader)](
            tmp_path, getattr(onboarding_import, name), patch_seam, install_sources
        )


class TestFacadeModuleAttributes:
    def test_shutil_and_platform_compat_are_the_shared_modules(self) -> None:
        assert onboarding_import.shutil is shutil
        assert onboarding_import.platform_compat is platform_compat

    @pytest.mark.parametrize(("is_windows", "expected"), [(True, "profile"), (False, "posix")])
    def test_a_platform_patch_through_the_facade_reaches_home_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        is_windows: bool,
        expected: str,
    ) -> None:
        monkeypatch.setattr(onboarding_import.platform_compat, "IS_WINDOWS", is_windows)
        env = {"HOME": str(tmp_path / "posix"), "USERPROFILE": str(tmp_path / "profile")}
        assert onboarding_sources._home_from(None, env) == tmp_path / expected


@pytest.mark.parametrize("attr", _MCP_HOST_PATHS)
def test_the_mcp_host_paths_resolve_under_tmp_path(attr: str, tmp_path: Path) -> None:
    mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
    assert Path(getattr(mcp_handlers, attr)).is_relative_to(tmp_path)


def _run_python(tmp_path: Path, code: str) -> str:
    env_home = tmp_path / "kc-home"
    env_home.mkdir(exist_ok=True)
    # The whole environment is inherited, because Windows needs SYSTEMROOT and
    # friends to start the interpreter at all; only the home and data-home
    # variables are pinned under tmp_path. The child runs sys.executable by path
    # and spawns nothing, so the inherited PATH cannot reach a version-manager shim.
    env = {
        **os.environ,
        "KIROCREW_HOME": str(env_home),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONPATH": str(Path(onboarding_import.__file__).resolve().parents[1]),
        "KIROCREW_TELEMETRY": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


class TestImportTimeBehaviour:
    def test_importing_the_facade_keeps_the_deferred_imports_deferred(self, tmp_path) -> None:
        out = _run_python(
            tmp_path,
            "import json, sys\n"
            "import kiro_crew.onboarding_import\n"
            f"print(json.dumps([m for m in {list(_DEFERRED_MODULES)!r} if m in sys.modules]))\n",
        )
        assert json.loads(out.strip().splitlines()[-1]) == []

    @pytest.mark.parametrize("module", _ENGINE_MODULES)
    def test_every_engine_module_imports_first(self, tmp_path, module: str) -> None:
        """No import cycle: any module can be the process's first engine import."""
        out = _run_python(
            tmp_path,
            f"import {module}\n"
            "import kiro_crew.onboarding_import as facade\n"
            "print(facade.preview_import.__module__)\n",
        )
        assert out.strip().splitlines()[-1] == "kiro_crew.onboarding_import"

    def test_importing_mcp_cleanup_does_not_load_the_engine(self, tmp_path) -> None:
        out = _run_python(
            tmp_path,
            "import sys\n"
            "import kiro_crew.mcp_cleanup\n"
            "print('kiro_crew.onboarding_import' in sys.modules)\n",
        )
        assert out.strip().splitlines()[-1] == "False"


def test_the_claude_code_source_id_is_the_foreign_app_not_the_provider() -> None:
    """Every engine module keeps ``"claude_code"`` as a source id, never a provider check.

    ``test_agent_sdk_provider_identity`` reads only ``onboarding_import.py``; the
    Claude Code descriptor and adapter now live in ``onboarding_sources``, so the
    same rule is held over the whole engine here.
    """
    for path in map(_engine_source, _ENGINE_MODULES):
        assert "is_claude_code" not in path.read_text(encoding="utf-8"), path
    assert "claude_code" in onboarding_import._sources()


def test_the_facade_module_is_importable_through_importlib() -> None:
    assert importlib.import_module("kiro_crew.onboarding_import") is onboarding_import
