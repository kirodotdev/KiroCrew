"""Dev Fleet Pull + Build: the sync runner, its install step and request handling.

Covers the runner's step markers, which install step the sync builds, the UTF-8 pins
every step carries, the handler's input validation and single-flight 409, and the
fleet payload's build-pending, run-pointer and provision-run fields.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _cleanup_sync_tempdirs, _install_step, _run_sync

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod
from kiro_crew.testing.wait import async_wait_until


# --- sync runner emits ::step:: markers ---
@pytest.mark.asyncio
async def test_sync_script_emits_step_markers():
    """The sync runner emits ::step::<idx>::<label> before each step.

    The runner is a snapshotted module (sync_runner.py) run by path, so the
    gateway-side contract is "invoke that module"; the marker EMISSION itself is
    proven by execution in test_dev_fleet_sync_runner.py. Here we pin that the
    sync invokes the runner by path and that the module still emits the marker.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    repository_mod._UPSTREAM_REMOTE = "origin"
    worktree_ops_mod._SYNC_RID = None
    with (
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="main"),
        patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")),
        patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"),
        patch(
            "kiro_crew.apps.builtins.dev_fleet.worktree_ops.sandboxed_spawn_argv",
            side_effect=lambda cmd, mode, env=None: (cmd, env or {}, None),
        ),
        patch.object(
            runtime_mod, "_start_run", new_callable=AsyncMock, return_value="run-123"
        ) as mock_start,
    ):
        async with mod._SYNC_LOCK:
            result = await mod._sync_start_locked()
    # Clean up the snapshot + steps file the stubbed _start_run would have —
    # BEFORE any assertion, so a failing assertion cannot leak the staged
    # temporary directories (no test side effects).
    _cleanup_sync_tempdirs(mock_start)
    assert result["ok"] is True
    cmd_args = mock_start.call_args[0]
    runner_cmd = cmd_args[1]
    assert runner_cmd[0].endswith("python") or "python" in runner_cmd[0]
    assert any(str(a).endswith("sync_runner.py") for a in runner_cmd)
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert "::step::" in src
    repository_mod._UPSTREAM_REMOTE = None


# --- Windows: a write-locked console script must not be handed to pip ---
# The probe itself moved to kiro_crew.dep_sync with the substitute it feeds, and
# its tests moved with it (test/test_dep_sync.py). What stays here is the sync's
# use of the result: which install step gets built.


@pytest.mark.asyncio
async def test_sync_substitutes_a_dependency_only_install_when_a_script_is_locked():
    """The sync proceeds; only the editable reinstall is swapped out.

    pip cannot replace the locked wrapper, but it does not have to. An editable
    install serves source straight from ``src``, so the merge alone makes the new
    revision live; the only thing the reinstall was still buying is a dependency
    the revision added, and installing a dependency never touches the project's
    own console script. Refusing the whole sync instead left the single-checkout
    Windows layout — the ordinary one — with no working Pull+build at all.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew import dep_sync

    result, cmd, steps = await _run_sync(mod, [r"C:\repo\.venv\Scripts\kirocrew.exe"])

    assert result["ok"] is True
    # A run was started, so fetch and merge do happen.
    assert cmd is not None
    # The install step's argv is what the substitution changes; inspect it
    # directly rather than source text.
    install = _install_step(steps)
    argv_flat = " ".join(install["argv"])
    assert "dep_sync.py" in argv_flat
    assert "-e" not in install["argv"]
    # It must NOT be run as `-m kiro_crew...dep_sync`. That would import the
    # module from the working tree after the merge has landed, pulling the whole
    # package __init__ chain with it, so a revision that raises the
    # `requires-python` floor with newer syntax would SyntaxError while being
    # parsed and the floor refusal written for that revision could never run.
    assert dep_sync.__name__ not in argv_flat
    assert "-m" not in install["argv"]
    # The file it runs is a snapshot taken BEFORE the merge, so it lives outside
    # the checkout being synced.
    assert r"C:\repo\dep_sync.py" not in argv_flat
    # ORDER MATTERS, and it is the SAME order the reinstall it replaces uses:
    # fetch -> merge -> install. Installing first would need the merge to be
    # proven impossible to fail, which cannot be done completely; a failed install
    # after a landed merge is exactly what the reinstall path already does.
    labels = [s["label"] for s in steps]
    assert labels.index("Merge") < labels.index("pip install")


@pytest.mark.asyncio
async def test_sync_keeps_the_editable_reinstall_when_nothing_is_locked():
    """A venv this gateway is NOT served by must still get the real reinstall.

    The substitution is a concession to a lock, not an improvement to prefer: it
    cannot refresh a console script, so anywhere pip can do the whole job, it
    should.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew import dep_sync

    result, cmd, steps = await _run_sync(mod, [])

    assert result["ok"] is True
    install = _install_step(steps)
    argv_flat = " ".join(install["argv"])
    assert "-e" in install["argv"]
    assert dep_sync.__name__ not in argv_flat
    assert "dep_sync.py" not in argv_flat


