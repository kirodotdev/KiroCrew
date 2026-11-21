"""The scratch root's stand-in is sealed read-only around its private windows on Linux.

The namespace launcher hides the scratch root with an empty stand-in, a writable tmpfs no
other process sees, and binds the process's own directory and its session tree's directory
back inside it as private windows. A write anywhere else under the root (a stale
``$KIROCREW_SCRATCH``, another session's path) would land in that stand-in, report
success and be deleted with it. The planner therefore marks the scratch root's mask
``seal_around_windows`` and carries it as ``seal_windowed_masks``; the launcher binds every
window and then remounts the stand-in read-only, so such a write fails with EROFS, as the
Seatbelt profile already refuses it on macOS.

The planner cases run against a literal host and against the live host adapter. The
launcher cases drive its own stages with the stand-in libc from
``test_sandbox_launcher_program``, whose binds hide their targets as real mounts do; it
records a read-only remount without enforcing it, so the kernel half is pinned by
``test_scratch_root_seal_real_sandbox.py``. A mask that contains the root, and the check
that every sealed root is still sealed once every mask is placed, are
``test_scratch_root_seal_containers.py``'s.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_sandbox_launcher_program import (
    CoveringLibc,
    _resolved,
    identity,
    launch,
    payload,
    read_back_read_only_remounts,
    refusal,
)

from kiro_crew import agent_scratch, sandbox
from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_plan, sandbox_seatbelt
from kiro_crew.sandbox_plan import BACKEND_NAMESPACE, BACKEND_SEATBELT, SandboxRequest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")


@pytest.fixture(autouse=True)
def read_only_remounts(_floor_monkeypatch: pytest.MonkeyPatch) -> set[tuple[int, int]]:
    """What the fake libc remounted read-only reads back as read-only (``confirm_sealed_roots``).

    Patched on the floor's own stack, so a test's ``monkeypatch.undo()`` leaves it in place.
    """
    return read_back_read_only_remounts(_floor_monkeypatch)


_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher stages pin with os.O_PATH and mount through /proc/self/fd",
)

_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_REMOUNT = 32
_MS_BIND = 4096
_MS_REC = 16384


# --------------------------------------------------------------------------- #
# The planner, against a literal host.
# --------------------------------------------------------------------------- #

_HOME = "/srv/u"
_ROOT = f"{_HOME}/.kiro/crew/scratch"
_OWN = f"{_ROOT}/runtime-own"
_TREE = f"{_ROOT}/runtime-tree"


def _host(**overrides: Any) -> sandbox_plan.PlanHost:
    fields: dict[str, Any] = {
        "home": _HOME,
        "tier_dirs": (".aws", ".kiro/crew/scratch", ".kiro/crew/tasks"),
        "sealed_mask_dirs": (_ROOT,),
        "uid": 1000,
        "gid": 1000,
    }
    fields.update(overrides)
    return sandbox_plan.PlanHost(**fields)


def _plan(
    backend: str = BACKEND_NAMESPACE, host: sandbox_plan.PlanHost | None = None, **request: Any
) -> sandbox_plan.ConfinementPlan:
    return sandbox_plan.plan_confinement(
        SandboxRequest(tier="strict", backend=backend, **request), host or _host()
    )


def test_the_planner_seals_the_listed_mask_with_or_without_windows() -> None:
    for windows in ((), (_OWN,), (_OWN, _TREE)):
        plan = _plan(extra_private_dirs=windows)
        assert plan.windows == windows
        assert plan.seal_windowed_masks == (_ROOT,)
        assert plan.seal_windowless_masks == ()
        wire = sandbox_plan.namespace_payload(plan)
        assert wire["seal_windowed_masks"] == [_ROOT]
        assert wire["seal_windowless_masks"] == []


def test_a_mask_absent_from_the_host_list_is_never_sealed_around_its_windows() -> None:
    plan = _plan(host=_host(sealed_mask_dirs=()), extra_private_dirs=(_OWN,))
    assert plan.seal_windowed_masks == ()
    assert sandbox_plan.namespace_payload(plan)["seal_windowed_masks"] == []
    assert not [m for m in plan.masks if m.seal_around_windows]


def test_the_caller_opt_in_is_unchanged_beside_the_sealed_scratch_root() -> None:
    bundle = "/data/bundle/data"
    plan = _plan(extra_hidden_dirs=(bundle,), seal_hidden_dirs=True, extra_private_dirs=(_OWN,))
    assert plan.seal_windowless_masks == (bundle,)
    assert plan.seal_windowed_masks == (_ROOT,)
    tier_only = _plan(extra_hidden_dirs=(bundle,), extra_private_dirs=(_OWN,))
    assert tier_only.seal_windowless_masks == () and tier_only.seal_windowed_masks == (_ROOT,)


def test_the_listed_spelling_is_folded_onto_the_resolved_data_home() -> None:
    """A ``$HOME`` spelling reached through a link is respelled like every mask path."""
    aliases = ((f"{_HOME}/.kiro/crew", "/data/crew", 7, 8),)
    plan = _plan(crew_home_aliases=aliases, extra_private_dirs=(_OWN,))
    assert "/data/crew/scratch" in plan.sensitive_dirs
    assert plan.windows == ("/data/crew/scratch/runtime-own",)
    assert plan.seal_windowed_masks == ("/data/crew/scratch",)


def test_a_lifted_scratch_root_is_not_sealed() -> None:
    plan = _plan(extra_visible_dirs=(_ROOT,))
    assert _ROOT not in plan.sensitive_dirs
    assert plan.seal_windowed_masks == ()


def test_a_visible_directory_below_the_root_lifts_the_mask_and_its_seal() -> None:
    """A partial lift cancels the whole mask, so there is no stand-in to seal."""
    plan = _plan(extra_visible_dirs=(f"{_ROOT}/shared",), extra_private_dirs=(_OWN,))
    assert _ROOT not in plan.sensitive_dirs
    assert [c.path for c in plan.cancellations] == [_ROOT]
    (mask,) = [m for m in plan.masks if m.path == _ROOT]
    assert mask.cancelled and mask.seal_around_windows
    assert plan.seal_windowed_masks == ()
    assert sandbox_plan.namespace_payload(plan)["seal_windowed_masks"] == []


def test_seatbelt_needs_no_seal_and_already_denies_the_write() -> None:
    """macOS refuses the same write by rule, so the flag is namespace only."""
    plan = _plan(BACKEND_SEATBELT, extra_private_dirs=(_OWN, _TREE))
    assert plan.seal_windowed_masks == ()
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    deny = (
        f"(deny file-write* (require-all (subpath {json.dumps(_ROOT)}) "
        f"(require-not (subpath {json.dumps(_OWN)})) "
        f"(require-not (subpath {json.dumps(_TREE)}))))"
    )
    assert deny in profile.splitlines()


# --------------------------------------------------------------------------- #
# The planner, through the live host adapter.
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home under *tmp_path*, at the names the live adapter reads."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    for name in ("KIROCREW_POD", "KIROCREW_OS_HOME", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: crew)
    monkeypatch.setattr(sandbox, "kiro_agents_dir", lambda: home / ".kiro" / "agents")
    run = crew / "run"
    voice = run / "voice-runtime"
    monkeypatch.setattr(
        sandbox,
        "_voice_runtime_paths_cache",
        (str(crew), str(voice), (str(voice),), (str(run),), (str(run), str(crew))),
    )
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: False)
    return home


@pytest.mark.parametrize("seal_hidden_dirs", [False, True])
@pytest.mark.parametrize("tier", ["standard", "strict", "cc"])
def test_the_production_plan_seals_the_scratch_root_around_its_windows(
    live_home: Path, tier: str, seal_hidden_dirs: bool
) -> None:
    root = str(live_home / ".kiro" / "crew" / "scratch")
    own, tree = f"{root}/runtime-own", f"{root}/runtime-tree"
    plan = sandbox._spawn_plan(
        BACKEND_NAMESPACE, tier, extra_private_dirs=(own, tree), seal_hidden_dirs=seal_hidden_dirs
    )
    wire = sandbox_plan.namespace_payload(plan)
    # Both ``$HOME`` spellings of the data home are listed; the launcher skips an absent one.
    spellings = {str(live_home / prefix / "scratch") for prefix in (".kiro/crew", ".kirocrew")}
    assert spellings <= set(wire["sensitive_dirs"])
    assert own in wire["private_dirs"] and tree in wire["private_dirs"]
    assert set(wire.get("seal_windowed_masks", [])) == spellings
    # The scratch root is a tier mask, so the caller opt-in still does not reach it.
    assert wire["seal_windowless_masks"] == []


def test_a_relocated_data_home_seals_its_own_scratch_root(
    live_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
    root = str(relocated / "scratch")
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard", extra_private_dirs=(f"{root}/a",))
    assert root in plan.sensitive_dirs
    assert plan.windows == (f"{root}/a",)
    assert root in sandbox_plan.namespace_payload(plan).get("seal_windowed_masks", [])


@pytest.mark.parametrize(
    ("tier", "codex_mask", "container_first"),
    [("standard", True, False), ("strict", False, True), ("cc", False, True)],
    ids=["codex mask after the root", "strict tier mask before it", "cc tier mask before it"],
)
def test_a_data_home_under_aws_puts_a_containing_mask_in_the_list(
    live_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    codex_mask: bool,
    container_first: bool,
) -> None:
    """``KIROCREW_HOME`` under ``~/.aws`` is accepted, and ``~/.aws`` is then masked too.

    A Codex session adds the credential home as a caller mask, after every tier mask; the
    strict and cc tiers list ``.aws`` themselves, ahead of the relocated scratch root. Either
    way one mask in the list contains the root the launcher seals around its windows.
    """
    aws = live_home / ".aws"
    relocated = aws / "crew"
    relocated.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)
    root = str(relocated / "scratch")
    plan = sandbox._spawn_plan(
        BACKEND_NAMESPACE,
        tier,
        extra_private_dirs=(f"{root}/own",),
        extra_hidden_dirs=(str(aws),) if codex_mask else (),
    )
    dirs = list(plan.sensitive_dirs)
    assert root in plan.seal_windowed_masks
    assert (dirs.index(str(aws)) < dirs.index(root)) == container_first


def test_a_pods_remapped_home_scratch_is_masked_not_sealed(
    live_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pod's data home is its ``KIROCREW_HOME``; the scratch there is the sealed one.

    The pod's remapped ``HOME`` (``KIROCREW_OS_HOME``) re-anchors the tier leaves, so its
    scratch spelling is masked, but no window is allocated under it, so it is not sealed.
    """
    pod_home = tmp_path / "pod"
    os_home = pod_home / "os-home"
    (os_home / ".kiro" / "crew").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: pod_home)
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))
    root = str(pod_home / "scratch")
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard", extra_private_dirs=(f"{root}/a",))
    remapped = str(os_home / ".kiro" / "crew" / "scratch")
    assert remapped in plan.sensitive_dirs
    assert root in plan.seal_windowed_masks
    assert remapped not in plan.seal_windowed_masks
    assert plan.windows == (f"{root}/a",)


