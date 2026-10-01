"""A ``file:`` / ``npm link`` adapter is accepted the way Node would run it.

npm installs a ``file:`` or ``npm link`` dependency as a SYMLINK inside the
project's ``node_modules`` and hoists nothing for it: the linked package's own
dependencies live under the link target's ``node_modules``. A completeness check
that looks for the adapter's dependency marker at the hoisted root alone rejects
every such adapter as incomplete, and the resolution ladder falls through -- with
nothing logged -- to whatever copy sits on PATH, so a locally patched adapter
runs as the unpatched global build. Node resolves a bare import from a symlinked
package by walking ``node_modules`` directories upward from the module's REAL
path; the one helper shared by the three Node ACP adapter resolvers
(claude-agent-acp, codex-acp, pi-acp) does the same, refuses an entry whose
dependency is reachable nowhere, and logs why when it skips a project-local copy.

Every case runs each of the three resolvers end to end with the other rungs of
the ladder stubbed, so the assertion is about what would actually be spawned.
Directory links are made with ``make_dir_link`` -- a junction on an unelevated
Windows runner -- so the Windows job keeps these assertions instead of skipping
them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

import kiro_crew.acp.client as acp_client
from conftest import cap_node_module_walk, make_dir_link
from kiro_crew.agent_sdk.backends import NODE_ADAPTER_ENTRY_SEGMENTS

_LOGGER = "kiro_crew.acp.client"


@dataclass(frozen=True)
class _Adapter:
    """One Node ACP adapter as its resolver sees it."""

    npm_pkg: str
    bin_name: str
    pkg_entry: Path
    dep_marker: Path
    resolve: Callable[[], tuple[list[str] | None, str]]


_ADAPTERS = [
    pytest.param(
        _Adapter(
            acp_client.CLAUDE_ACP_NPM_PKG,
            acp_client.CLAUDE_ACP_BIN,
            acp_client._CLAUDE_ACP_PKG_ENTRY,
            acp_client._CLAUDE_ACP_DEP_MARKER,
            acp_client._resolve_claude_acp_bin,
        ),
        id="claude-agent-acp",
    ),
    pytest.param(
        _Adapter(
            acp_client.CODEX_ACP_NPM_PKG,
            acp_client.CODEX_ACP_BIN,
            acp_client._CODEX_ACP_PKG_ENTRY,
            acp_client._CODEX_ACP_DEP_MARKER,
            acp_client._resolve_codex_acp_bin,
        ),
        id="codex-acp",
    ),
    pytest.param(
        _Adapter(
            acp_client.PI_ACP_NPM_PKG,
            acp_client.PI_ACP_BIN,
            acp_client._PI_ACP_PKG_ENTRY,
            acp_client._PI_ACP_DEP_MARKER,
            acp_client._resolve_pi_acp_bin,
        ),
        id="pi-acp",
    ),
]

# The override env vars of the three adapters: a host that sets one would bypass the
# rung under test, so every case clears them all.
_OVERRIDE_ENV_VARS = ("CLAUDE_AGENT_ACP_BIN", "CODEX_ACP_BIN", "PI_ACP_BIN")


def _hoisted_layout(root: Path, adapter: _Adapter) -> Path:
    """An ordinary ``npm install``: the adapter and its dependency hoisted flat."""
    entry = root / adapter.pkg_entry
    entry.parent.mkdir(parents=True)
    entry.write_text("// adapter\n", encoding="utf-8")
    (root / adapter.dep_marker).mkdir(parents=True)
    return entry


def _linked_layout(tmp_path: Path, root: Path, adapter: _Adapter, *, dep_at: str | None) -> Path:
    """A ``file:`` / ``npm link`` install, as npm lays it out.

    The package directory under *root* (the consumer's ``node_modules``) is a
    directory link to a checkout at ``<tmp>/checkout/packages/<pkg>``; the root
    itself hoists nothing for it. *dep_at* places the dependency marker:

    - ``"target"``: under the link target's own ``node_modules`` (a plain
      ``npm install`` inside the linked checkout);
    - ``"target_parent"``: hoisted at the checkout's root ``node_modules`` (a
      monorepo workspace), which only a walk from the entry's REAL path reaches;
    - ``"consumer_root"``: under *root* only, which Node never searches for a
      symlinked package -- only a walk from the lexical link path would find it;
    - ``None``: nowhere.

    Returns the entry script's path INSIDE THE TARGET -- the real path Node runs.
    """
    checkout = tmp_path / "checkout"
    target = checkout / "packages" / Path(adapter.npm_pkg).name
    target_entry = target.joinpath(*NODE_ADAPTER_ENTRY_SEGMENTS)
    target_entry.parent.mkdir(parents=True)
    target_entry.write_text("// locally patched adapter\n", encoding="utf-8")
    dep_root = {
        "target": target / "node_modules",
        "target_parent": checkout / "node_modules",
        "consumer_root": root,
        None: None,
    }[dep_at]
    if dep_root is not None:
        (dep_root / adapter.dep_marker).mkdir(parents=True)
    link = root / adapter.npm_pkg
    link.parent.mkdir(parents=True, exist_ok=True)
    make_dir_link(link, target)
    return target_entry


def _isolate_ladder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter: _Adapter,
    *,
    root: Path,
    on_path: Path | None,
) -> str:
    """Pin every rung of the ladder except the project-local one.

    The project-local rung reads *root* only; mise answers nothing; PATH holds
    *on_path* (the global copy of this adapter) or nothing. Returns the fake
    ``node`` every script is wrapped with.
    """
    node = str(tmp_path / "node")
    for var in _OVERRIDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(acp_client, "_vendored_acp_roots", lambda *_a, **_kw: [root])
    monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
    monkeypatch.setattr(acp_client, "_mise_node_installs_dir", lambda: tmp_path / "no-mise")
    monkeypatch.setattr(acp_client, "_resolve_node_for_script", lambda _script: node)

    def _which(name: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        if on_path is not None and name == adapter.bin_name:
            return str(on_path)
        return node if name == "node" else None

    monkeypatch.setattr(acp_client.shutil, "which", _which)
    return node


def _global_copy(tmp_path: Path, adapter: _Adapter) -> Path:
    """The unpatched global build of the adapter, as ``npm i -g`` puts it on PATH."""
    copy = tmp_path / "global" / "bin" / adapter.bin_name
    copy.parent.mkdir(parents=True)
    copy.write_text("// global build\n", encoding="utf-8")
    return copy


def _skip_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "project-local" in record.getMessage()
    ]


class TestALinkedAdapterResolvesLikeNodeRunsIt:
    """The issue's shape: a ``file:`` install with its dependencies beside the target."""

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_the_linked_adapter_wins_over_the_global_copy_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: _Adapter
    ) -> None:
        root = tmp_path / "proj" / "node_modules"
        target_entry = _linked_layout(tmp_path, root, adapter, dep_at="target")
        global_copy = _global_copy(tmp_path, adapter)
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=global_copy)

        argv, _searched = adapter.resolve()

        # The spawn is the locally installed adapter at its real path -- NOT the
        # unpatched copy on PATH, which a hoisted-root-only check falls through to.
        assert argv == [node, str(target_entry.resolve())]
        assert str(global_copy.resolve()) not in argv

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_the_linked_adapter_is_the_only_copy_and_sessions_can_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: _Adapter
    ) -> None:
        # The issue reporter's "remove the global copy" step: with nothing on PATH, a
        # hoisted-root-only check leaves the harness with no adapter at all.
        root = tmp_path / "proj" / "node_modules"
        target_entry = _linked_layout(tmp_path, root, adapter, dep_at="target")
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=None)

        argv, _searched = adapter.resolve()

        assert argv == [node, str(target_entry.resolve())]

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_a_workspace_dependency_above_the_link_target_is_reached_from_the_real_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: _Adapter
    ) -> None:
        # A monorepo checkout hoists the dependency at ITS root, above the linked
        # package. Node reaches it because it walks up from the real path; a walk
        # from the lexical link path under the consumer's node_modules never does.
        # This is the case that pins the realpath step, not just the walk.
        root = tmp_path / "proj" / "node_modules"
        target_entry = _linked_layout(tmp_path, root, adapter, dep_at="target_parent")
        global_copy = _global_copy(tmp_path, adapter)
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=global_copy)

        argv, _searched = adapter.resolve()

        assert argv == [node, str(target_entry.resolve())]


