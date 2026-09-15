"""The suite must never resolve the operator's real data home, by any route.

Three barriers, tested here because each one alone has a hole the next one covers:

1. The rootdir ``KIROCREW_HOME`` pin uses a PRIVATE ``pytest.MonkeyPatch``, so a test
   calling ``undo()`` on the shared function-scoped instance cannot pop it.
2. ``conftest`` sets ``KIROCREW_HOME`` at IMPORT time when it is unset, so "unset" --
   the state that makes ``config.paths`` fall through to ``~/.kiro/crew`` -- is not a
   reachable state for any fixture, patched or not.
3. ``pytest_configure`` refuses the session outright when the variable names a real
   data home, which is the one case the other two cannot distinguish from intent.

What barrier 1 exists to stop, concretely: a test reverted its own patches with the
shared instance's ``undo()``, which also popped the home pin. ``resolve_store_path``
then resolved into the live home and the test wrote ``b"this is not a sqlite
database"`` over a real 36 MB ``memory.db`` that had no backup. Barriers 2 and 3 exist
because barrier 1 protects only the fixtures that remembered to use a private
instance, and "remember to" is the contract the rootdir conftest exists to delete.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from kiro_crew.config.loader import config_dir
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The homes the suite must refuse, stated HERE rather than imported from the rootdir
#: conftest. Reading its private tuple would make these tests agree with whatever that
#: list happens to say; naming the paths independently is what lets them catch an entry
#: being dropped from it. ~/.kirocrew is the deprecated location and still holds real
#: data on installs predating the move, so it is just as destructive a target.
_GUARDED_HOMES = (Path.home() / ".kiro" / "crew", Path.home() / ".kirocrew")


def _real_home() -> Path:
    return Path.home() / ".kiro" / "crew"


# ── Barrier 1: the pin survives a shared undo ────────────────────────────────


def test_undo_on_the_shared_monkeypatch_does_not_unpin_the_data_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = os.environ["KIROCREW_HOME"]
    assert Path(pinned).resolve() != _real_home().resolve()

    monkeypatch.setenv("KIROCREW_SOME_UNRELATED_VAR", "1")
    monkeypatch.undo()

    assert os.environ.get("KIROCREW_HOME") == pinned
    assert config_dir().resolve() != _real_home().resolve()
    assert str(config_dir().resolve()).startswith(str(Path(pinned).resolve()))


def test_a_test_can_still_override_the_pin_with_its_own_setenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The escape hatch stays open: a test that wants its own home gets it.

    Asserted because the private-instance fix would be easy to "harden" into a pin
    nothing can override, which would break every test that chooses its own home.
    """
    mine = tmp_path / "my-home"
    monkeypatch.setenv("KIROCREW_HOME", str(mine))
    import kiro_crew.config.paths as paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    assert config_dir().resolve() == mine.resolve()


def test_shared_undo_preserves_every_host_path_and_spawn_guard(monkeypatch) -> None:
    """Observe the live floor without ever opening an unpinned path or spawning.

    A missing pin must fail an assertion, never execute the operation whose guard
    is being tested. The extra patch makes undo nonempty even on an isolated run.
    """
    import asyncio.base_events

    from kiro_crew import agent_state, sandbox, subagent_persistence
    from kiro_crew.config import paths

    root = _load_root_conftest()
    targets = [
        (sys.modules[module], attr)
        for module, attr, _relative in root._SHARED_KIRO_PATHS
        if module in sys.modules
    ]
    targets += [
        (sys.modules[module], attr)
        for module, attr in root._AGENT_SPEC_HOOKS
        if module in sys.modules
    ]
    targets += [
        (paths, "_agents_dir_override"),
        (paths, "_sessions_dir_override"),
        (subagent_persistence, "_SUBAGENTS_DIR"),
        (agent_state, "config_dir"),
        (sandbox, "_mount_source_candidate_roots"),
        (sandbox, "_mount_pinned_source_names"),
        (sandbox, "_launcher_tmpfs_roots"),
        (sandbox, "_bound_source_basenames"),
        (subprocess.Popen, "__init__"),
        (asyncio.base_events.BaseEventLoop, "subprocess_exec"),
        (asyncio.base_events.BaseEventLoop, "subprocess_shell"),
        (os, "execve"),
    ]
    protected = [(module, attr, getattr(module, attr)) for module, attr in targets]
    env = {
        key: os.environ.get(key)
        for key in (
            "KIROCREW_HOME",
            "KIROCREW_WORKSPACE",
            "XDG_CONFIG_HOME",
            "KIRO_HOME",
            "KIROCREW_SKIP_MODEL_DOWNLOAD",
            "KIROCREW_TELEMETRY",
            "OLLAMA_MODELS",
        )
    }
    monkeypatch.setenv("KIROCREW_SOME_UNRELATED_VAR", "1")
    monkeypatch.undo()

    for module, attr, value in protected:
        assert getattr(module, attr) is value, f"shared undo removed {module.__name__}.{attr}"
    assert {key: os.environ.get(key) for key in env} == env


@pytest.mark.parametrize("undo_during_test", [False, True])
def test_host_floor_teardown_unwinds_test_overrides_before_its_own_pins(
    undo_during_test: bool,
) -> None:
    """Use inert sentinels to prove both stacks restore the pre-fixture value."""
    root = _load_root_conftest()
    target = SimpleNamespace(path="ambient")
    floor_cycle = root._floor_monkeypatch.__wrapped__()
    pin = next(floor_cycle)
    try:
        pin.setattr(target, "path", "isolated")
        test_cycle = root.monkeypatch.__wrapped__(pin)
        shared = next(test_cycle)
        try:
            shared.setattr(target, "path", "test-override")
            assert target.path == "test-override"
            if undo_during_test:
                shared.undo()
                assert target.path == "isolated"
                shared.setattr(target, "path", "second-override")
        finally:
            with pytest.raises(StopIteration):
                next(test_cycle)
        assert target.path == "isolated"
    finally:
        floor_cycle.close()
    assert target.path == "ambient"
    shared.undo()
    assert target.path == "ambient"