def test_every_sealed_leaf_is_a_hidden_leaf() -> None:
    """A sealed leaf that no mask covers matches no ``Mask`` path, so nothing would be sealed."""
    from kiro_crew.sandbox_leaves import _CREW_HIDDEN_LEAVES, _CREW_SEALED_MASK_LEAVES

    assert set(_CREW_SEALED_MASK_LEAVES) <= set(_CREW_HIDDEN_LEAVES)


@pytest.mark.parametrize("caller", ["cron", "private state"])
def test_an_allocation_the_seal_refuses_degrades_to_the_inherited_temp(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, caller: str
) -> None:
    """Inside a sandbox an allocation under the sealed root fails with EROFS; no caller crashes."""
    from kiro_crew import cron_script_child_env
    from kiro_crew.acp import runtime_spawn_launch

    def _sealed(label: str) -> Path:
        raise OSError(errno.EROFS, "Read-only file system", f"/data/scratch/{label}")

    monkeypatch.setattr(agent_scratch, "allocate_scratch", _sealed)
    caplog.set_level(logging.DEBUG)
    if caller == "cron":
        assert cron_script_child_env._allocate_cron_scratch(None) is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "inherited temp" in warnings[0].getMessage()
    else:
        assert runtime_spawn_launch._allocate_private_state() is None
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_sealed_leaf_table_reaches_the_plan(
    live_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.sandbox_leaves import _CREW_SEALED_MASK_LEAVES

    assert "scratch" in _CREW_SEALED_MASK_LEAVES
    tasks = str(live_home / ".kiro" / "crew" / "tasks")
    monkeypatch.setattr(sandbox, "_CREW_SEALED_MASK_LEAVES", ("tasks",))
    sealed = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard").seal_windowed_masks
    assert tasks in sealed
    assert not [path for path in sealed if path.endswith("/scratch")]


def test_the_seatbelt_host_lists_nothing_to_seal(live_home: Path) -> None:
    host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT))
    assert host.sealed_mask_dirs == ()


# --------------------------------------------------------------------------- #
# The launcher, driven through its stages.
# --------------------------------------------------------------------------- #


class _Scratch:
    """A scratch root with two windows (the process's own directory and its tree's)."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "home" / ".kiro" / "crew" / "scratch"
        self.own = self.root / "runtime-own"
        self.tree = self.root / "runtime-tree"
        self.other = self.root / "runtime-other"
        for directory in (self.own, self.tree, self.other):
            directory.mkdir(parents=True)
        (self.tree / "brief.md").write_text("tree brief")
        (self.other / "theirs.md").write_text("another session's")
        self.windows = [str(self.own), str(self.tree)]


def _read_only_remounts(libc: CoveringLibc) -> list[int]:
    """Indexes of every read-only remount the stages made, in call order."""
    return [
        i
        for i, call in enumerate(libc.calls)
        if call.flags & _MS_REMOUNT and call.flags & _MS_RDONLY
    ]


def _window_binds(libc: CoveringLibc, windows: list[str]) -> list[int]:
    """Indexes of the binds that open each window at its own path."""
    return [
        i
        for i, call in enumerate(libc.calls)
        if call.flags & _MS_BIND and not call.flags & _MS_REMOUNT and call.target_path in windows
    ]


def _placed(
    tmp_path: Path,
    scratch: _Scratch,
    environ: dict[str, str] | None = None,
    *,
    drop: tuple[str, ...] = (),
    **plan: Any,
) -> tuple[program.Launch, CoveringLibc]:
    """Place the masks of a plan over *scratch* and confirm its sealed roots, as ``run_child`` does.

    *drop* removes payload keys first.
    """
    fields: dict[str, Any] = {
        "sensitive_dirs": [str(scratch.root)],
        "private_dirs": scratch.windows,
    }
    fields.update(plan)
    wire = payload(**fields)
    for key in drop:
        del wire[key]
    libc = CoveringLibc()
    run = launch(tmp_path, wire, libc=libc, environ=environ)
    program.place_masks(run)
    program.retire_stand_in_stage(run)
    program.confirm_sealed_roots(run)
    return run, libc


def read_back_survives_a_test_local_undo(
    module: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Assert *module*'s autouse read-back cannot be lifted by a test's own ``monkeypatch.undo()``.

    Without undoing the shared stack, which would also lift any other fixture's patch on it:
    the fixture registers its patches on ``_floor_monkeypatch`` alone, and the helper puts
    them on the stack it is given, so an undo of another stack leaves them in place.
    """
    tree = ast.parse(Path(module).read_text(encoding="utf-8"))
    (fixture,) = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "read_only_remounts"
    ]
    assert any("autouse=True" in ast.unparse(d) for d in fixture.decorator_list)
    assert [arg.arg for arg in fixture.args.args] == ["_floor_monkeypatch"]
    returned = [
        ast.unparse(n.value) for n in ast.walk(fixture) if isinstance(n, ast.Return) and n.value
    ]
    assert returned == ["read_back_read_only_remounts(_floor_monkeypatch)"]
    read_back = os.fstatvfs
    with monkeypatch.context() as stack:
        read_back_read_only_remounts(stack)
        assert os.fstatvfs is not read_back
        stack.undo()
        assert os.fstatvfs is read_back, "an undo lifts only its own stack's patches"
    scratch = _Scratch(tmp_path)
    run, _libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])
    assert str(scratch.root) in run.masked_names