class TestAnEntryWithNoReachableDependencyIsStillRefused:
    """The completeness guard keeps its teeth: nothing Node could not import is spawned."""

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_a_link_target_without_the_dependency_falls_through_and_says_why(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        adapter: _Adapter,
    ) -> None:
        root = tmp_path / "proj" / "node_modules"
        target_entry = _linked_layout(tmp_path, root, adapter, dep_at=None)
        global_copy = _global_copy(tmp_path, adapter)
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=global_copy)
        # The walk up from the link target must not find a node_modules the HOST keeps
        # above the temp root; what sits there is not this test's to assert on.
        cap_node_module_walk(monkeypatch, tmp_path)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            argv, _searched = adapter.resolve()

        # Refused: the global copy is what runs...
        assert argv == [node, str(global_copy.resolve())]
        assert str(target_entry.resolve()) not in argv
        # ...and the skip is on record. ONE warning names the skipped entry and the
        # dependency Node could not have imported from it.
        warnings = _skip_warnings(caplog)
        assert len(warnings) == 1, caplog.text
        message = warnings[0].getMessage()
        assert str(root / adapter.pkg_entry) in message
        assert adapter.dep_marker.as_posix() in message

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_a_dependency_hoisted_only_in_the_consumer_root_does_not_rescue_a_linked_adapter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        adapter: _Adapter,
    ) -> None:
        # Node resolves a symlinked package's imports from its REAL path, so a copy
        # of the dependency in the consumer's own node_modules is invisible to it
        # and the adapter would die at ESM import. A walk from the lexical link path
        # would find that copy and accept the adapter; the real-path walk must not.
        root = tmp_path / "proj" / "node_modules"
        target_entry = _linked_layout(tmp_path, root, adapter, dep_at="consumer_root")
        global_copy = _global_copy(tmp_path, adapter)
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=global_copy)
        cap_node_module_walk(monkeypatch, tmp_path)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            argv, _searched = adapter.resolve()

        assert argv == [node, str(global_copy.resolve())]
        warnings = _skip_warnings(caplog)
        assert len(warnings) == 1, caplog.text
        # The warning names where the entry REALLY lives, which is where Node looked.
        assert str(target_entry.resolve().parent) in warnings[0].getMessage()

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_an_incomplete_hoisted_copy_is_refused_and_says_why(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        adapter: _Adapter,
    ) -> None:
        # The hoisted guard case (entry script, no hoisted dependency): a
        # fall-through to PATH, with the reason on record.
        root = tmp_path / "proj" / "node_modules"
        entry = root / adapter.pkg_entry
        entry.parent.mkdir(parents=True)
        entry.write_text("// adapter without its deps\n", encoding="utf-8")
        global_copy = _global_copy(tmp_path, adapter)
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=global_copy)
        cap_node_module_walk(monkeypatch, tmp_path)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            argv, _searched = adapter.resolve()

        assert argv == [node, str(global_copy.resolve())]
        warnings = _skip_warnings(caplog)
        assert len(warnings) == 1, caplog.text
        assert str(entry) in warnings[0].getMessage()