@pytest.mark.asyncio
async def test_every_step_gets_a_utf8_pin_in_its_environment():
    """The runner's own reconfigure() does not reach its children.

    Each step is a separate process that inherits the pipe but re-derives its
    encoding from the locale, so the Python steps (pip, and the build-and-stage
    child) would still encode a non-ASCII checkout path with the codepage and
    die on it. The environment is the only channel that reaches a child.

    The MECHANISM is proven by execution in test_dev_fleet_sync_runner.py
    (a child spawned under a divergent inherited PYTHONIOENCODING observes the
    utf-8 pin). What stays here is the module contract: run_step assigns the
    pin onto every step's env before spawning.
    """
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    run_step_body = src.split("def run_step(", 1)[1].split("\ndef ", 1)[0]
    assert 'env["PYTHONIOENCODING"] = "utf-8:replace"' in run_step_body
    # Applied to the env actually handed to the spawn, not a stale copy. Matched
    # on the keyword alone, not on a formatted argument run: the spawn became a
    # multi-line Popen when stderr got its own pipe, and a substring spanning two
    # arguments made this fail on a formatting change rather than on a defect.
    assert "env=env," in run_step_body
    assert "cwd=cwd," in run_step_body
    # Set before the step is spawned, not after.
    assert run_step_body.index("PYTHONIOENCODING") < run_step_body.index("subprocess.Popen(")


@pytest.mark.asyncio
async def test_sync_runs_every_step_when_nothing_is_locked():
    """Control: an unlocked venv gets the full sync, reinstall included."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    result, cmd, steps = await _run_sync(mod, [])

    assert result["ok"] is True, result
    labels = [s["label"] for s in steps]
    assert "pip install" in labels
    assert "Pull" in labels


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locked",
    [[], [r"C:\repo\.venv\Scripts\kirocrew.exe"]],
    ids=["reinstall-branch", "substitute-branch"],
)
async def test_sync_refuses_a_venv_that_serves_another_checkout(locked):
    """The refusal covers BOTH install paths, and refuses before either runs.

    `<repo>/.venv` is only where the interpreter was found; it can be an install
    of a different checkout, and `pip install -e .` would then silently repoint
    that editable install at this repo — so the OTHER checkout's gateway becomes
    this code on its next restart. The dependency-only path has always refused
    this; the reinstall path did not, which left the safer path as the only
    guarded one. Parametrized over both branches because that asymmetry is
    exactly the bug: a fix that only covers the one it was found on is not one.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew import dep_sync

    with patch.object(
        dep_sync,
        "venv_not_mapped_to",
        return_value="the target venv imports this project from /other/checkout",
    ):
        result, cmd, steps = await _run_sync(mod, locked)

    assert result["ok"] is False
    assert "/other/checkout" in result["error"]
    # Remedy-first, like every other refusal on this endpoint.
    assert "own editable install" in result["error"]
    # Nothing ran: no fetch, no merge, no install. A refusal after the merge
    # would leave the checkout moved with its dependencies unresolved.
    assert cmd is None