@_LINUX_ONLY
def test_a_test_local_undo_keeps_the_read_only_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autouse read-back is on the floor's own stack, so a test's ``undo()`` keeps it.

    Without it the fake seal reads back as writable and the last check refuses.
    """
    read_back_survives_a_test_local_undo(__file__, monkeypatch, tmp_path)


@_LINUX_ONLY
def test_the_stand_in_is_sealed_after_every_window_is_bound(tmp_path: Path) -> None:
    scratch = _Scratch(tmp_path)
    run, libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])

    stand_in = run.masked_names[str(scratch.root)]
    seals = _read_only_remounts(libc)
    assert [libc.calls[i].target_id for i in seals] == [stand_in]
    assert [libc.calls[i].target_path for i in seals] == [str(scratch.root)]
    binds = _window_binds(libc, scratch.windows)
    assert len(binds) == 2 and max(binds) < seals[0], "the seal must follow every window bind"
    seal = libc.calls[seals[0]]
    assert seal.flags & _MS_BIND and seal.source_id == stand_in
    # The windows are bound back on their real trees, and nothing else is in the stand-in.
    assert (scratch.tree / "brief.md").read_text() == "tree brief"
    assert sorted(os.listdir(scratch.root)) == ["runtime-own", "runtime-tree"]


@_LINUX_ONLY
def test_no_window_is_remounted_read_only(tmp_path: Path) -> None:
    scratch = _Scratch(tmp_path)
    _run, libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])
    for i in _read_only_remounts(libc):
        assert libc.calls[i].target_path not in scratch.windows
    for window in scratch.windows:
        probe = Path(window) / "written-inside"
        probe.write_text("kept")
        assert probe.read_text() == "kept"


@_LINUX_ONLY
@pytest.mark.parametrize(
    "sets", [("seal_windowed_masks",), ("seal_windowed_masks", "seal_windowless_masks")]
)
def test_a_windowless_scratch_root_is_sealed_once(tmp_path: Path, sets: tuple[str, ...]) -> None:
    """No window (its allocation failed): sealed once, whether one set lists it or both."""
    scratch = _Scratch(tmp_path)
    listed = {key: [str(scratch.root)] for key in sets}
    run, libc = _placed(tmp_path, scratch, private_dirs=[], **listed)
    seals = _read_only_remounts(libc)
    assert [libc.calls[i].target_id for i in seals] == [run.masked_names[str(scratch.root)]]


@_LINUX_ONLY
@pytest.mark.parametrize("opted_in", [False, True])
def test_without_the_set_a_windowed_mask_keeps_its_writable_stand_in(
    tmp_path: Path, opted_in: bool
) -> None:
    """A mask the plan does not seal around its windows keeps the windowed exemption."""
    scratch = _Scratch(tmp_path)
    listed = {"seal_windowless_masks": [str(scratch.root)]} if opted_in else {}
    _run, libc = _placed(tmp_path, scratch, **listed)
    assert _read_only_remounts(libc) == []


@_LINUX_ONLY
def test_an_older_payload_seals_nothing_around_its_windows(tmp_path: Path) -> None:
    """A payload from before the key, through ``place_masks``: no read-only remount."""
    scratch = _Scratch(tmp_path)
    environ = {"TMPDIR": str(scratch.root / "runtime-stale")}
    run, libc = _placed(tmp_path, scratch, environ, drop=("seal_windowed_masks",))
    assert run.seal_windowed_masks == frozenset()
    assert run.masked_names.get(str(scratch.root)) is not None
    assert _read_only_remounts(libc) == []
    program.scrub_env(run)
    assert environ["TMPDIR"] == str(scratch.root / "runtime-stale")


@_LINUX_ONLY
def test_the_seal_reasserts_the_locked_flags_of_the_stand_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remount carries the stand-in's nosuid/nodev/noexec, read once the windows are bound.

    Inside a user namespace a remount that would clear a locked bit is refused with EPERM,
    and that refusal would end every spawn, so the bits ``_locked_mount_flags`` reads must
    be on the seal.
    """
    scratch = _Scratch(tmp_path)
    reads: list[tuple[int, int] | None] = []

    def _locked(target: Any) -> SimpleNamespace:
        reads.append(identity(target))
        return SimpleNamespace(f_flag=os.ST_NOSUID | os.ST_NODEV | os.ST_NOEXEC)

    monkeypatch.setattr(os, "statvfs", _locked)
    run, libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])

    stand_in = run.masked_names[str(scratch.root)]
    (seal,) = [libc.calls[i] for i in _read_only_remounts(libc)]
    assert seal.flags == (_MS_REMOUNT | _MS_BIND | _MS_RDONLY | _MS_NOSUID | _MS_NODEV | _MS_NOEXEC)
    assert reads == [stand_in], "the flags are read off the stand-in, once"


@_LINUX_ONLY
def test_an_inherited_temp_path_under_the_sealed_root_is_dropped(tmp_path: Path) -> None:
    """A temp or log variable naming the sealed root outside every window is removed.

    Such a value is inherited from the gateway's environment (an agent shell that restarted
    it), and with the root sealed it could only fail with EROFS. A value outside the root,
    one inside a bound window, and ``KIROCREW_SCRATCH`` are kept.
    """
    scratch = _Scratch(tmp_path)
    stale = scratch.root / "runtime-stale"
    environ = {
        "TMPDIR": str(stale),
        "TMP": str(scratch.root),
        "TEMP": str(scratch.own / ".." / "runtime-stale"),
        "KIRO_CHAT_LOG_FILE": str(stale / "kiro-log" / "kiro-chat.log"),
        "KIROCREW_SCRATCH": str(stale),
        "OTHER": str(stale),
    }
    run, _libc = _placed(tmp_path, scratch, environ, seal_windowed_masks=[str(scratch.root)])
    program.scrub_env(run)
    assert {
        k: environ.get(k) for k in ("TMPDIR", "TMP", "TEMP", "KIRO_CHAT_LOG_FILE")
    } == dict.fromkeys(("TMPDIR", "TMP", "TEMP", "KIRO_CHAT_LOG_FILE"))
    assert environ["KIROCREW_SCRATCH"] == str(stale)
    assert environ["OTHER"] == str(stale)


@_LINUX_ONLY
def test_a_temp_path_inside_a_bound_window_or_outside_the_root_is_kept(tmp_path: Path) -> None:
    scratch = _Scratch(tmp_path)
    environ = {
        "TMPDIR": str(scratch.own),
        "TMP": str(tmp_path / "elsewhere"),
        "TEMP": str(scratch.tree) + "/",
        "KIRO_CHAT_LOG_FILE": str(scratch.own / "kiro-log" / "kiro-chat.log"),
    }
    kept = dict(environ)
    run, _libc = _placed(tmp_path, scratch, environ, seal_windowed_masks=[str(scratch.root)])
    program.scrub_env(run)
    assert {k: environ[k] for k in kept} == kept


@_LINUX_ONLY
def test_the_dropped_keys_are_the_scratch_keys_a_tool_writes_unasked(tmp_path: Path) -> None:
    """Every key ``agent_scratch`` exports, except the one an agent writes on purpose."""
    scratch = _Scratch(tmp_path)
    stale = str(scratch.root / "runtime-stale")
    environ = dict.fromkeys(agent_scratch.SCRATCH_ENV_KEYS, stale)
    run, _libc = _placed(tmp_path, scratch, environ, seal_windowed_masks=[str(scratch.root)])
    program.scrub_env(run)
    kept = {key for key in agent_scratch.SCRATCH_ENV_KEYS if key in environ}
    assert kept == {"KIROCREW_SCRATCH"}


@_LINUX_ONLY
def test_nothing_is_dropped_unless_a_listed_mask_was_placed(tmp_path: Path) -> None:
    """Not listed, or listed but absent on disk: the environment is left as it is."""
    scratch = _Scratch(tmp_path)
    stale = str(scratch.root / "runtime-stale")
    environ = {"TMPDIR": stale}
    run, _libc = _placed(tmp_path, scratch, environ)
    program.scrub_env(run)
    assert environ["TMPDIR"] == stale
    absent = tmp_path / "absent" / "scratch"
    environ = {"TMPDIR": str(absent / "x")}
    run, libc = _placed(
        tmp_path,
        scratch,
        environ,
        sensitive_dirs=[str(absent)],
        private_dirs=[],
        seal_windowed_masks=[str(absent)],
    )
    assert _read_only_remounts(libc) == []
    program.scrub_env(run)
    assert environ["TMPDIR"] == str(absent / "x")


@_LINUX_ONLY
def test_a_window_the_launcher_skips_is_sealed_over_and_its_temp_is_dropped(
    tmp_path: Path,
) -> None:
    """An unvouched window that cannot be opened is skipped; the root is still sealed.

    The process's own directory vanished (swept, or a link was planted there) before the
    launcher pinned it. Nothing vouched for it, so the launcher skips it and the mask covers
    its name: a write there fails with EROFS instead of landing in the stand-in. Its temp
    and log variables would fail the same way unasked, so they are dropped and the tools
    fall back to ``/tmp``; ``KIROCREW_SCRATCH`` (the tree) and the tree window are kept.
    """
    scratch = _Scratch(tmp_path)
    scratch.own.rmdir()
    environ = {
        "TMPDIR": str(scratch.own),
        "KIRO_CHAT_LOG_FILE": str(scratch.own / "kiro-log" / "kiro-chat.log"),
        "KIROCREW_SCRATCH": str(scratch.tree),
    }
    run, libc = _placed(tmp_path, scratch, environ, seal_windowed_masks=[str(scratch.root)])

    stand_in = run.masked_names[str(scratch.root)]
    assert [libc.calls[i].target_id for i in _read_only_remounts(libc)] == [stand_in]
    assert run.bound_windows == {str(scratch.tree)}
    assert sorted(os.listdir(scratch.root)) == ["runtime-tree"]
    assert (scratch.tree / "brief.md").read_text() == "tree brief"
    program.scrub_env(run)
    assert "TMPDIR" not in environ and "KIRO_CHAT_LOG_FILE" not in environ
    assert environ["KIROCREW_SCRATCH"] == str(scratch.tree)


