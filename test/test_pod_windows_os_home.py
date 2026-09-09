"""The win32 pod OS home, and a whole ``pod up`` driven through the win32 branches.

``_seed_pod_os_home`` gates on ``pinned_fs.supports_pinned_walk()``, which is
False on Windows because that platform has no ``O_DIRECTORY``/``O_NOFOLLOW`` and
no ``dir_fd``. The branch that runs instead is otherwise pure Python over
``platform_compat.pin_directory``, so these tests force that gate False and
``IS_WINDOWS`` True and run the real code on ``tmp_path``.

The last test walks ``pod up`` end to end with only the service-manager and
process-spawn boundaries mocked, which is what catches the next platform refusal
sitting behind the one just fixed.

What only a real windows-latest run can establish: whether ``pin_directory``'s
``CreateFileW`` really refuses a junction and really blocks a rename of the
directories above it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.identity_stores import Product, StoreMapping
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import PodConfig


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    c = PodConfig.load()
    c.pods_dir.mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def windows_os_home(monkeypatch):
    """Force the win32 branch: no pinned walk, and IS_WINDOWS true."""
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)


# --------------------------------------------------------------------------
# The refusal that is NOT relaxed
# --------------------------------------------------------------------------
def test_a_non_windows_host_without_a_pinned_walk_still_refuses(tmp_path, monkeypatch):
    """Only win32 gets the pin_directory branch; every other such host refuses.

    That refusal is the only thing standing between the pod's kiro-cli and the
    real host tree a planted link points at, so a POSIX host that somehow lacks
    the walk must not inherit the Windows compromise.
    """
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    with pytest.raises(rt.PodError, match="this platform provides none"):
        rt._seed_pod_os_home(tmp_path / "os-home")


def test_a_pinned_host_never_reaches_the_windows_branch(tmp_path, monkeypatch):
    """A host WITH the pinned walk keeps the POSIX path, IS_WINDOWS notwithstanding."""
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(
        rt,
        "_seed_pod_os_home_windows",
        lambda *a: pytest.fail("the pinned host must not divert"),
    )
    if not pinned_fs.supports_pinned_walk():
        pytest.skip("this host has no pinned walk, so the POSIX branch cannot run here")
    rt._seed_pod_os_home(tmp_path / "os-home")
    assert (tmp_path / "os-home" / ".aws" / "sso" / "cache").is_dir()


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------
def test_a_fresh_os_home_is_built_to_the_grant_corridor(tmp_path, windows_os_home):
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    assert (os_home / ".aws" / "sso" / "cache").is_dir()


def test_the_grant_cache_is_created_empty(tmp_path, windows_os_home):
    """Nothing host-derived is ever staged there, on any platform."""
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    assert list((os_home / ".aws" / "sso" / "cache").iterdir()) == []


def test_building_it_twice_is_a_no_op(tmp_path, windows_os_home):
    """It runs again on every boot, so a second call must not fail or clobber."""
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    (os_home / ".aws" / "sso" / "cache" / "grant.json").write_text("{}")
    rt._seed_pod_os_home(os_home)
    assert (os_home / ".aws" / "sso" / "cache" / "grant.json").read_text() == "{}"


def test_every_level_is_pinned_while_the_tree_is_built(tmp_path, windows_os_home, monkeypatch):
    """The pin is the guarantee, so the call is pinned by argv and by order.

    A handle held on each level blocks a rename or a delete of it and of
    everything above it, which is what stops a component being swapped after it is
    checked. The pinned chain therefore starts at the deepest ancestor that
    already existed and runs unbroken down to the grant cache — derived from the
    paths rather than hardcoded, so the assertion cannot drift from the tree the
    function actually builds.
    """
    pinned: list[str] = []
    real = rt.pin_directory

    def spy(path):
        pinned.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(rt, "pin_directory", spy)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [])
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    # tmp_path exists, `pod` does not, so the anchor is tmp_path itself.
    expected = [tmp_path.name, "pod", "os-home", ".aws", "sso", "cache"]
    assert pinned == expected


def test_the_first_pin_is_taken_before_anything_is_created(tmp_path, windows_os_home, monkeypatch):
    """The ORDER is the guarantee, and the outermost level is where it is easiest to lose.

    Every level below the outermost inherits a parent pin. The outermost has none to
    inherit, so it is the one level where "create under a pinned parent" has to be
    arranged deliberately: a by-name ancestor screen followed by
    ``mkdir(parents=True)`` walks and creates through directories nothing is holding,
    and a same-UID process that swaps one in that window has the pod's whole OAuth
    grant corridor built inside its directory. Nothing detects that either -- the
    ``fd_real_path`` witness compares a value only against itself, so it cannot see a
    swap that landed before the pin existed.

    Asserted as a property rather than as a race: at the moment the FIRST pin is
    taken, none of the levels this function is about to create may exist yet. A
    pin taken after a creation is a pin that came too late.
    """
    os_home = tmp_path / "pod" / "os-home"
    created_before_first_pin: list[str] = []
    first_pin: list[Path] = []
    real = rt.pin_directory

    def spy(path):
        if not first_pin:
            first_pin.append(Path(path))
            for level in (os_home.parent, os_home, os_home / ".aws"):
                if level.exists():
                    created_before_first_pin.append(str(level))
        return real(path)

    monkeypatch.setattr(rt, "pin_directory", spy)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [])
    rt._seed_pod_os_home(os_home)

    assert first_pin, "the tree must be built under a pin, so one must be taken"
    assert created_before_first_pin == [], (
        "these levels existed before the first pin was taken, so they were created "
        "under a parent nothing was holding"
    )
    # And the pin that came first is an ANCESTOR of everything it protects, which
    # is what makes it freeze the whole chain above the tree being built.
    assert first_pin[0] == tmp_path, first_pin


def test_every_pinned_handle_is_closed(tmp_path, windows_os_home, monkeypatch):
    """Held until done, then released in one place: a leak is a per-boot leak."""
    closed: list[list[int]] = []
    real = pinned_fs.close_all
    monkeypatch.setattr(
        rt.pinned_fs, "close_all", lambda fds: closed.append(list(fds)) or real(fds)
    )
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [])
    rt._seed_pod_os_home(tmp_path / "pod" / "os-home")
    assert closed, "the handles must be closed through close_all"
    # The anchor, the two levels created down to the os-home, and the three grant
    # corridor levels: derived from the chain, not a magic number.
    assert len(closed[-1]) == 6, closed


# --------------------------------------------------------------------------
# Reparse-point screening
# --------------------------------------------------------------------------
def test_a_reparse_point_above_the_os_home_is_refused(tmp_path, windows_os_home, monkeypatch):
    """Screened before anything is created, so the tree cannot land through a link."""
    os_home = tmp_path / "pod" / "os-home"
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == os_home.parent)
    with pytest.raises(rt.PodError, match="symbolic link or a junction"):
        rt._seed_pod_os_home(os_home)
    assert not os_home.exists()


def test_a_reparse_point_at_the_os_home_itself_is_refused(tmp_path, windows_os_home, monkeypatch):
    os_home = tmp_path / "pod" / "os-home"
    os_home.parent.mkdir(parents=True)
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == os_home)
    with pytest.raises(rt.PodError, match="symbolic link or a junction"):
        rt._seed_pod_os_home(os_home)


@pytest.mark.parametrize("level", [".aws", "sso", "cache"])
def test_a_reparse_point_at_any_inner_level_is_refused(
    tmp_path, windows_os_home, monkeypatch, level
):
    """The credential corridor is only as good as its weakest component."""
    os_home = tmp_path / "pod" / "os-home"
    seen: list[str] = []

    def fake_is_reparse(path):
        p = Path(path)
        seen.append(p.name)
        return p.name == level and os_home in p.parents or (p.name == level and p.parent == os_home)

    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", fake_is_reparse)
    with pytest.raises(rt.PodError, match="symbolic link or a junction"):
        rt._seed_pod_os_home(os_home)


def test_the_inner_screen_runs_before_the_mkdir(tmp_path, windows_os_home, monkeypatch):
    """A screen after the create would already have written through the link."""
    parent = tmp_path / "held"
    parent.mkdir()
    child = parent / ".aws"
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == child)
    parent_fd = platform_compat.pin_directory(parent)
    try:
        with pytest.raises(rt.PodError, match="symbolic link or a junction"):
            rt._pin_created_dir_windows(parent_fd, child, what="probe")
    finally:
        os.close(parent_fd)
    assert not child.exists()


def test_creating_a_level_without_a_pinned_parent_is_refused(tmp_path):
    """The parent pin is a precondition, not an assumption a caller may skip."""
    with pytest.raises(rt.PodError, match="without a pinned parent"):
        rt._pin_created_dir_windows(-1, tmp_path / "x", what="probe")


# --------------------------------------------------------------------------
# Staging the sign-in material stays best-effort
# --------------------------------------------------------------------------
def _store(tmp_path: Path) -> StoreMapping:
    source = tmp_path / "host-store"
    (source / "inner").mkdir(parents=True)
    (source / "token.json").write_text('{"accessToken": "abc"}')
    (source / "inner" / "extra.json").write_text("{}")
    return StoreMapping(
        source=source,
        staged_relative=Path(".local/share/kiro-cli"),
        product=Product.KIRO_CLI,
    )


def test_the_runtime_store_is_staged_through_pinned_sources(tmp_path, windows_os_home, monkeypatch):
    mapping = _store(tmp_path)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [mapping])
    calls: list[dict] = []
    real = pinned_fs.copy_file_pinned

    def spy(by_name, dst=None, **kwargs):
        calls.append({"dst": dst, "src_fd": kwargs.get("src_fd"), "dir_fd": kwargs.get("dir_fd")})
        return real(by_name, dst, **kwargs)

    monkeypatch.setattr(rt.pinned_fs, "copy_file_pinned", spy)
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)

    staged_root = os_home / ".local" / "share" / "kiro-cli"
    assert (staged_root / "token.json").read_text() == '{"accessToken": "abc"}'
    assert (staged_root / "inner" / "extra.json").is_file()
    assert calls, "the store must be copied through copy_file_pinned"
    for call in calls:
        assert isinstance(call["src_fd"], int), "the source must be a pinned descriptor"
        assert call["dir_fd"] is None, "win32 has no dir_fd; a dir_fd form would be a lie"


def test_the_staged_copy_is_create_only(tmp_path, windows_os_home, monkeypatch):
    """A pod that already refreshed its own credential is never clobbered."""
    mapping = _store(tmp_path)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [mapping])
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    staged = os_home / ".local" / "share" / "kiro-cli" / "token.json"
    staged.write_text('{"accessToken": "pod-refreshed"}')
    rt._seed_pod_os_home(os_home)
    assert staged.read_text() == '{"accessToken": "pod-refreshed"}'


def test_a_token_copy_failure_leaves_the_pod_booting_signed_out(
    tmp_path, windows_os_home, monkeypatch
):
    """Building the tree is mandatory; staging is not. The boot must survive.

    A signed-out pod can prompt for sign-in inside itself, which is strictly
    better than refusing to boot at all.
    """
    mapping = _store(tmp_path)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [mapping])
    monkeypatch.setattr(
        rt.pinned_fs,
        "copy_file_pinned",
        lambda *a, **k: (_ for _ in ()).throw(OSError("access is denied")),
    )
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    assert (os_home / ".aws" / "sso" / "cache").is_dir(), "the corridor must still exist"
    assert not (os_home / ".local" / "share" / "kiro-cli" / "token.json").exists()


def test_an_absent_host_store_stages_nothing_and_does_not_raise(
    tmp_path, windows_os_home, monkeypatch
):
    mapping = StoreMapping(
        source=tmp_path / "no-such-store",
        staged_relative=Path(".local/share/kiro-cli"),
        product=Product.KIRO_CLI,
    )
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [mapping])
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    assert rt._stage_runtime_auth_store_windows(os_home, mapping) == 0


def test_a_host_store_behind_a_reparse_point_stages_nothing(tmp_path, monkeypatch):
    mapping = _store(tmp_path)
    monkeypatch.setattr(rt.pinned_fs, "is_reparse_point", lambda p: Path(p) == mapping.source)
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    assert rt._stage_runtime_auth_store_windows(os_home, mapping) == 0


def test_a_sqlite_store_stages_nothing_rather_than_a_torn_copy(
    tmp_path, windows_os_home, monkeypatch
):
    """A per-file copy of a live database cannot be consistent, so it is skipped."""
    source = tmp_path / "host-store"
    source.mkdir()
    (source / "auth.sqlite3").write_bytes(b"SQLite format 3\x00")
    (source / "auth.sqlite3-wal").write_bytes(b"wal")
    mapping = StoreMapping(
        source=source,
        staged_relative=Path(".local/share/kiro-cli"),
        product=Product.KIRO_CLI,
    )
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [mapping])
    os_home = tmp_path / "pod" / "os-home"
    rt._seed_pod_os_home(os_home)
    staged_root = os_home / ".local" / "share" / "kiro-cli"
    assert list(staged_root.iterdir()) == [], "no database and no sidecar may be staged"


def test_the_file_cap_stops_a_runaway_store(tmp_path, windows_os_home, monkeypatch):
    source = tmp_path / "host-store"
    source.mkdir()
    for i in range(rt._RUNTIME_AUTH_STORE_FILE_CAP + 5):
        (source / f"f{i:04d}.json").write_text("{}")
    mapping = StoreMapping(
        source=source,
        staged_relative=Path(".local/share/kiro-cli"),
        product=Product.KIRO_CLI,
    )
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    staged = rt._stage_runtime_auth_store_windows(os_home, mapping)
    assert staged == rt._RUNTIME_AUTH_STORE_FILE_CAP


# --------------------------------------------------------------------------
# A whole `pod up` through the win32 branches
# --------------------------------------------------------------------------
def test_pod_up_boots_end_to_end_under_the_windows_branches(cfg, tmp_path, monkeypatch, capsys):
    """``boot`` from a pinned checkout to the gateway spawn, all win32 paths live.

    Only two boundaries are mocked, and both are genuinely unavailable here: the
    service manager (``schtasks``) and the gateway spawn. Everything between --
    name validation, the checkout pin, the seed, the config floor, the OS home,
    the env build -- runs the real Windows code, which is what surfaces the next
    platform refusal sitting behind the one just fixed.
    """
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt, "IS_LINUX", False)
    monkeypatch.setattr(rt, "IS_MACOS", False)

    checkout = tmp_path / "worktree"
    bin_dir = checkout / ".venv" / "Scripts"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "kirocrew.exe"
    binary.write_text("stub")
    binary.chmod(0o700)
    (checkout / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
    rt.pin_checkout(cfg, "demo", checkout)

    monkeypatch.setattr(rt, "resolve_checkout", lambda c, n: checkout)
    monkeypatch.setattr(rt.prov, "venv_bin", lambda c: binary)
    monkeypatch.setattr(rt, "target_supports_flag", lambda c, f: True)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [])

    spawned: dict[str, object] = {}

    def fake_supervise(c, name, bin_path, argv, env, *, gateway_pid_record=None):
        spawned["bin"] = str(bin_path)
        spawned["argv"] = list(argv)
        spawned["home"] = env.get("KIROCREW_HOME")
        spawned["os_home"] = env.get("KIROCREW_OS_HOME")
        # Captured, not merely tolerated: the supervisor cannot adopt a restart
        # successor without the pod's own gateway pid sidecar, so a boot that
        # stopped passing it would silently lose restart tracking. Asserted below.
        spawned["gateway_pid_record"] = gateway_pid_record
        return 0

    monkeypatch.setattr(rt.win_backend, "supervise_gateway", fake_supervise)
    monkeypatch.setattr(rt.win_backend, "schtasks", lambda *a: _cp())

    rc = rt.boot(cfg, "demo")

    out = capsys.readouterr().out
    assert rc == 0, f"boot refused: {out}"
    assert spawned["bin"] == str(binary)
    assert "gateway" in spawned["argv"] and "--no-crons" in spawned["argv"]
    assert spawned["gateway_pid_record"] is not None, (
        "the boot must hand the supervisor the pod's gateway pid sidecar; without it "
        "a restart successor cannot be identified and the pod reads as stopped while "
        "it is still serving"
    )
    home = Path(str(spawned["home"]))
    assert home == cfg.home_dir("demo")
    os_home = Path(str(spawned["os_home"]))
    assert os_home == home / "os-home"
    assert (os_home / ".aws" / "sso" / "cache").is_dir(), "the grant corridor must be built"
    config = json.loads((home / "config.json").read_text())
    for section in rt.SEED_DISABLED_SECTIONS:
        node = config.get(section)
        if isinstance(node, dict) and "enabled" in node:
            assert node["enabled"] is False, section


def test_pod_up_boots_end_to_end_with_a_seeded_scenario(cfg, tmp_path, monkeypatch, capsys):
    """The same walk with ``--seed``, so the seed and the OS home run together."""
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(rt.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt, "IS_LINUX", False)
    monkeypatch.setattr(rt, "IS_MACOS", False)

    checkout = tmp_path / "worktree"
    bin_dir = checkout / ".venv" / "Scripts"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "kirocrew.exe"
    binary.write_text("stub")
    binary.chmod(0o700)
    (checkout / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
    rt.pin_checkout(cfg, "demo", checkout)
    rt.write_env_file(cfg, "demo", {"SEED": "minimal"})

    monkeypatch.setattr(rt, "resolve_checkout", lambda c, n: checkout)
    monkeypatch.setattr(rt.prov, "venv_bin", lambda c: binary)
    monkeypatch.setattr(rt, "target_supports_flag", lambda c, f: True)
    monkeypatch.setattr(rt, "_runtime_auth_store_mappings", lambda: [])
    monkeypatch.setattr(rt.win_backend, "supervise_gateway", lambda *a, **k: 0)
    monkeypatch.setattr(rt.win_backend, "schtasks", lambda *a: _cp())

    rc = rt.boot(cfg, "demo")
    out = capsys.readouterr().out
    assert rc == 0, f"boot refused: {out}"
    home = cfg.home_dir("demo")
    assert rt._seeded_scenario_in_dir(home) == "minimal"
    assert (home / "os-home" / ".aws" / "sso" / "cache").is_dir()
    assert win.pid_record_path(cfg, "demo").exists() is False, "the wrapper clears its record"