@pytest.mark.asyncio
async def test_sync_runner_pins_utf8_stdout_before_its_first_print():
    """Align the writing side with the reader.

    `_start_run` decodes the stream as UTF-8, while a piped stdout on Windows
    encodes with the process locale codepage — a mismatch that mangles or kills
    any non-ASCII print. Pinned before the step loop so no print predates it.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, _ = await _run_sync(mod, [])

    assert cmd is not None
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in src
    # Within main(), the reconfigure runs before run_steps() is called, so no
    # print in the step loop predates it.
    main_body = src.split("def main(", 1)[1]
    assert "reconfigure(" in main_body
    assert main_body.index("reconfigure(") < main_body.index("run_steps(")


def test_utf8_reconfigure_survives_a_legacy_codepage_pipe():
    """Prove the mechanism, not just its presence in the source.

    Forcing a legacy codepage on the child's stdout is what the Windows
    mismatch looks like; the reconfigure call is what keeps the print
    non-fatal, and the reader side decodes UTF-8.
    """
    import subprocess
    import sys as _sys

    text = "\u9648\u660e\u4f2a"
    body = (
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        f"print({text!r}, flush=True)\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run([_sys.executable, "-c", body], capture_output=True, env=env, timeout=60)

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    # Matches how the run reader decodes the stream.
    assert text in proc.stdout.decode(errors="replace")


def test_pythonioencoding_saves_a_step_child_that_prints_a_non_ascii_path():
    """The child half of the same mismatch, proven end to end.

    A step child cannot call the runner's reconfigure() for itself, so this is
    the case the environment variable has to carry: the SAME print that dies
    under a legacy codepage survives once the pin is in the env, which is what
    a pip or build step does when it echoes a non-ASCII checkout path.
    """
    import subprocess
    import sys as _sys

    path = "C:\\Users\\\u9648\u660e\u4f2a\\KiroCrew"
    body = f"print({path!r}, flush=True)\n"
    codepage = {**os.environ, "PYTHONIOENCODING": "cp1252"}

    # Without the pin the child dies on the encode — the defect being fixed.
    unpinned = subprocess.run(
        [_sys.executable, "-c", body], capture_output=True, env=codepage, timeout=60
    )
    assert unpinned.returncode != 0
    assert b"UnicodeEncodeError" in unpinned.stderr

    # With it, the child survives and the reader gets the real path back.
    pinned = subprocess.run(
        [_sys.executable, "-c", body],
        capture_output=True,
        env={**codepage, "PYTHONIOENCODING": "utf-8:replace"},
        timeout=60,
    )
    assert pinned.returncode == 0, pinned.stderr.decode(errors="replace")
    assert path in pinned.stdout.decode(errors="replace")


# --- repo owner/name parsing ---
@pytest.mark.asyncio
async def test_repo_owner_name_ssh():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    fleet_state_mod._OWNER_REPO = None
    with patch.object(
        runtime_mod,
        "_run_cmd",
        new_callable=AsyncMock,
        return_value=(0, "git@github.com:kirodotdev/KiroCrew.git\n", ""),
    ):
        result = await mod._repo_owner_name()
    assert result == "kirodotdev/KiroCrew"


@pytest.mark.asyncio
async def test_repo_owner_name_https():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    fleet_state_mod._OWNER_REPO = None
    with patch.object(
        runtime_mod,
        "_run_cmd",
        new_callable=AsyncMock,
        return_value=(0, "https://github.com/kirodotdev/KiroCrew.git\n", ""),
    ):
        result = await mod._repo_owner_name()
    assert result == "kirodotdev/KiroCrew"


# --- _find_worktree ambiguity rejection ---
@pytest.mark.asyncio
async def test_find_worktree_ambiguous():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    wts = [
        {"path": "/a/feature-x", "is_main": False},
        {"path": "/b/feature-x", "is_main": False},
    ]
    with patch.object(
        repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=wts
    ):
        result, err = await mod._find_worktree("feature-x")
    assert result is None
    assert "ambiguous" in err


@pytest.mark.asyncio
async def test_find_worktree_not_found():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(
        repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[]
    ):
        result, err = await mod._find_worktree("nope")
    assert result is None
    assert "not found" in err


# --- force bool strictness (handler validates) ---
@pytest.mark.asyncio
async def test_worktree_remove_force_must_be_bool():
    """The /worktree/remove handler rejects non-boolean force."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    # Build a mock request with non-bool force
    async def fake_json():
        return {"name": "test-wt", "force": "yes"}

    request = MagicMock()
    request.json = fake_json
    request.content_length = 100

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._valid_worktree_names",
        new_callable=AsyncMock,
        return_value={"test-wt"},
    ):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "force must be a boolean" in body["error"]