@_LINUX_ONLY
def test_every_stage_after_the_seal_leaves_the_sealed_stand_in_untouched(tmp_path: Path) -> None:
    """The stages after the seal place their mounts elsewhere and create nothing in it.

    A leaf nested in a window is re-hidden after the seal, inside the window's real tree; a
    file mask under the root is skipped as covered; an exposed file is restored into its own
    mask; the file masks and ``~/.ssh`` are bound outside the root.
    """
    scratch = _Scratch(tmp_path)
    home = tmp_path / "home"
    nested = scratch.own / "hidden"
    nested.mkdir()
    (nested / "secret").write_text("s")
    index = scratch.root / ".trees.json"
    index.write_text("{}")
    aws = home / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[profile x]\n")
    (aws / "credentials").write_text("[default]\n")
    netrc = home / ".netrc"
    netrc.write_text("machine x\n")
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("key")
    (ssh / "known_hosts").write_text("host ssh-ed25519 AAAA\n")
    masked = [str(aws), str(scratch.root), str(nested), str(index)]
    libc = CoveringLibc()
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=masked,
            private_dirs=scratch.windows,
            seal_windowed_masks=[str(scratch.root)],
            sensitive_files=[str(netrc), *masked],
            expose_files=[[str(aws / "config"), "config"]],
            ssh_dir=str(ssh),
            ssh_known_hosts=str(ssh / "known_hosts"),
            hide_ssh=1,
        ),
        libc=libc,
    )
    program.preread_exposed_files(run)
    program.place_masks(run)
    program.confirm_crew_home_aliases(run)

    seals = _read_only_remounts(libc)
    stand_in = run.masked_names[str(scratch.root)]
    assert [libc.calls[i].target_id for i in seals] == [stand_in]
    # The nested leaf was re-hidden after the seal, inside the window.
    rehide = [
        i
        for i, call in enumerate(libc.calls)
        if call.target_path == str(nested) and not call.flags & _MS_REMOUNT
    ]
    assert rehide and min(rehide) > seals[0]
    assert os.listdir(nested) == []
    # Nothing but the two window mount points is in the sealed stand-in.
    assert sorted(os.listdir(scratch.root)) == ["runtime-own", "runtime-tree"]
    assert (aws / "config").read_text() == "[profile x]\n"
    assert not (aws / "credentials").exists()


# --------------------------------------------------------------------------- #
# The launcher's own temp directory, when no stand-in root qualifies.
# --------------------------------------------------------------------------- #


class _SealingLibc(CoveringLibc):
    """``CoveringLibc`` plus two kernel behaviours a stand-in made in ``tempfile``'s dir meets.

    A bind whose source sits inside its target (a stand-in created in the directory it then
    hides) leaves the target showing the source's tree, as the kernel does. A read-only
    remount makes the directory refuse creates (EACCES through its mode, where the kernel
    answers EROFS), which is what turns a stand-in created there into an ended launch.
    """

    def __init__(self) -> None:
        super().__init__()
        self.read_only: list[str] = []

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if flags & _MS_REMOUNT:
            if flags & _MS_RDONLY:
                path = _resolved(target)
                os.chmod(path, 0o555)
                self.read_only.append(path)
            return 0
        if source is not None and flags & _MS_BIND and fstype != b"tmpfs":
            src, tgt = _resolved(source), _resolved(target)
            if src.startswith(tgt.rstrip("/") + "/") and src not in self.stages:
                moved = os.path.join(str(self.aside_root), str(len(self.covered)))
                os.rename(tgt, moved)
                os.rename(moved + src[len(tgt) :], tgt)
                self.aside[tgt] = moved
                self.covered.append(tgt)
                return 0
        return super().bound(source, target, fstype, flags)


@pytest.fixture
def no_stand_in_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A host where no stand-in root qualifies: stand-ins go to ``tempfile``'s directory.

    ``tempfile`` reads ``os.environ``, so the temp variables are set there, and its cache is
    cleared and restored around the test. ``TEMP`` names ``safe``, a directory outside every
    tree, so a fallback never reaches the real ``/tmp``.
    """
    scratch = _Scratch(tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    aws = tmp_path / "home" / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\n")
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.setenv("TEMP", str(safe))
    return {
        "root": scratch.root,
        "own": scratch.own,
        "tree": scratch.tree,
        "safe": safe,
        "aws": aws,
    }


def _no_root_launch(tmp_path: Path, libc: CoveringLibc, **plan: Any) -> program.Launch:
    run = launch(tmp_path, payload(**plan), libc=libc, environ=os.environ)
    run.tmpfs_src = None
    return run


@_LINUX_ONLY
@pytest.mark.parametrize("spelling", ["the root itself", "a directory below it"])
def test_a_temp_dir_under_the_sealed_root_does_not_end_a_launch_with_no_stand_in_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    spelling: str,
) -> None:
    """``TMPDIR`` names the scratch root, or a directory under it, and no root qualifies.

    Without the pin, the first stand-in is made in that directory, ``tempfile`` caches it,
    the scratch mask hides it (the root is sealed, a directory below it is gone), and the
    next mask's stand-in create raises, ending the launch. With it, every stand-in is made
    in ``safe`` and the agent still gets its ``TMPDIR`` back for ``scrub_env`` to judge.
    """
    t = no_stand_in_root
    temp = t["root"]
    if spelling == "a directory below it":
        temp = t["root"] / "runtime-stale"
        temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp))
    libc = _SealingLibc()
    run = _no_root_launch(
        tmp_path,
        libc,
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        seal_windowed_masks=[str(t["root"])],
    )
    try:
        program.pick_stand_in_root(run)
        program.place_masks(run)
    finally:
        for path in libc.read_only:
            os.chmod(path, 0o755)

    assert libc.read_only == [str(t["root"])]
    assert run.masked_names.get(str(t["aws"])) is not None, "the mask after the seal landed"
    assert tempfile.gettempdir() == str(t["safe"])
    assert os.environ["TMPDIR"] == str(temp)


class _StagingLibc(_SealingLibc):
    """``_SealingLibc`` that accepts the private stand-in stage's tmpfs and records where.

    Once the stage is accepted every stand-in is spelled below its descriptor
    (``/proc/self/fd/<n>/kirocrew_sb_...``); each such path is handed on as the directory it
    names, which is how the kernel resolves it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.staged: list[str] = []

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        return super().mount(_below_fd(source), _below_fd(target), fstype, flags, data)

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if fstype == b"tmpfs":
            self.staged.append(_resolved(target))
            return 0
        return super().bound(source, target, fstype, flags)


def _below_fd(path: Any) -> Any:
    """*path* with a ``/proc/self/fd/<n>/`` prefix replaced by the directory ``<n>`` holds."""
    if path is None:
        return None
    spelling = os.fsdecode(path)
    prefix = "/proc/self/fd/"
    if not spelling.startswith(prefix) or "/" not in spelling[len(prefix) :]:
        return path
    fd, rest = spelling[len(prefix) :].split("/", 1)
    return os.fsencode(os.path.join(os.readlink(prefix + fd), rest))


@_LINUX_ONLY
@pytest.mark.parametrize("spelling", ["the root itself", "a directory below it"])
def test_the_private_stage_is_made_outside_the_sealed_root_and_leaves_nothing_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    spelling: str,
) -> None:
    """With no stand-in root, the private stand-in stage is made in ``tempfile``'s directory.

    Made under the scratch root, the stage would be hidden by the mask placed over that root,
    so ``retire_stand_in_stage`` could not remove its host directory and an empty
    ``kirocrew_sb_`` directory would stay in the real scratch root. Made in ``safe``, every
    stand-in is created on the stage, the root is sealed around its windows, and the stage is
    gone from the host once the masks are placed.
    """
    t = no_stand_in_root
    temp = t["root"]
    if spelling == "a directory below it":
        temp = t["root"] / "runtime-stale"
        temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp))
    # The stand-in libc mounts nothing, so the stage stays a plain directory; the
    # fresh-mount check is what a real tmpfs passes, and is pinned to that answer.
    monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)
    libc = _StagingLibc()
    run = _no_root_launch(
        tmp_path,
        libc,
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        seal_windowed_masks=[str(t["root"])],
    )
    try:
        program.pick_stand_in_root(run)
        program.stage_stand_ins_privately(run)
        assert run.stand_in_stage is not None, "the stage was mounted"
        program.place_masks(run)
        program.retire_stand_in_stage(run)
    finally:
        for path in libc.read_only:
            os.chmod(path, 0o755)

    (stage,) = libc.staged
    assert os.path.dirname(stage) == str(t["safe"])
    assert libc.read_only == [str(t["root"])]
    assert run.masked_names.get(str(t["aws"])) is not None, "the mask after the seal landed"
    assert sorted(os.listdir(t["root"])) == ["runtime-own", "runtime-tree"]
    assert os.listdir(t["safe"]) == [], "the retired stage left its host directory behind"
    assert os.environ["TMPDIR"] == str(temp)


