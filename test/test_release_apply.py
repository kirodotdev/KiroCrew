"""``release_apply.apply_release``: an update installs the release, code and KAS engine.

What is pinned:

* the git route installs the code first and runs the engine step only when the
  code landed; an engine failure is its own return code, distinct from a pip
  failure and a refusal, and names ``kirocrew kas install``;
* the wheel route hands the wheel engine an engine step that runs the new tree's
  own interpreter with the apply's build context;
* the engine step itself runs ``<new python> -I -P -m kiro_crew kas install
  --if-in-use`` through the update's one child spawn, and a nonzero exit is an
  :class:`EngineInstallError` carrying the child's redacted reason;
* nothing else in an update path runs the package install or the wheel apply: the
  update routes reach them only through ``release_apply``.

The suite-wide ``release_engine_installs`` fixture stubs the engine step; the tests
of the step itself hold the real function, imported at collection time.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from kiro_crew import dep_sync
from kiro_crew.platform import release_apply, wheel_engine
from kiro_crew.platform.release_apply import _install_engine as _real_install_engine
from kiro_crew.platform.wheel_engine_contract import WheelUpdateError, _BuildContext
from kiro_crew.testing.wait import await_until

_PY = Path("/venv/bin/python")


def _git(messages: list[tuple[str, bool]], **over) -> release_apply.GitRelease:
    return release_apply.GitRelease(
        Path("/repo"), _PY, lambda m, e: messages.append((m, e)), **over
    )


class TestGitRelease:
    @pytest.mark.parametrize("rc", [1, dep_sync.REFUSED])
    def test_a_failed_package_install_never_reaches_the_engine(
        self, monkeypatch, release_engine_installs, rc
    ):
        monkeypatch.setattr(dep_sync, "sync_or_reinstall", lambda *a, **k: rc)
        assert release_apply.apply_release(_git([])) == rc
        assert release_engine_installs == []

    def test_the_engine_runs_on_the_target_interpreter_after_the_code(
        self, monkeypatch, release_engine_installs
    ):
        order: list[str] = []

        def install(*_a, **_k):
            order.append("code")
            return 0

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", install)
        messages: list[tuple[str, bool]] = []
        assert release_apply.apply_release(_git(messages, trusted_env=False)) == 0
        assert order == ["code"]
        assert release_engine_installs == [(_PY, False)]
        assert (release_apply.ENGINE_STEP_MESSAGE, False) in messages

    @pytest.mark.parametrize(("trusted_path", "expected"), [("/usr/bin:/bin", True), (None, False)])
    def test_a_host_without_a_trusted_path_keeps_the_gateway_environment(
        self, monkeypatch, release_engine_installs, trusted_path, expected
    ):
        """Windows has no trusted system PATH; its git route must not fail closed on it."""
        monkeypatch.setattr(dep_sync, "sync_or_reinstall", lambda *a, **k: 0)
        monkeypatch.setattr(release_apply, "trusted_system_path", lambda: trusted_path)
        assert release_apply.apply_release(_git([])) == 0
        assert release_engine_installs == [(_PY, expected)]

    def test_an_engine_failure_is_engine_failed_and_names_the_install(self, monkeypatch):
        monkeypatch.setattr(dep_sync, "sync_or_reinstall", lambda *a, **k: 0)

        def fail(_python, _ctx):
            raise release_apply.EngineInstallError("the new version's KAS engine install exited 1")

        monkeypatch.setattr(release_apply, "_install_engine", fail)
        messages: list[tuple[str, bool]] = []
        rc = release_apply.apply_release(_git(messages))
        assert rc == release_apply.ENGINE_FAILED
        assert rc not in (0, 1, dep_sync.REFUSED)
        errors = [m for m, error in messages if error]
        assert len(errors) == 1
        assert "keeps running" in errors[0]
        assert "kirocrew kas install" in errors[0]

    def test_the_release_cancel_reaches_the_package_install(self, monkeypatch):
        seen: list[bool] = []

        def install(*_a, cancel, **_k):
            seen.append(cancel.is_set())
            return dep_sync.REFUSED

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", install)
        stop = wheel_engine.ApplyCancel()
        stop.set("shutdown")
        assert release_apply.apply_release(_git([], cancel=stop)) == dep_sync.REFUSED
        assert seen == [True]


class TestSupervised:
    @pytest.mark.asyncio
    async def test_a_cancel_sets_the_release_cancel_off_the_event_loop(self, monkeypatch):
        """Setting the cancel kills the engine child, which blocks on Windows."""
        loop_thread = threading.current_thread()
        set_on: list[threading.Thread] = []

        class _Recording(wheel_engine.ApplyCancel):
            def set(self, reason: str = "cancel") -> None:
                set_on.append(threading.current_thread())
                super().set(reason)

        started = asyncio.Event()
        release_install = threading.Event()
        loop = asyncio.get_running_loop()

        def install(*_a, **_k):
            loop.call_soon_threadsafe(started.set)
            # Released by the test's `finally`; the bound only keeps a failed test
            # from pinning the executor thread for good.
            release_install.wait(timeout=120)
            return dep_sync.REFUSED

        monkeypatch.setattr(release_apply, "ApplyCancel", _Recording)
        monkeypatch.setattr(dep_sync, "sync_or_reinstall", install)
        task = asyncio.create_task(release_apply.apply_release_supervised(None, _git([])))
        try:
            await await_until(started, what="the install starting")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release_install.set()
        assert len(set_on) == 1
        assert set_on[0] is not loop_thread


class TestWheelRelease:
    def test_the_wheel_engine_gets_an_engine_step_on_the_new_tree(self, monkeypatch, tmp_path):
        passed: dict = {}

        def apply(**kwargs):
            passed.update(kwargs)
            return tmp_path / "crew-venv-9.9.9"

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", apply)
        engine_calls: list[tuple[Path, _BuildContext]] = []
        monkeypatch.setattr(
            release_apply, "_install_engine", lambda py, ctx: engine_calls.append((py, ctx))
        )
        progress: list[str] = []
        release = release_apply.WheelRelease(
            channel="stable",
            feed_base="https://f",
            artifact_base="https://a",
            expected_version="9.9.9",
            progress=progress.append,
            trusted_env=False,
        )
        assert release_apply.apply_release(release) == tmp_path / "crew-venv-9.9.9"
        assert passed["expected_version"] == "9.9.9"
        assert passed["trusted_env"] is False
        ctx = _BuildContext(trusted_env=False)
        passed["install_engine"](tmp_path / "crew-venv-9.9.9", ctx)
        assert engine_calls == [(tmp_path / "crew-venv-9.9.9" / "bin" / "python3", ctx)]
        assert progress == [release_apply.ENGINE_STEP_MESSAGE]


class TestEngineStep:
    def test_runs_the_new_release_install_through_the_update_spawn(self, monkeypatch):
        spawned: list = []

        def spawn(argv, timeout, step, *, ctx, env_overrides):
            spawned.append((argv, timeout, step, ctx))
            return 0, b"", b""

        monkeypatch.setattr(release_apply, "_spawn_build_child", spawn)
        ctx = _BuildContext(trusted_env=True)
        _real_install_engine(_PY, ctx)
        assert spawned == [
            (
                [str(_PY), "-I", "-P", "-m", "kiro_crew", "kas", "install", "--if-in-use"],
                release_apply.ENGINE_INSTALL_TIMEOUT_SECS,
                "the KAS engine install",
                ctx,
            )
        ]

    def test_a_nonzero_exit_raises_with_the_childs_reason(self, monkeypatch):
        monkeypatch.setattr(
            release_apply,
            "_spawn_build_child",
            lambda *a, **k: (1, b"", b"node did not run: exit 127\n"),
        )
        with pytest.raises(
            release_apply.EngineInstallError, match="exited 1: node did not run: exit 127"
        ):
            _real_install_engine(_PY, _BuildContext())

    def test_a_relative_data_home_reaches_the_child_resolved(self, monkeypatch, tmp_path):
        """The child runs in ``/``; a relative override must not move its data home."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KIROCREW_HOME", "crew-data")
        seen: list = []

        def spawn(argv, timeout, step, *, ctx, env_overrides):
            seen.append(env_overrides)
            return 0, b"", b""

        monkeypatch.setattr(release_apply, "_spawn_build_child", spawn)
        _real_install_engine(_PY, _BuildContext())
        home = seen[0]["KIROCREW_HOME"]
        assert Path(home).is_absolute()
        # The data home this process resolved, not the raw relative spelling.
        assert Path(home) == Path(release_apply.data_home()).resolve()
        assert home != "crew-data"

    def test_no_override_pins_nothing(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        assert release_apply._data_home_env() == {}

    @pytest.mark.parametrize(
        "usage",
        [
            b"kirocrew: error: argument command: invalid choice: 'kas' (choose from ...)\n",
            b"kirocrew: error: unrecognized arguments: --if-in-use\n",
        ],
        ids=["no-kas-command", "no-flag"],
    )
    def test_a_target_release_that_predates_the_step_is_nothing_to_install(
        self, monkeypatch, usage
    ):
        """A channel move or rollback to an older build must not fail on the flag."""
        monkeypatch.setattr(release_apply, "_spawn_build_child", lambda *a, **k: (2, b"", usage))
        _real_install_engine(_PY, _BuildContext())

    def test_another_usage_error_still_fails(self, monkeypatch):
        monkeypatch.setattr(
            release_apply,
            "_spawn_build_child",
            lambda *a, **k: (2, b"", b"kirocrew: error: unrecognized arguments: --bogus\n"),
        )
        with pytest.raises(release_apply.EngineInstallError, match="exited 2"):
            _real_install_engine(_PY, _BuildContext())

    def test_an_engine_failure_is_a_wheel_update_failure(self):
        """So the wheel routes classify it as ``failed``: nothing was promoted."""
        assert issubclass(release_apply.EngineInstallError, WheelUpdateError)


def test_the_module_does_not_load_the_wheel_engine_on_import():
    """The gateway imports this at boot; the managed-venv engine stays off that path."""
    src = Path(release_apply.__file__)
    tree = ast.parse(src.read_text(encoding="utf-8"))
    top_level = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and any(
            alias.name == "wheel_engine" or (node.module or "").endswith(".wheel_engine")
            for alias in node.names
        )
    ]
    assert top_level == []


#: The update routes. Each reaches the package install and the wheel apply only
#: through ``release_apply``; adding a route means adding it here.
_UPDATE_ROUTES = (
    "cli_server_update.py",
    "dashboard/handlers/updates_apply.py",
    "dashboard/handlers/updates_step_up.py",
    "platform/wheel_apply.py",
    "slack/gateway_runtime/update_apply.py",
)
_INSTALL_CALLS = frozenset(
    {"sync_or_reinstall", "sync_or_reinstall_supervised", "apply_wheel_update"}
)


@pytest.mark.parametrize("rel", _UPDATE_ROUTES)
def test_no_update_route_installs_code_except_through_release_apply(rel):
    src = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / rel
    tree = ast.parse(src.read_text(encoding="utf-8"))
    direct = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in _INSTALL_CALLS)
            or (isinstance(node.func, ast.Name) and node.func.id in _INSTALL_CALLS)
        )
    )
    assert direct == [], f"{rel} installs code outside release_apply at lines {direct}"