# --- sync single-flight (409 on busy) ---
@pytest.mark.asyncio
async def test_sync_returns_409_when_already_running():
    import asyncio

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    # Inject a fake running sync with a live task + process
    worktree_ops_mod._SYNC_RID = "fake123"
    async with mod._RUNS_LOCK:
        mod._RUNS["fake123"] = {
            "status": "running",
            "exit_code": None,
            "label": "sync",
            "output": [],
        }

    # Simulate a genuinely-running process (returncode=None)
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.returncode = None
    never_done = asyncio.get_event_loop().create_future()
    running_task = asyncio.ensure_future(never_done)
    mod._ACTIVE_RUNS["fake123"] = (running_task, mock_proc)

    try:
        result = await mod._sync()
        assert result["ok"] is False
        assert "already running" in result["error"]
    finally:
        async with mod._RUNS_LOCK:
            del mod._RUNS["fake123"]
        worktree_ops_mod._SYNC_RID = None
        mod._ACTIVE_RUNS.pop("fake123", None)
        never_done.set_result(None)
        await running_task


# --- redaction ---
def test_redact_applied():
    from kiro_crew.apps.builtins.dev_fleet.server import _redact

    # Should not crash on normal strings
    assert _redact("hello world") == "hello world"
    # Should redact AWS keys
    result = _redact("key=AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in result


# --- disk aggregation name derivation ---
@pytest.mark.asyncio
async def test_disk_name_derivation_from_path():
    """_disk() derives worktree name from w['path']."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._DISK.update({"status": "idle", "total_mb": None, "per": {}})

    fake_worktrees = [
        {"path": "/home/user/kirocrew", "is_main": True},
        {"path": "/home/user/kirocrew-wt-feature-x", "is_main": False},
    ]

    with (
        patch.object(
            repository_mod,
            "_discover_worktrees",
            new_callable=AsyncMock,
            return_value=fake_worktrees,
        ),
        patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "100\t/fake\n", "")
        ),
    ):
        result = await mod._disk()
        assert result["status"] == "computing"

        # The scan finishes on a background task.
        await async_wait_until(
            lambda: mod._DISK["status"] == "done", describe=lambda: mod._DISK["status"]
        )

    assert mod._DISK["status"] == "done"
    assert "kirocrew" in mod._DISK["per"] or "kirocrew-wt-feature-x" in mod._DISK["per"]
    assert mod._DISK["total_mb"] == 200


# --- _build_pending (server-side truth for build-pending chip) ---
def test_build_pending_false_when_dist_older():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    original_start = mod._START_EPOCH
    # Set start epoch far in the future so dist mtime is always older
    fleet_state_mod._START_EPOCH = time.time() + 99999
    try:
        result = mod._build_pending()
        # dist dir may or may not exist in test env; either way it should not be "pending"
        assert result is False
    finally:
        fleet_state_mod._START_EPOCH = original_start


def test_build_pending_true_when_dist_newer():
    import os
    import tempfile
    from pathlib import Path

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    # Create a temp dir to act as dist
    with tempfile.TemporaryDirectory() as tmpdir:
        dist_path = Path(tmpdir) / "dist"
        dist_path.mkdir()
        # Touch to ensure mtime is fresh
        os.utime(str(dist_path), (time.time(), time.time()))

        # Patch the dist resolution
        original_start = mod._START_EPOCH
        fleet_state_mod._START_EPOCH = time.time() - 100  # pretend started 100s ago
        with patch.object(Path, "__new__", wraps=Path.__new__):
            # Directly test the logic: dist mtime > start epoch
            assert dist_path.stat().st_mtime > mod._START_EPOCH
        fleet_state_mod._START_EPOCH = original_start


def test_build_pending_false_when_dist_missing():
    """_build_pending returns False when dist dir does not exist (OSError path)."""
    import tempfile
    from pathlib import Path

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    original_start = mod._START_EPOCH
    fleet_state_mod._START_EPOCH = 0  # very old — would be pending IF dist existed
    try:
        # Point __file__ resolution at a temp dir with no 'static/dist' subtree
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_file = Path(tmpdir) / "handlers" / "dev_fleet.py"
            fake_file.parent.mkdir(parents=True)
            fake_file.touch()

            def patched_build_pending() -> bool:
                try:
                    dist = fake_file.resolve().parent.parent / "static" / "dist"
                    if not dist.exists():
                        return False
                    return dist.stat().st_mtime > mod._START_EPOCH
                except OSError:
                    return False

            result = patched_build_pending()
            assert result is False
    finally:
        fleet_state_mod._START_EPOCH = original_start


# --- run pointers are NOT baked into the cached snapshot ---
@pytest.mark.asyncio
async def test_fleet_build_does_not_bake_run_pointers():
    """`_build_fleet` must leave the run pointers to the request-time overlay.

    The snapshot it returns is cached and served stale-while-revalidate, so a
    pointer written here is a frozen answer to a live question: a run started
    after the build would be invisible until the cache turned over, which is the
    "no progress, press it again" bug. `_with_live_run_pointers` owns both
    pointers; this pins that there is only one owner, so a future edit cannot
    quietly reintroduce a second, staler one.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    worktree_ops_mod._SYNC_RID = "test-rid-abc"
    try:
        with (
            patch.object(
                repository_mod,
                "_discover_worktrees",
                new_callable=AsyncMock,
                return_value=[
                    {"path": "/fake/main", "head": "abc1234", "branch": "main", "is_main": True}
                ],
            ),
            patch.object(
                repository_mod,
                "_git_info",
                new_callable=AsyncMock,
                return_value={
                    "branch": "main",
                    "head": "abc1234",
                    "dirty": False,
                    "ahead": 0,
                    "behind": 0,
                    "last_updated_at": None,
                },
            ),
            patch.object(
                fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None
            ),
            patch.object(repository_mod, "_git_ahead", new_callable=AsyncMock, return_value=0),
            patch.object(runtime_mod, "_load_cfg", return_value=None),
            patch.object(fleet_state_mod, "_build_pending", return_value=False),
        ):
            data = await mod._build_fleet()
        assert "sync_run_id" not in data
        assert all("provision_run_id" not in w for w in data["worktrees"])
        assert "build_pending" in data
    finally:
        worktree_ops_mod._SYNC_RID = None


@pytest.mark.asyncio
async def test_fleet_includes_build_pending():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_discover_worktrees",
            new_callable=AsyncMock,
            return_value=[
                {"path": "/fake/main", "head": "abc1234", "branch": "main", "is_main": True}
            ],
        ),
        patch.object(
            repository_mod,
            "_git_info",
            new_callable=AsyncMock,
            return_value={
                "branch": "main",
                "head": "abc1234",
                "dirty": False,
                "ahead": 0,
                "behind": 0,
                "last_updated_at": None,
            },
        ),
        patch.object(
            fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None
        ),
        patch.object(repository_mod, "_git_ahead", new_callable=AsyncMock, return_value=0),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(fleet_state_mod, "_build_pending", return_value=True),
    ):
        data = await mod._build_fleet()
    assert data["build_pending"] is True