class _StuckStageLibc(_StagingLibc):
    """``_StagingLibc`` on which the stand-in stage will not detach.

    The stage is detached through its own descriptor (``/proc/self/fd/<n>``); the window
    stages, made inside it, still detach.
    """

    def umount2(self, target, flags):  # noqa: ANN001, ANN201
        spelled = os.fsdecode(target)
        if not (spelled.startswith("/proc/self/fd/") and spelled[14:].isdigit()):
            return super().umount2(target, flags)
        self.detached.append(os.fsdecode(target))
        ctypes.set_errno(errno.EBUSY)
        return -1


def _staged_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    t: dict[str, Path],
    libc: CoveringLibc,
    *,
    stage_mounts: bool,
) -> tuple[program.Launch, str | None]:
    """Stage, place, retire and confirm over the no-root host, as ``run_child`` does.

    With *stage_mounts* the fresh-mount check is pinned to what a real tmpfs answers, as in
    the test above; without it the stage is refused and the stand-ins stay in ``safe``.
    """
    monkeypatch.setenv("TMPDIR", str(t["safe"]))
    if stage_mounts:
        monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)
    run = _no_root_launch(
        tmp_path,
        libc,
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        seal_windowed_masks=[str(t["root"])],
    )
    try:
        program.pick_stand_in_root(run)
        program.stage_stand_ins_privately(run)
        assert (run.stand_in_stage is not None) == stage_mounts
        program.place_masks(run)
        program.retire_stand_in_stage(run)
        return run, refusal(program.confirm_sealed_roots, run)
    finally:
        for path in getattr(libc, "read_only", ()):
            os.chmod(path, 0o755)


def _source_closes(libc: CoveringLibc, stand_in: Any) -> list[int]:
    """Indexes of the binds that put a view of the sealed root over its stand-in's own entry."""
    return [
        i
        for i, call in enumerate(libc.calls)
        if call.flags & _MS_BIND
        and not call.flags & (_MS_REMOUNT | _MS_REC)
        and isinstance(call.target, bytes)
        and call.target.startswith(b"/proc/self/fd/")
        and call.source_id == stand_in
        and call.target_id == stand_in
    ]


@_LINUX_ONLY
def test_a_stand_in_on_the_private_stage_has_its_entry_closed_and_goes_with_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """The control: the entry on the stage is covered, and the stage detaches and is removed."""
    libc = _StagingLibc()
    run, message = _staged_run(tmp_path, monkeypatch, no_stand_in_root, libc, stage_mounts=True)
    root = str(no_stand_in_root["root"])
    assert message is None, message
    source, how = run.stand_in_sources[root]
    assert how == "closed"
    (stage,) = libc.staged
    assert os.path.dirname(source) == stage, "the entry is recorded by its host path"
    assert len(_source_closes(libc, run.masked_names[root])) == 1
    assert os.listdir(no_stand_in_root["safe"]) == [], "the stage was retired"


def _entry_on_a_stuck_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t: dict[str, Path], libc: CoveringLibc
) -> tuple[program.Launch, str, str]:
    """Stage, place and retire with a stage that will not detach: ``(run, root, entry)``."""
    run, message = _staged_run(tmp_path, monkeypatch, t, libc, stage_mounts=True)
    assert message is None, message
    root = str(t["root"])
    entry, how = run.stand_in_sources[root]
    (stage,) = libc.staged
    assert how == "closed" and os.path.dirname(entry) == stage
    assert libc.detached and os.path.isdir(stage), "the stage stayed in place"
    return run, root, entry


@_LINUX_ONLY
def test_a_private_stage_that_does_not_detach_leaves_the_spawn_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """As on main, a stage that stays mounted is a warning: its sealed entry is covered."""
    libc = _StuckStageLibc()
    run, root, entry = _entry_on_a_stuck_stage(tmp_path, monkeypatch, no_stand_in_root, libc)
    assert len(_source_closes(libc, run.masked_names[root])) == 1
    # The fake places a mount by renaming, so the entry is gone, as it is on a stage the
    # agent cannot reach; the next test puts a path back at it.
    assert not os.path.lexists(entry)


@_LINUX_ONLY
@pytest.mark.parametrize("cover", ["read-only", "writable"])
def test_an_entry_on_a_stage_that_does_not_detach_must_be_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path], cover: str
) -> None:
    """The stage stays reachable and its entry still reaches the root: read-only runs, else refuses.

    A link put back at the entry stands for the entry a real kernel keeps under the stage;
    the cover reads back read-only as the sealed root's own mount does, or writable, as
    when the bind meant to cover it did not land. The read-back goes by inode, so this pins
    the last check's answer to each; which mount the entry reaches on a kernel, the cover
    or a copy of the stage, is ``test_scratch_root_seal_stage_mounts.py``'s.
    """
    libc = _StuckStageLibc()
    run, root, entry = _entry_on_a_stuck_stage(tmp_path, monkeypatch, no_stand_in_root, libc)
    os.symlink(root, entry)
    if cover == "writable":
        _read_back_writable_at(monkeypatch, entry)
    message = refusal(program.confirm_sealed_roots, run)
    if cover == "read-only":
        assert message is None, message
    else:
        assert message is not None and entry in message and root in message, message
        assert "still reaches it on a writable mount" in message


@_LINUX_ONLY
@pytest.mark.parametrize("stage_mounts", [True, False], ids=["on the stage", "on the host root"])
def test_a_cover_that_cannot_be_placed_refuses_the_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    stage_mounts: bool,
) -> None:
    """The bind over the stand-in's own entry fails: the spawn refuses at that bind."""
    libc = _NoCoverLibc() if stage_mounts else _NoCoverHostLibc()
    monkeypatch.setenv("TMPDIR", str(no_stand_in_root["safe"]))
    if stage_mounts:
        monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)
    run = _no_root_launch(
        tmp_path,
        libc,
        sensitive_dirs=[str(no_stand_in_root["root"])],
        private_dirs=[str(no_stand_in_root["own"]), str(no_stand_in_root["tree"])],
        seal_windowed_masks=[str(no_stand_in_root["root"])],
    )
    try:
        program.pick_stand_in_root(run)
        program.stage_stand_ins_privately(run)
        message = refusal(program.place_masks, run)
    finally:
        for path in libc.read_only:
            os.chmod(path, 0o755)
    assert message is not None and "closing the writable entry" in message, message
    assert str(no_stand_in_root["root"]) in message


def _read_back_writable_at(monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    """A descriptor opened at *entry* reads back as on a writable mount."""
    opened: dict[int, str] = {}
    real_open, read_back = os.open, os.fstatvfs

    def _open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = os.fsdecode(path)
        return fd

    def _fstatvfs(fd: int) -> Any:
        flags = read_back(fd).f_flag
        if opened.get(fd) == entry:
            flags &= ~os.ST_RDONLY
        return SimpleNamespace(f_flag=flags)

    monkeypatch.setattr(os, "open", _open)
    monkeypatch.setattr(os, "fstatvfs", _fstatvfs)


class _NoCoverMixin:
    """A libc on which the bind covering the sealed stand-in's own entry fails."""

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        spelled_source, spelled_target = os.fsdecode(source or b""), os.fsdecode(target or b"")
        if (
            flags == _MS_BIND
            and spelled_source.startswith("/proc/self/fd/")
            and spelled_target.startswith("/proc/self/fd/")
            and spelled_source != spelled_target
            and identity(source) is not None
            and identity(source) == identity(target)
        ):
            ctypes.set_errno(errno.EPERM)
            return -1
        return super().mount(source, target, fstype, flags, data)  # type: ignore[misc]


class _NoCoverLibc(_NoCoverMixin, _StagingLibc):
    """``_StagingLibc`` that refuses the cover."""


class _NoCoverHostLibc(_NoCoverMixin, _SealingLibc):
    """``_SealingLibc`` that refuses the cover."""


@_LINUX_ONLY
def test_without_the_private_stage_the_stand_ins_own_entry_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """The stage is refused, so the stand-in is made in ``safe``, where the agent can reach it.

    After the seal, a view of the sealed root is bound over the stand-in's own entry there,
    through a descriptor held on it since it was made, and the check passes; a write
    through that entry then fails as one under the root does (pinned under a real kernel by
    the real-sandbox test).
    """
    libc = CoveringLibc()
    run, message = _staged_run(tmp_path, monkeypatch, no_stand_in_root, libc, stage_mounts=False)
    root = str(no_stand_in_root["root"])
    assert message is None, message
    stand_in = run.masked_names[root]
    source, how = run.stand_in_sources[root]
    assert how == "closed"
    assert os.path.dirname(source) == str(no_stand_in_root["safe"])
    (close,) = _source_closes(libc, stand_in)
    seal = [i for i in _read_only_remounts(libc) if libc.calls[i].target_id == stand_in]
    assert seal and seal[0] < close, "the view bound over the entry is the sealed root"
    # Two descriptors: the root reached by its name, bound over the entry's own; the
    # entry bound over itself would be the writable view it is there to cover.
    assert libc.calls[close].source != libc.calls[close].target


@_LINUX_ONLY
def test_a_stand_in_replaced_as_it_is_made_refuses_before_it_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor held on the sealed root's stand-in must be the stand-in just made."""
    scratch = _Scratch(tmp_path)
    libc = CoveringLibc()
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=[str(scratch.root)],
            private_dirs=scratch.windows,
            seal_windowed_masks=[str(scratch.root)],
        ),
        libc=libc,
    )
    monkeypatch.setattr(program, "_stand_in_identity", lambda _stand_in: (0, 0))
    message = refusal(program.place_masks, run)
    assert message is not None and "was replaced as it was made" in message, message
    assert str(scratch.root) in message
    assert not [call for call in libc.calls if call.target_path == str(scratch.root)]