class TestAnOrdinaryHoistedInstallIsUnchanged:
    """``npm install`` without links: the hoisted root satisfies the walk, quietly."""

    @pytest.mark.parametrize("adapter", _ADAPTERS)
    def test_the_hoisted_adapter_is_accepted_without_a_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        adapter: _Adapter,
    ) -> None:
        root = tmp_path / "proj" / "node_modules"
        entry = _hoisted_layout(root, adapter)
        global_copy = _global_copy(tmp_path, adapter)
        node = _isolate_ladder(monkeypatch, tmp_path, adapter, root=root, on_path=global_copy)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            argv, _searched = adapter.resolve()

        assert argv == [node, str(entry.resolve())]
        assert _skip_warnings(caplog) == []


class TestTheThreeResolversShareOneHelper:
    """A fourth copy of the completeness check cannot drift from the other three."""

    def test_every_adapter_resolver_reaches_the_project_local_rung_through_the_one_helper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[Path, Path]] = []

        def _record(pkg_entry: Path, dep_marker: Path, pkg_dir: Path | None = None) -> None:
            seen.append((pkg_entry, dep_marker))
            return None

        monkeypatch.setattr(acp_client, "_vendored_adapter_entry", _record)
        for param in _ADAPTERS:
            adapter = param.values[0]
            _isolate_ladder(monkeypatch, tmp_path, adapter, root=tmp_path / "unused", on_path=None)
            argv, _searched = adapter.resolve()
            assert argv is None

        assert seen == [
            (param.values[0].pkg_entry, param.values[0].dep_marker) for param in _ADAPTERS
        ]