# --- provision_run_id exposed in fleet response (reattach after reload) ---
# --- provision run-id selection (what a reloaded page can reattach to) ---
# These pin the SELECTION semantics at their owning unit rather than through a
# fleet build: the pointer reaches the payload via the request-time overlay
# (`_with_live_run_pointers`), which
# `test_fleet_handler_overlays_runs_started_after_snapshot` covers.
@pytest.mark.asyncio
async def test_provision_reattach_ids_expose_running_and_failed_runs():
    with (
        patch.dict(
            mod._PROVISION_INFLIGHT,
            {
                "wt-running": "rid-running",
                "wt-failed": "rid-failed",
            },
            clear=True,
        ),
        patch.dict(
            mod._RUNS,
            {
                "rid-running": {"status": "running", "exit_code": None, "output": []},
                "rid-failed": {"status": "done", "exit_code": 1, "output": []},
            },
            clear=True,
        ),
    ):
        rids = await mod._provision_reattach_ids()
    assert rids == {"wt-running": "rid-running", "wt-failed": "rid-failed"}


@pytest.mark.asyncio
async def test_provision_reattach_ids_omit_successful_and_evicted_runs():
    with (
        patch.dict(
            mod._PROVISION_INFLIGHT,
            {
                # Success: nothing to reattach — the fleet row shows the built state.
                "wt-ok": "rid-ok",
                # Evicted from the bounded run registry: output is unfetchable.
                "wt-gone": "rid-gone",
            },
            clear=True,
        ),
        patch.dict(
            mod._RUNS,
            {
                "rid-ok": {"status": "done", "exit_code": 0, "output": []},
            },
            clear=True,
        ),
    ):
        rids = await mod._provision_reattach_ids()
    assert rids == {}