# ── Barrier 2: "unset" is not a reachable state ──────────────────────────────


def test_the_import_time_floor_leaves_no_unset_state_to_fall_back_from() -> None:
    """Importing ``conftest`` with the variable unset installs a scratch floor.

    This is the barrier that does not depend on any fixture. ``undo()`` restores the
    value the PROCESS started with, so giving the process a scratch value means the
    worst an undo can do is restore that -- never the empty state that
    ``config.paths._valid_override_home`` reads as "use ``~/.kiro/crew``".
    """
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import conftest, os\n"
        "print(os.environ['KIROCREW_HOME'])\n" % str(_REPO_ROOT)
    )
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_HOME"}
    # An inherited temp base may itself be the live Crew home. The import-time
    # floor must choose from its own platform candidates and never ask
    # tempfile's environment-sensitive default where to create the directory.
    env.update({key: str(_real_home()) for key in ("TMPDIR", "TEMP", "TMP")})
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        **UTF8_TEXT,
        cwd=str(_REPO_ROOT),
        env=env,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    floor = Path(out.stdout.strip())
    for real in _GUARDED_HOMES:
        resolved_floor = floor.resolve()
        resolved_real = real.resolve()
        assert resolved_floor != resolved_real
        assert resolved_real not in resolved_floor.parents
        assert resolved_floor not in resolved_real.parents


def test_an_operator_chosen_home_is_left_alone() -> None:
    """A deliberately exported scratch home is honoured, not replaced by the floor.

    The floor covers only the unset case. Overriding a value the operator chose would
    silently move where a debugging run writes.
    """
    chosen = "/tmp/kirocrew-operator-chosen-home"
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import conftest, os\n"
        "print(os.environ['KIROCREW_HOME'])\n" % str(_REPO_ROOT)
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        **UTF8_TEXT,
        cwd=str(_REPO_ROOT),
        env={**os.environ, "KIROCREW_HOME": chosen},
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == chosen


# ── Barrier 3: the session refuses a real data home ──────────────────────────


def _load_root_conftest():
    """The ROOTDIR conftest, by path.

    A bare ``import conftest`` from ``test/`` resolves ``test/conftest.py`` instead,
    which does not own this guard.
    """
    spec = importlib.util.spec_from_file_location("_root_conftest", _REPO_ROOT / "conftest.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "shape",
    ["exact", "parent", "home_itself", "child"],
)
def test_the_guard_refuses_every_shape_that_reaches_live_data(shape: str) -> None:
    """The guard is exercised IN-PROCESS, never by aiming a real session at live data.

    Deliberately not a subprocess. Spawning ``pytest`` with ``KIROCREW_HOME`` set to
    the operator's real home makes the guard under test the ONLY thing standing between
    the suite and live data, and it is already too late by then: the child loads the
    real ``config.json`` before ``pytest_configure`` runs, so the read side reaches
    live data on every run of the test. Calling the predicate directly proves the same
    property with nothing at stake.

    ``parent`` and ``home_itself`` are the shapes an equality check misses, and they are
    the dangerous ones: ``~/.kiro`` is kiro-cli's own home and resolves the whole live
    tree beneath it.
    """
    root = _load_root_conftest()
    live = _GUARDED_HOMES[0]
    target = {
        "exact": live,
        "parent": live.parent,
        "home_itself": Path.home(),
        "child": live / "workspace",
    }[shape]
    with mock.patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
        with pytest.raises(pytest.UsageError, match="live data home"):
            root._refuse_a_real_data_home()


def test_the_guard_accepts_a_scratch_home(tmp_path: Path) -> None:
    """The refusal must not be so broad that no session can run at all."""
    root = _load_root_conftest()
    with mock.patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path / "scratch")}):
        root._refuse_a_real_data_home()


def test_the_deprecated_home_is_guarded_too() -> None:
    """``~/.kirocrew`` predates the move and still holds real data on old installs."""
    root = _load_root_conftest()
    with mock.patch.dict(os.environ, {"KIROCREW_HOME": str(_GUARDED_HOMES[1])}):
        with pytest.raises(pytest.UsageError, match="live data home"):
            root._refuse_a_real_data_home()


def test_the_guarded_homes_match_what_the_product_resolves() -> None:
    """The conftest's literals must name the homes the product actually uses.

    The guard cannot import ``kiro_crew`` (the rootdir conftest is imported before
    every collection), so it spells the two paths literally. This is the cross-check
    that keeps them honest: a future data-home move would otherwise update
    ``config.paths`` and leave a fail-open guard naming a directory nobody uses.
    """
    root = _load_root_conftest()
    from kiro_crew.config import paths

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KIROCREW_HOME", None)
        paths._resolved_home = None
        default_home = paths._default_home()
    assert default_home.resolve() in {p.resolve() for p in root._REAL_DATA_HOMES}
    assert set(root._REAL_DATA_HOMES) == set(_GUARDED_HOMES)


def test_the_guard_is_reached_from_the_one_pytest_configure() -> None:
    """A duplicate ``pytest_configure`` would silently replace the earlier one.

    The guard is a plain function called from the single hook rather than a second hook
    of that name, and this pins that: a module-level redefinition is not an error, so
    nothing else would report a guard that stopped running.
    """
    source = (_REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    assert source.count("\ndef pytest_configure(") == 1
    assert "_refuse_a_real_data_home()" in source