@_LINUX_ONLY
@pytest.mark.parametrize(
    "where",
    ["root", "below the root", "the own window", "an odd spelling", "a sealed path", "a mask"],
)
def test_the_launchers_own_temp_dir_is_outside_every_masked_or_sealed_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    where: str,
) -> None:
    """Every stand-in this launcher makes lands outside the trees it masks or seals."""
    t = no_stand_in_root
    run_dir = tmp_path / "home" / ".kiro" / "crew" / "run"
    run_dir.mkdir()
    temp = {
        "root": t["root"],
        "below the root": t["root"] / "runtime-other",
        "the own window": t["own"],
        "an odd spelling": Path(str(t["root"]) + "//runtime-own/."),
        "a sealed path": run_dir,
        "a mask": t["aws"],
    }[where]
    monkeypatch.setenv("TMPDIR", str(temp))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        readonly_dirs=[str(run_dir)],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    stand_in = Path(program._stand_in_mkdtemp(run))
    assert stand_in.parent == t["safe"]
    assert os.environ["TMPDIR"] == str(temp), "the agent's own value is put back"


@_LINUX_ONLY
def test_a_temp_dir_outside_every_tree_is_used_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    t = no_stand_in_root
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("TMPDIR", str(elsewhere))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == elsewhere


@_LINUX_ONLY
def test_a_stand_in_root_leaves_tempfile_and_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """With a qualifying root ``tempfile`` is never asked, exactly as before the pin."""
    t = no_stand_in_root
    monkeypatch.setenv("TMPDIR", str(t["root"]))
    shm = tmp_path / "shm"
    shm.mkdir()
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        seal_windowed_masks=[str(t["root"])],
        stand_in_roots=[str(shm)],
    )
    monkeypatch.setattr(program, "_home_device", lambda _environ: None)
    program.pick_stand_in_root(run)
    assert run.tmpfs_src == str(shm)
    assert tempfile.tempdir is None
    assert os.environ["TMPDIR"] == str(t["root"])


@_LINUX_ONLY
def test_a_cached_temp_dir_does_not_survive_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    t = no_stand_in_root
    monkeypatch.setattr(tempfile, "tempdir", str(t["root"]))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert tempfile.gettempdir() != str(t["root"])
    assert not Path(tempfile.gettempdir()).is_relative_to(t["root"])


@_LINUX_ONLY
@pytest.mark.parametrize("answer", ["none usable", "inside a mask"])
def test_no_temp_dir_outside_the_trees_refuses_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path], answer: str
) -> None:
    t = no_stand_in_root

    def _gettempdir() -> str:
        if answer == "none usable":
            raise FileNotFoundError("No usable temporary directory found")
        return str(t["root"] / "runtime-own")

    monkeypatch.setattr(tempfile, "gettempdir", _gettempdir)
    monkeypatch.setenv("TMPDIR", str(t["root"]))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        seal_windowed_masks=[str(t["root"])],
    )
    message = refusal(program.pick_stand_in_root, run)
    assert message is not None and "no temporary directory outside the masked" in message
    # ``scratch_env`` sets TMPDIR on every scratch-allocated spawn, so the remedy is one
    # that holds across spawns.
    assert "/dev/shm" in message and "/tmp" in message and "Set TMPDIR" not in message
    assert os.environ["TMPDIR"] == str(t["root"])


def _crew_alias(scratch_root: Path) -> list[list[Any]]:
    """``crew_home_aliases`` with ``~/.kirocrew`` linked to the canonical ``~/.kiro/crew``."""
    canonical = scratch_root.parent
    alias = canonical.parent.parent / ".kirocrew"
    alias.symlink_to(canonical)
    info = os.stat(canonical)
    return [[str(alias), str(canonical), info.st_dev, info.st_ino]]


@_LINUX_ONLY
@pytest.mark.parametrize("cwd", ["safe", "own"], ids=["outside every tree", "the own window"])
def test_a_relative_temp_dir_is_judged_by_the_directory_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path], cwd: str
) -> None:
    """``TMPDIR=.`` names the working directory, and ``tempfile`` returns it as ``.``.

    Outside every tree it is used; in the spawn's own window it is set aside for ``TEMP``.
    """
    t = no_stand_in_root
    monkeypatch.chdir(t[cwd])
    monkeypatch.setenv("TMPDIR", ".")
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        private_dirs=[str(t["own"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == t["safe"]
    assert os.environ["TMPDIR"] == "."


@pytest.fixture
def crew_alias(no_stand_in_root: dict[str, Path]) -> list[list[Any]]:
    return _crew_alias(no_stand_in_root["root"])


def _via_alias(path: Path, aliases: list[list[Any]]) -> str:
    """*path*, under the canonical data home, spelled under the alias instead."""
    alias, canonical = aliases[0][0], aliases[0][1]
    assert str(path).startswith(canonical)
    return alias + str(path)[len(canonical) :]


@_LINUX_ONLY
@pytest.mark.parametrize("where", ["root", "below the root", "the own window"])
def test_a_temp_dir_spelled_through_a_crew_home_alias_is_pinned_away_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    crew_alias: list[list[Any]],
    where: str,
) -> None:
    """The masks are folded onto the canonical spelling, and so is the value checked."""
    t = no_stand_in_root
    temp = {
        "root": t["root"],
        "below the root": t["root"] / "runtime-other",
        "the own window": t["own"],
    }[where]
    spelled = _via_alias(temp, crew_alias)
    assert os.path.isdir(spelled), "the alias reaches the same directory"
    monkeypatch.setenv("TMPDIR", spelled)
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        seal_windowed_masks=[str(t["root"])],
        crew_home_aliases=crew_alias,
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == t["safe"]
    assert os.environ["TMPDIR"] == spelled


@_LINUX_ONLY
def test_a_temp_path_spelled_through_a_crew_home_alias_is_judged_on_the_canonical_spelling(
    tmp_path: Path,
) -> None:
    """Dropped outside the windows and kept inside one, as its canonical spelling would be."""
    scratch = _Scratch(tmp_path)
    aliases = _crew_alias(scratch.root)
    environ = {
        "TMPDIR": _via_alias(scratch.root / "runtime-stale", aliases),
        "TMP": _via_alias(scratch.root, aliases),
        "TEMP": _via_alias(scratch.own, aliases),
        "KIRO_CHAT_LOG_FILE": _via_alias(scratch.tree / "kiro-log" / "kiro-chat.log", aliases),
    }
    kept = {k: environ[k] for k in ("TEMP", "KIRO_CHAT_LOG_FILE")}
    run, _libc = _placed(
        tmp_path,
        scratch,
        environ,
        seal_windowed_masks=[str(scratch.root)],
        crew_home_aliases=aliases,
    )
    program.scrub_env(run)
    assert "TMPDIR" not in environ and "TMP" not in environ
    assert {k: environ[k] for k in kept} == kept


@_LINUX_ONLY
def test_the_written_spelling_is_folded_through_the_recorded_aliases(tmp_path: Path) -> None:
    """Folded as the planner folded the trees, also when the alias names nothing on disk now.

    The pin runs before ``check_crew_home_aliases`` re-reads the aliases, so an alias the
    plan recorded is honoured by spelling while the resolved spelling cannot follow it.
    """
    canonical = tmp_path / "home" / ".kiro" / "crew"
    (canonical / "scratch").mkdir(parents=True)
    gone = str(tmp_path / "home" / ".kirocrew")
    run = launch(tmp_path, payload(crew_home_aliases=[[gone, str(canonical), 0, 0]]))
    groups = (("tree", [str(canonical / "scratch")]),)
    assert program._held_in(run, gone + "/scratch/runtime-x", groups, ("tree",), None) == "tree"
    assert program._held_in(run, gone + "/elsewhere", groups, ("tree",), None) is None


def _linked_data_home(scratch_root: Path) -> Path:
    """A plain symlink to the data home, which no ``crew_home_aliases`` entry names."""
    link = scratch_root.parent.parent.parent.parent / "linked-crew-home"
    link.symlink_to(scratch_root.parent)
    return link


def _via_link(path: Path, link: Path) -> str:
    """*path*, under the data home, spelled through *link* instead."""
    return str(link / path.relative_to(link.resolve()))


@_LINUX_ONLY
@pytest.mark.parametrize(
    "where",
    ["root", "below the root", "the own window", "a link in the root pointing out"],
)
def test_a_temp_dir_spelled_through_a_symlink_is_pinned_away_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path], where: str
) -> None:
    """The kernel follows the link into the tree, so the value is judged as it resolves too.

    A link inside the root that points out is judged as written as well: the stage is made
    and later removed by that name, which the root's mask hides.
    """
    t = no_stand_in_root
    link = _linked_data_home(t["root"])
    out = t["root"] / "runtime-out"
    out.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (tmp_path / "elsewhere").mkdir()
    temp = {
        "root": _via_link(t["root"], link),
        "below the root": _via_link(t["root"] / "runtime-other", link),
        "the own window": _via_link(t["own"], link),
        "a link in the root pointing out": str(out),
    }[where]
    assert os.path.isdir(temp)
    monkeypatch.setenv("TMPDIR", temp)
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == t["safe"]
    assert os.environ["TMPDIR"] == temp


