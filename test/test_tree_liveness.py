"""Unit tests for ``platform/tree_liveness``: the liveness hold and the stable-link rewrite.

The engine-level behaviour (a prune never deletes a held tree) is pinned in
``test_wheel_engine_lifecycle.py``; these tests pin the entry-point helper's own
branches, which every ``kirocrew`` process runs at start.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.platform import tree_liveness
from kiro_crew.platform_compat import IS_POSIX, try_acquire_lock


def _tree(tmp_path: Path, *, lock: bool = True) -> Path:
    tree = tmp_path / "crew-venv-1.0"
    (tree / "bin").mkdir(parents=True)
    (tree / tree_liveness.TREE_MARKER).write_text("layout\n", encoding="utf-8")
    if lock:
        (tree / tree_liveness.LIVENESS_LOCK).touch()
    return tree


@pytest.fixture(autouse=True)
def _no_held_fd(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tree_liveness, "_HELD_FD", None)
    yield
    held = tree_liveness._HELD_FD
    if held is not None:
        os.close(held)


class TestRunningTree:
    def test_names_a_marked_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tree = _tree(tmp_path)
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tree))
        assert tree_liveness.running_tree() == tree.resolve()

    def test_an_unmarked_prefix_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tmp_path))
        assert tree_liveness.running_tree() is None

    def test_an_unreadable_prefix_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_path: str) -> str:
            raise OSError("gone")

        monkeypatch.setattr(tree_liveness.os.path, "realpath", _boom)
        assert tree_liveness.running_tree() is None


@pytest.mark.skipif(not IS_POSIX, reason="the liveness hold is POSIX-only")
class TestHoldRunningTreeLock:
    def test_holds_a_shared_lock_that_blocks_an_exclusive_taker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = _tree(tmp_path)
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tree))
        tree_liveness.hold_running_tree_lock()
        assert tree_liveness._HELD_FD is not None
        prober = os.open(str(tree / tree_liveness.LIVENESS_LOCK), os.O_RDWR)
        try:
            assert not try_acquire_lock(prober, exclusive=True), "the prune must see it held"
        finally:
            os.close(prober)

    def test_a_second_call_keeps_the_first_hold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = _tree(tmp_path)
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tree))
        tree_liveness.hold_running_tree_lock()
        first = tree_liveness._HELD_FD
        tree_liveness.hold_running_tree_lock()
        assert tree_liveness._HELD_FD == first

    def test_off_an_engine_tree_takes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tmp_path))
        tree_liveness.hold_running_tree_lock()
        assert tree_liveness._HELD_FD is None

    def test_a_missing_lock_file_leaves_the_tree_unmarked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = _tree(tmp_path, lock=False)
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tree))
        tree_liveness.hold_running_tree_lock()
        assert tree_liveness._HELD_FD is None

    def test_a_refused_lock_closes_its_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = _tree(tmp_path)
        monkeypatch.setattr(tree_liveness.sys, "prefix", str(tree))
        opened: list[int] = []
        real_open = os.open

        def _record(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path).endswith(tree_liveness.LIVENESS_LOCK):
                opened.append(fd)
            return fd

        closed: list[int] = []
        real_close = os.close

        def _close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(tree_liveness.os, "open", _record)
        monkeypatch.setattr(tree_liveness.os, "close", _close)
        monkeypatch.setattr(tree_liveness, "try_acquire_lock", lambda _fd, exclusive: False)
        tree_liveness.hold_running_tree_lock()
        assert tree_liveness._HELD_FD is None
        assert len(opened) == 1
        assert opened[0] in closed, "a refused hold must not leak its descriptor"


class TestThroughStableLink:
    def test_a_path_inside_an_engine_tree_goes_through_the_stable_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = _tree(tmp_path)
        exe = tree / "bin" / "kirocrew"
        exe.write_text("", encoding="utf-8")
        seen: list[str] = []

        def _stable(path: str) -> str:
            seen.append(path)
            return "/stable/bin/kirocrew"

        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "stable_launch_path", _stable)
        assert tree_liveness.through_stable_link(str(exe)) == "/stable/bin/kirocrew"
        assert seen == [str(exe)]

    def test_a_path_outside_any_engine_tree_is_unchanged(self, tmp_path: Path) -> None:
        plain = tmp_path / "bin" / "kirocrew"
        plain.parent.mkdir(parents=True)
        plain.write_text("", encoding="utf-8")
        assert tree_liveness.through_stable_link(str(plain)) == str(plain)

    def test_an_unresolvable_path_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_path: str) -> str:
            raise ValueError("embedded null byte")

        monkeypatch.setattr(tree_liveness.os.path, "realpath", _boom)
        assert tree_liveness.through_stable_link("/x/bin/kirocrew") == "/x/bin/kirocrew"

    def test_an_unreadable_parent_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_self: Path) -> bool:
            raise OSError("permission denied")

        monkeypatch.setattr(tree_liveness.Path, "is_file", _boom)
        target = str(tmp_path / "bin" / "kirocrew")
        assert tree_liveness.through_stable_link(target) == target


class TestEntryPointLiveness:
    @pytest.mark.parametrize("command", [["gateway"], ["mcp-core"], ["cron", "list"]])
    def test_only_non_gateway_commands_hold_the_tree_in_cli_main(
        self, command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from kiro_crew import cli

        hold = MagicMock()
        monkeypatch.setattr(tree_liveness, "hold_running_tree_lock", hold)
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", *command])
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        monkeypatch.setattr(cli, "maybe_reexec", lambda _argv: None)

        def stop_before_dispatch() -> None:
            raise RuntimeError("stop before command dispatch")

        monkeypatch.setattr(cli, "ensure_data_home", stop_before_dispatch)
        with pytest.raises(RuntimeError, match="stop before command dispatch"):
            cli.main()
        if command[0] == "gateway":
            hold.assert_not_called()
        else:
            hold.assert_called_once_with()

    def test_gateway_holds_off_loop_after_readiness_before_update_work(self) -> None:
        import ast
        import inspect
        import textwrap

        from kiro_crew.slack.gateway import GatewayOrchestrator

        source = textwrap.dedent(inspect.getsource(GatewayOrchestrator.run))
        tree = ast.parse(source)
        holds = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and ast.unparse(node.value.func) == "asyncio.to_thread"
            and node.value.args
            and ast.unparse(node.value.args[0]) == "hold_running_tree_lock"
        ]
        assert len(holds) == 1, "the gateway must take its tree hold once, off-loop"
        hold = holds[0]
        ready = source.index('print(f"KIROCREW_READY:')
        approval = source.index("approval_ready.set()")
        imported = source.index(
            "from kiro_crew.platform.tree_liveness import hold_running_tree_lock"
        )
        held = source.index("await asyncio.to_thread(hold_running_tree_lock)")
        signals = source.index("self._install_shutdown_signal_handlers()")
        updates = source.index("asyncio.create_task(self._run_update_checks())")
        assert ready < approval < imported < held < signals < updates
        guarded = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try) and hold in list(ast.walk(node))
        )
        assert any(
            handler.type is not None
            and ast.unparse(handler.type) == "Exception"
            and any(
                isinstance(node, ast.Call) and ast.unparse(node.func) == "logger.debug"
                for node in ast.walk(handler)
            )
            for handler in guarded.handlers
        ), "a failed tree hold must only log at debug and leave startup available"