@_LINUX_ONLY
def test_a_mask_spelled_through_a_symlink_holds_a_temp_dir_named_by_its_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """The trees are resolved too, so a plan that spells the data home through a link counts."""
    t = no_stand_in_root
    link = _linked_data_home(t["root"])
    monkeypatch.setenv("TMPDIR", str(t["root"] / "runtime-other"))
    linked_root = _via_link(t["root"], link)
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[linked_root],
        seal_windowed_masks=[linked_root],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == t["safe"]


@_LINUX_ONLY
def test_a_symlink_outside_every_tree_that_points_out_stays_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """A link in the data home, outside every tree, to a directory outside them is used as is."""
    t = no_stand_in_root
    (tmp_path / "elsewhere").mkdir()
    out = t["root"].parent / "tmp-link"
    out.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(out))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == out


@_LINUX_ONLY
def test_a_temp_path_spelled_through_a_symlink_is_judged_as_it_resolves(tmp_path: Path) -> None:
    """Dropped under the sealed root outside the windows, kept in a window or outside."""
    scratch = _Scratch(tmp_path)
    link = _linked_data_home(scratch.root)
    (tmp_path / "elsewhere").mkdir()
    out = scratch.root.parent / "tmp-link"
    out.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    environ = {
        "TMPDIR": _via_link(scratch.root / "runtime-stale", link),
        "TMP": _via_link(scratch.root, link),
        "TEMP": _via_link(scratch.own, link),
        "KIRO_CHAT_LOG_FILE": str(out / "kiro-chat.log"),
    }
    kept = {k: environ[k] for k in ("TEMP", "KIRO_CHAT_LOG_FILE")}
    run, _libc = _placed(tmp_path, scratch, environ, seal_windowed_masks=[str(scratch.root)])
    program.scrub_env(run)
    assert "TMPDIR" not in environ and "TMP" not in environ
    assert {k: environ[k] for k in kept} == kept


def _spelling_world(tmp_path: Path, root: Path, own: Path) -> dict[str, Any]:
    """Every spelling a temp value can take, built around one scratch root and its window.

    The working directory the tests use is the data home, so a relative value is read
    from there.
    """
    crew = root.parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    aliases = _crew_alias(root)
    link = _linked_data_home(root)
    links = {
        "out_in_root": (root / "runtime-out", elsewhere),
        "out_outside": (crew / "tmp-link", elsewhere),
        "into_root": (own / "into-root", root / "runtime-other"),
        "to_root": (elsewhere / "to-root", root),
        "to_own": (elsewhere / "to-own", own),
        "own_out": (own / "to-elsewhere", elsewhere),
    }
    for name, target in links.values():
        name.symlink_to(target, target_is_directory=True)
    return {
        "aliases": aliases,
        "root": str(root),
        "own": str(own),
        "crew": str(crew),
        "elsewhere": str(elsewhere),
        "link_root": _via_link(root, link),
        "link_own": _via_link(own, link),
        "alias_root": _via_alias(root, aliases),
        "alias_own": _via_alias(own, aliases),
        **{key: str(name) for key, (name, _target) in links.items()},
    }


#: ``(class, value template, answer)`` for the stand-in pin, on a host where nothing but the
#: value itself is usable: ``used`` (outside every tree, or a window) or ``refused`` (in a
#: tree, or a directory ``tempfile`` cannot use). Relative values are read from the data home.
_PIN_SPELLINGS = [
    ("outside, absolute", "{elsewhere}", "used"),
    ("outside, relative", "../../../elsewhere", "used"),
    ("outside, dot segments", "{elsewhere}/./", "used"),
    ("outside, a symlink that points out", "{out_outside}", "used"),
    ("root itself", "{root}", "refused"),
    ("root, relative", "scratch/runtime-other", "refused"),
    ("root, dot", "./scratch/.", "refused"),
    ("root, dot-dot out of a window", "scratch/runtime-own/../runtime-other", "refused"),
    ("root, trailing slash", "{root}/runtime-other/", "refused"),
    ("root, doubled slashes", "{root}//runtime-other", "refused"),
    ("root, leading doubled slash", "/{root}/runtime-other", "refused"),
    ("root, crew-home alias", "{alias_root}/runtime-other", "refused"),
    ("root, symlink into the tree", "{link_root}/runtime-other", "refused"),
    ("root, symlink in it pointing out", "{out_in_root}", "refused"),
    ("root, leading doubled slash on a symlink in it pointing out", "/{out_in_root}", "refused"),
    ("root, symlink in a window into it", "{into_root}", "refused"),
    ("root, missing path", "{root}/runtime-missing/sub", "refused"),
    ("root, parent is a symlink into it", "{to_root}/runtime-other", "refused"),
    ("window", "{own}", "used"),
    ("window, relative", "scratch/runtime-own", "used"),
    ("window, trailing slash", "{own}/", "used"),
    ("window, doubled slashes", "{root}//runtime-own", "used"),
    ("window, dot-dot through another window", "scratch/runtime-tree/../runtime-own", "used"),
    ("window, crew-home alias", "{alias_own}", "used"),
    ("window, symlink into the tree", "{link_own}", "used"),
    ("window, parent is a symlink to it", "{to_own}", "used"),
    ("window, symlink in it pointing out", "{own_out}", "used"),
    ("window, missing path", "{own}/missing", "refused"),
]


@_LINUX_ONLY
@pytest.mark.parametrize(
    ("spelling", "value", "answer"), _PIN_SPELLINGS, ids=[r[0] for r in _PIN_SPELLINGS]
)
def test_every_spelling_class_of_the_launchers_temp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    spelling: str,
    value: str,
    answer: str,
) -> None:
    """One rule (``_held_in``) answers every spelling, judged as written and as resolved."""
    t = no_stand_in_root
    world = _spelling_world(tmp_path, t["root"], t["own"])
    monkeypatch.chdir(world["crew"])
    _only_named_temp_dirs(monkeypatch, tmp_path)
    temp = value.format(**world)
    monkeypatch.setenv("TMPDIR", temp)
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"]), str(t["aws"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        seal_windowed_masks=[str(t["root"])],
        crew_home_aliases=world["aliases"],
    )
    if answer == "refused":
        message = refusal(program.pick_stand_in_root, run)
        assert message is not None and "no temporary directory outside the masked" in message
    else:
        program.pick_stand_in_root(run)
        landed = Path(program._stand_in_mkdtemp(run)).parent
        assert os.path.realpath(landed) == os.path.realpath(os.path.join(world["crew"], temp))
    assert os.environ["TMPDIR"] == temp


@_LINUX_ONLY
def test_a_relative_temp_dir_with_no_working_directory_is_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """With the launcher's working directory gone, a relative value has no base: set aside."""
    t = no_stand_in_root
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    monkeypatch.setenv("TMPDIR", "scratch/runtime-other")
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == t["safe"]
    assert os.environ["TMPDIR"] == "scratch/runtime-other"


#: ``(class, value template, exec directory template or None, answer)`` for the env drop
#: after the masks: ``dropped`` (a sealed root outside every window) or ``kept``. Relative
#: values are read from the directory the child starts in: the plan's exec directory when
#: it names one, else the data home the test starts from.
_DROP_SPELLINGS = [
    ("empty", "", None, "kept"),
    ("outside, absolute", "{elsewhere}", None, "kept"),
    ("outside, relative", "../../../elsewhere", None, "kept"),
    ("outside, a symlink that points out", "{out_outside}", None, "kept"),
    (
        "outside, relative from an exec directory outside",
        "scratch/runtime-stale",
        "{elsewhere}",
        "kept",
    ),
    ("root itself", "{root}", None, "dropped"),
    ("root, absolute", "{root}/runtime-stale", None, "dropped"),
    ("root, relative", "scratch/runtime-stale", None, "dropped"),
    ("root, relative from the exec directory", "scratch/runtime-stale", "{crew}", "dropped"),
    ("root, dot from an exec directory in it", ".", "{root}", "dropped"),
    ("root, dot-dot out of a window", "scratch/runtime-own/../runtime-stale", None, "dropped"),
    ("root, trailing slash", "{root}/runtime-stale/", None, "dropped"),
    ("root, doubled slashes", "{root}//runtime-stale", None, "dropped"),
    ("root, leading doubled slash", "/{root}/runtime-stale", None, "dropped"),
    ("root, crew-home alias", "{alias_root}/runtime-stale", None, "dropped"),
    ("root, symlink into the tree", "{link_root}/runtime-stale", None, "dropped"),
    ("root, symlink in it pointing out", "{out_in_root}", None, "dropped"),
    ("root, symlink in a window into it", "{into_root}", None, "dropped"),
    ("root, missing path", "{root}/runtime-missing/sub", None, "dropped"),
    ("root, parent is a symlink into it", "{to_root}/runtime-stale", None, "dropped"),
    ("window", "{own}", None, "kept"),
    ("window, relative", "scratch/runtime-own", None, "kept"),
    ("window, trailing slash", "{own}/", None, "kept"),
    ("window, doubled slashes", "{root}//runtime-own", None, "kept"),
    ("window, dot-dot through another window", "scratch/runtime-tree/../runtime-own", None, "kept"),
    ("window, crew-home alias", "{alias_own}", None, "kept"),
    ("window, symlink into the tree", "{link_own}", None, "kept"),
    ("window, parent is a symlink to it", "{to_own}/sub", None, "kept"),
    ("window, symlink in it pointing out", "{own_out}", None, "kept"),
    ("window, missing path", "{own}/missing/sub", None, "kept"),
]


@_LINUX_ONLY
@pytest.mark.parametrize(
    ("spelling", "value", "exec_cwd", "answer"),
    _DROP_SPELLINGS,
    ids=[r[0] for r in _DROP_SPELLINGS],
)
def test_every_spelling_class_of_an_inherited_temp_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
    value: str,
    exec_cwd: str | None,
    answer: str,
) -> None:
    """The same rule after the masks, read from the directory the child will start in."""
    scratch = _Scratch(tmp_path)
    world = _spelling_world(tmp_path, scratch.root, scratch.own)
    monkeypatch.chdir(world["crew"])
    temp = value.format(**world)
    environ = {"TMPDIR": temp}
    run, _libc = _placed(
        tmp_path,
        scratch,
        environ,
        seal_windowed_masks=[str(scratch.root)],
        crew_home_aliases=world["aliases"],
    )
    if exec_cwd is not None:
        run.exec_cwd = exec_cwd.format(**world)
    program.scrub_env(run)
    assert ("TMPDIR" not in environ) == (answer == "dropped"), environ
    if answer == "kept":
        assert environ["TMPDIR"] == temp


@_LINUX_ONLY
@pytest.mark.parametrize("hide_ssh", [1, 0], ids=["strict tier", "no ssh mask"])
def test_a_temp_dir_under_the_ssh_dir_the_strict_tier_hides_is_pinned_away(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_stand_in_root: dict[str, Path],
    hide_ssh: int,
) -> None:
    """``mask_ssh_keys`` hides ``~/.ssh`` last, so a stage made there would stay in it."""
    t = no_stand_in_root
    ssh_tmp = tmp_path / "home" / ".ssh" / "tmp"
    ssh_tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(ssh_tmp))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        seal_windowed_masks=[str(t["root"])],
        ssh_dir=str(ssh_tmp.parent),
        ssh_known_hosts=str(ssh_tmp.parent / "known_hosts"),
        hide_ssh=hide_ssh,
    )
    program.pick_stand_in_root(run)
    landed = Path(program._stand_in_mkdtemp(run)).parent
    assert landed == (t["safe"] if hide_ssh else ssh_tmp)


def _only_named_temp_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``tempfile`` sees the temp variables, then no usable system default, then the cwd.

    That is a host where ``/tmp``, ``/var/tmp`` and ``/usr/tmp`` are all unwritable.
    """

    def _candidates() -> list[str]:
        named = [os.environ[k] for k in ("TMPDIR", "TEMP", "TMP") if os.environ.get(k)]
        return [*named, str(tmp_path / "no-tmp"), str(tmp_path / "no-var-tmp"), os.getcwd()]

    monkeypatch.setattr(tempfile, "_candidate_tempdir_list", _candidates)
    monkeypatch.delenv("TEMP", raising=False)


@_LINUX_ONLY
def test_with_nothing_else_usable_the_own_window_holds_the_launchers_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path]
) -> None:
    """The window is bound back read-write, so it can hold the stage; it is the last resort.

    ``scratch_env`` names the window in all three variables. A directory outside every
    tree still wins when one is usable (``test_the_launchers_own_temp_dir_is_outside_...``).
    """
    t = no_stand_in_root
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    _only_named_temp_dirs(monkeypatch, tmp_path)
    for key in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(key, str(t["own"]))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=[str(t["root"])],
        private_dirs=[str(t["own"]), str(t["tree"])],
        seal_windowed_masks=[str(t["root"])],
    )
    program.pick_stand_in_root(run)
    assert Path(program._stand_in_mkdtemp(run)).parent == t["own"]
    assert os.listdir(project) == []


@_LINUX_ONLY
@pytest.mark.parametrize(
    "where",
    [
        "no window",
        "a mask inside the window",
        "a linked window",
        "a link in the window into the root",
    ],
)
def test_with_nothing_else_usable_no_other_tree_and_not_the_cwd_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_stand_in_root: dict[str, Path], where: str
) -> None:
    """Refused rather than placed in a tree, or in the user's project through the cwd."""
    t = no_stand_in_root
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    _only_named_temp_dirs(monkeypatch, tmp_path)
    nested = t["own"] / "hidden"
    nested.mkdir()
    linked = t["root"] / "runtime-linked"
    linked.symlink_to(t["safe"])
    # Written inside the window, resolved under the root outside it: the tree decides.
    into_root = t["own"] / "into-root"
    into_root.symlink_to(t["root"] / "runtime-other", target_is_directory=True)
    temp, sensitive = {
        "no window": (t["root"] / "runtime-other", [str(t["root"])]),
        "a mask inside the window": (nested, [str(t["root"]), str(nested)]),
        "a linked window": (linked, [str(t["root"])]),
        "a link in the window into the root": (into_root, [str(t["root"])]),
    }[where]
    monkeypatch.setenv("TMPDIR", str(temp))
    run = _no_root_launch(
        tmp_path,
        CoveringLibc(),
        sensitive_dirs=sensitive,
        private_dirs=[str(t["own"]), str(linked)],
        seal_windowed_masks=[str(t["root"])],
    )
    message = refusal(program.pick_stand_in_root, run)
    assert message is not None and "no temporary directory outside the masked" in message
    assert os.listdir(project) == []
    assert os.environ["TMPDIR"] == str(temp)


@_LINUX_ONLY
@pytest.mark.parametrize(
    "window, value",
    [
        ("{root}//runtime-own", "{root}//runtime-own"),
        ("{root}/./runtime-own", "{root}/runtime-own/sub/.."),
        ("{root}/runtime-own", "{root}//runtime-own"),
    ],
)
def test_a_bound_window_spelled_another_way_keeps_its_temp(
    tmp_path: Path, window: str, value: str
) -> None:
    """Windows and roots are compared normalized, as the value is."""
    scratch = _Scratch(tmp_path)
    root = str(scratch.root)
    spelled = window.format(root=root)
    environ = {"TMPDIR": value.format(root=root)}
    kept = dict(environ)
    run, _libc = _placed(
        tmp_path,
        scratch,
        environ,
        private_dirs=[spelled],
        seal_windowed_masks=[root + "/"],
        sensitive_dirs=[root + "/"],
    )
    assert run.bound_windows, "the window was bound"
    program.scrub_env(run)
    assert environ["TMPDIR"] == kept["TMPDIR"]


@_LINUX_ONLY
def test_a_sealed_root_spelled_another_way_still_drops_a_stale_temp(tmp_path: Path) -> None:
    scratch = _Scratch(tmp_path)
    spelled = str(scratch.root) + "/./"
    environ = {"TMPDIR": str(scratch.root / "runtime-stale")}
    run, libc = _placed(
        tmp_path,
        scratch,
        environ,
        private_dirs=[],
        sensitive_dirs=[spelled],
        seal_windowed_masks=[spelled],
    )
    assert _read_only_remounts(libc), "the root was sealed"
    program.scrub_env(run)
    assert "TMPDIR" not in environ
