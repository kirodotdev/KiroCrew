"""A packaged gateway's interpreter environment is sealed read-only for sandboxed children.

The gateway and every Python child it starts outside the sandbox load the venv
(``site-packages``, ``.pth`` files, ``__pycache__``, ``pyvenv.cfg``), the base
stdlib and every other directory on the interpreter's search path at start. A
sandboxed child that could write them would choose code the gateway runs with its
own identity. These directories ride the install-root seal on both backends, rename
guards included. A gateway-initiated install (memory embeddings setup) lifts them
for its own spawn with ``gateway_env_writable``; the ``kiro_crew`` package directory
stays sealed even then.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_env_scan, sandbox_plan, sandbox_tiers
from kiro_crew.sandbox_plan import BACKEND_NAMESPACE, BACKEND_SEATBELT, SandboxRequest
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")
# Several tests start real interpreters and one builds a module-scoped venv: one worker.
pytestmark = pytest.mark.xdist_group(name="interpreter_env_seal")

ENV = "/opt/crew-venv"
BASE = "/opt/python/3.12.13"
INSTALL = f"{ENV}/lib/python3.12/site-packages/kiro_crew"


def _env_targets(
    tmp_path: Path,
    prefixes: tuple[str, ...],
    homes=("/nonexistent-home",),
    path_dirs: tuple[str, ...] = (),
    site_dirs: tuple[str, ...] = (),
    executables: tuple[str, ...] = (),
):
    package = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    package.mkdir(parents=True, exist_ok=True)
    return sandbox._interpreter_env_targets_cached.__wrapped__(
        str(package), prefixes, path_dirs, site_dirs, homes, executables
    )


@pytest.fixture(autouse=True)
def _no_host_libpython(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic layouts never see the libpython the test runner itself mapped."""
    _floor_monkeypatch.setattr(sandbox_env_scan, "_mapped_libpython", lambda: [])


def _mkvenv(path: Path) -> Path:
    """A directory that reads as a venv prefix (``pyvenv.cfg``), sealed whole."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyvenv.cfg").write_text("home = /x/bin\n", encoding="utf-8")
    return path


@pytest.fixture
def no_conda_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No conda setting from the host leaks into a case: a fake home, no env vars."""
    for var in ("CONDA_PKGS_DIRS", "CONDA_ENVS_PATH", "CONDA_ENVS_DIRS", "CONDARC"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


# --------------------------------------------------------------------------- #
# Which prefixes are sealed.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_the_four_interpreter_prefixes_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = {name: tmp_path / name for name in ("p", "ep", "bp", "bep")}
    for path in dirs.values():
        _mkvenv(path)
    # The real sys module's attributes, not a stand-in module: sys is bound by more
    # than one sandbox owner, so it is never patched on one of them.
    monkeypatch.setattr(sys, "prefix", str(dirs["p"]))
    monkeypatch.setattr(sys, "exec_prefix", str(dirs["ep"]))
    monkeypatch.setattr(sys, "base_prefix", str(dirs["bp"]))
    monkeypatch.setattr(sys, "base_exec_prefix", str(dirs["bep"]))
    monkeypatch.setattr(sandbox_tiers, "_gateway_start_sys_path", lambda: ())
    import site

    # No real site dir reaches the resolver, so nothing outside tmp_path is created.
    monkeypatch.setattr(site, "getsitepackages", lambda *a: [])
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(
        sandbox_tiers, "_gateway_package_dir", lambda: f"{ENV}/lib/site-packages/kiro_crew"
    )
    sandbox._interpreter_env_targets_cached.cache_clear()
    try:
        targets = sandbox._resolved_interpreter_env_targets()
    finally:
        sandbox._interpreter_env_targets_cached.cache_clear()
    assert targets[:4] == [str(dirs[name]) for name in ("p", "ep", "bp", "bep")]


@_POSIX_ONLY
def test_a_linked_venv_is_sealed_in_both_spellings(tmp_path: Path) -> None:
    real = _mkvenv(tmp_path / "crew-venv-2")
    current = tmp_path / "crew-venv-current"
    current.symlink_to(real, target_is_directory=True)
    assert _env_targets(tmp_path, (str(current), str(current))) == (str(current), str(real))


@_POSIX_ONLY
def test_an_editable_checkout_seals_no_prefix(tmp_path: Path) -> None:
    source = tmp_path / "checkout" / "src" / "kiro_crew"
    source.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    targets = sandbox._interpreter_env_targets_cached.__wrapped__(
        str(source), (str(tmp_path),), (str(other),), (str(other),), ("/nonexistent-home",)
    )
    assert targets == ()


@_POSIX_ONLY
def test_a_prefix_this_uid_cannot_write_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv, system = _mkvenv(tmp_path / "venv"), _mkvenv(tmp_path / "usr")
    real_access = os.access
    monkeypatch.setattr(
        os, "access", lambda p, mode: False if str(p) == str(system) else real_access(p, mode)
    )
    other_uid = os.getuid() + 1  # neither writable nor owned, like a root-owned /usr
    monkeypatch.setattr(os, "getuid", lambda: other_uid)
    assert _env_targets(tmp_path, (str(venv), str(system))) == (str(venv),)


@_POSIX_ONLY
def test_an_owned_read_only_dir_on_sys_path_is_sealed(tmp_path: Path) -> None:
    # The owner can chmod it back, so read-only now does not mean read-only later.
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    site_dir.chmod(0o555)
    try:
        targets = _env_targets(tmp_path, (), path_dirs=(str(site_dir),))
    finally:
        site_dir.chmod(0o755)
    assert targets == (str(site_dir),)


@_POSIX_ONLY
def test_a_prefix_holding_home_is_skipped(tmp_path: Path) -> None:
    home = tmp_path / "home" / "u"
    home.mkdir(parents=True)
    venv = _mkvenv(home / ".venv")
    prefixes = (str(tmp_path / "home"), str(home), str(venv))
    assert _env_targets(tmp_path, prefixes, homes=(str(home),)) == (str(venv),)


@_POSIX_ONLY
def test_a_prefix_holding_the_cwd_is_not_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv, other = _mkvenv(tmp_path / "venv"), _mkvenv(tmp_path / "other")
    (venv / "work").mkdir()
    monkeypatch.setattr(os, "getcwd", lambda: str(venv / "work"))
    assert _env_targets(tmp_path, (str(venv), str(other))) == (str(other),)


@_POSIX_ONLY
def test_an_alias_of_the_cwd_on_sys_path_is_not_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "current"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setattr(os, "getcwd", lambda: str(project))
    assert _env_targets(tmp_path, (), path_dirs=(str(alias),)) == ()


@_POSIX_ONLY
def test_a_missing_entry_inside_an_excluded_prefix_is_still_created_and_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / "venv"
    (venv / "work").mkdir(parents=True)
    monkeypatch.setattr(os, "getcwd", lambda: str(venv / "work"))
    gone = venv / "lib" / "extra"
    targets = _env_targets(tmp_path, (str(venv),), path_dirs=(str(gone),))
    assert str(venv) not in targets
    assert str(gone) in targets and gone.is_dir()


def test_only_the_site_dirs_this_interpreter_uses_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A venv without system site-packages does not read its base's .pth files, and
    # site.getsitepackages() with no argument says so; passing prefixes would not.
    import site

    calls: list[tuple] = []
    monkeypatch.setattr(site, "getsitepackages", lambda *a: calls.append(a) or [])
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    sandbox._interpreter_env_targets_cached.cache_clear()
    try:
        sandbox._resolved_interpreter_env_targets()
    finally:
        sandbox._interpreter_env_targets_cached.cache_clear()
    assert calls == [()]


def test_the_resolver_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise RuntimeError("no package")

    monkeypatch.setattr(sandbox_tiers, "_gateway_package_dir", _boom)
    assert sandbox._resolved_interpreter_env_targets() == []


# --------------------------------------------------------------------------- #
# Every directory on the search path is sealed, .pth-added ones included.
# --------------------------------------------------------------------------- #


def _pipx_layout(root: Path) -> tuple[Path, Path, Path]:
    """pipx's layout: the gateway venv's ``site-packages`` holds ``pipx_shared.pth``
    naming ``$PIPX_HOME/shared/.../site-packages``, which is on ``sys.path`` but in
    no prefix. Returns ``(venv, venv_site, shared_site)``."""
    venv = root / "pipx" / "venvs" / "kiro-crew"
    venv_site = venv / "lib" / "python3.12" / "site-packages"
    shared_site = root / "pipx" / "shared" / "lib" / "python3.12" / "site-packages"
    venv_site.mkdir(parents=True)
    shared_site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /x/bin\n", encoding="utf-8")
    (venv_site / "pipx_shared.pth").write_text(f"{shared_site}\n", encoding="utf-8")
    return venv, venv_site, shared_site


def _sealed(path: Path, targets: tuple[str, ...]) -> bool:
    return any(os.path.commonpath((str(path), t)) == t for t in targets)


@_POSIX_ONLY
def test_a_pth_added_dir_is_sealed_where_sitecustomize_would_be_planted(
    tmp_path: Path,
) -> None:
    venv, venv_site, shared_site = _pipx_layout(tmp_path)
    outside = tmp_path / "pipx"  # the venv's prefix does not reach the shared dir
    assert not _sealed(shared_site, (str(venv),))
    # Gateway started with -S or before site ran: only the site dir is known.
    targets = _env_targets(tmp_path, (str(venv),), site_dirs=(str(venv_site),))
    assert _sealed(shared_site / "sitecustomize.py", targets)
    assert str(outside) not in targets
    # And as the running gateway sees it: the .pth already put it on sys.path.
    on_path = _env_targets(tmp_path, (str(venv),), path_dirs=(str(shared_site),))
    assert _sealed(shared_site / "sitecustomize.py", on_path)


def test_pth_lines_are_read_as_site_reads_them(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    (site_dir / "rel").mkdir(parents=True)
    elsewhere = tmp_path / "abs" / "elsewhere"
    (site_dir / "a.pth").write_text(
        f"# comment\nimport os; os.getcwd()\n\nrel\n{elsewhere}\n", encoding="utf-8"
    )
    (site_dir / "b.txt").write_text("ignored\n", encoding="utf-8")
    (site_dir / ".hidden.pth").write_text(f"{tmp_path / 'hidden'}\n", encoding="utf-8")
    assert sandbox_env_scan._pth_dirs((str(site_dir), str(tmp_path / "gone"))) == [
        str(site_dir / "rel"),
        str(elsewhere),
    ]


@_POSIX_ONLY
def test_an_existing_zip_on_sys_path_is_sealed_as_a_file(tmp_path: Path) -> None:
    # zipimport loads modules from a zip on sys.path, so a writable one is a hook.
    zipped = tmp_path / "python312.zip"
    zipped.write_bytes(b"")
    targets = _env_targets(tmp_path, (), path_dirs=(str(zipped), ""))
    assert targets == (str(zipped),)


@_POSIX_ONLY
def test_a_missing_zip_entry_becomes_an_empty_sealed_file(tmp_path: Path) -> None:
    import subprocess
    import zipfile

    lib = tmp_path / "base" / "lib"
    lib.mkdir(parents=True)
    entry = lib / "python312.zip"
    targets = _env_targets(tmp_path, (), path_dirs=(str(entry),))
    assert entry.is_file() and not entry.is_dir() and entry.stat().st_size == 0
    assert targets == (str(entry),)
    # The empty placeholder loads nothing; the same name holding a real archive would.
    probe = "import sys; sys.path.insert(0, sys.argv[1]); import zzplanted"
    run = [sys.executable, "-I", "-c", probe, str(entry)]
    kw = dict(capture_output=True, timeout=FALLBACK_TIMEOUT_SECS)
    assert subprocess.run(run, **kw).returncode != 0
    real = tmp_path / "real.zip"
    with zipfile.ZipFile(real, "w") as archive:
        archive.writestr("zzplanted.py", "")
    assert subprocess.run([*run[:-1], str(real)], **kw).returncode == 0


@_POSIX_ONLY
def test_a_zip_entry_created_by_a_concurrent_pass_is_still_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seen missing, then created by the other pass before this one's exclusive create.
    entry = tmp_path / "base" / "lib" / "python312.zip"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"")
    real_lexists = os.path.lexists
    monkeypatch.setattr(os.path, "lexists", lambda p: p != str(entry) and real_lexists(p))
    assert _env_targets(tmp_path, (), path_dirs=(str(entry),)) == (str(entry),)


@_POSIX_ONLY
def test_a_missing_zip_entry_under_a_read_only_parent_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "usr" / "lib"
    lib.mkdir(parents=True)
    monkeypatch.setattr(sandbox_env_scan, "_writable_or_owned", lambda p: False)
    assert _env_targets(tmp_path, (), path_dirs=(str(lib / "python312.zip"),)) == ()
    assert list(lib.iterdir()) == []


@_POSIX_ONLY
def test_a_stale_pth_target_is_created_and_sealed_alone(tmp_path: Path) -> None:
    site_dir = tmp_path / "venv-site"
    site_dir.mkdir()
    projects = tmp_path / "projects"
    workspace = projects / "app"
    workspace.mkdir(parents=True)
    gone = projects / "removed" / "src"
    (site_dir / "_editable.pth").write_text(f"{gone}\n", encoding="utf-8")
    targets = _env_targets(tmp_path, (), site_dirs=(str(site_dir),))
    assert gone.is_dir()
    assert str(gone) in targets
    assert not _sealed(workspace, targets)
    assert str(projects) not in targets and str(projects / "removed") not in targets


@_POSIX_ONLY
def test_a_missing_user_site_is_created_and_sealed_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local = home / ".local"
    local.mkdir(parents=True)
    user_site = local / "lib" / "python3.12" / "site-packages"
    targets = _env_targets(tmp_path, (), homes=(str(home),), site_dirs=(str(user_site),))
    assert targets == (str(user_site),)


@_POSIX_ONLY
def test_a_missing_entry_under_a_sticky_dir_never_seals_that_dir(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    entry = shared / "not-yet"
    targets = _env_targets(tmp_path, (), path_dirs=(str(entry),))
    assert str(shared) not in targets
    assert targets == (str(entry),)


@_POSIX_ONLY
def test_a_dangling_link_into_a_sticky_dir_seals_only_the_created_dir(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    link = tmp_path / "link"
    link.symlink_to(shared / "target", target_is_directory=True)
    targets = _env_targets(tmp_path, (), path_dirs=(str(link / "lib"),))
    assert str(shared) not in targets
    assert str(shared / "target" / "lib") in targets


@_POSIX_ONLY
def test_a_missing_entry_that_cannot_be_created_warns_and_stays_unsealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _refuse(path, *args, **kwargs):
        raise PermissionError(path)

    monkeypatch.setattr(os, "makedirs", _refuse)
    entry = tmp_path / "libs" / "not-yet"
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        targets = _env_targets(tmp_path, (), path_dirs=(str(entry),))
    assert targets == ()
    warned = [r.getMessage() for r in caplog.records if "could not create" in r.getMessage()]
    assert len(warned) == 1 and str(entry) in warned[0]


@_POSIX_ONLY
def test_a_home_on_sys_path_is_skipped_and_a_user_site_is_not(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_site = home / ".local" / "lib" / "python3.12" / "site-packages"
    user_site.mkdir(parents=True)
    targets = _env_targets(tmp_path, (), homes=(str(home),), path_dirs=(str(home), str(user_site)))
    assert targets == (str(user_site),)


@_POSIX_ONLY
def test_a_project_venv_off_the_gateway_path_stays_writable(tmp_path: Path) -> None:
    venv, venv_site, _shared = _pipx_layout(tmp_path)
    project = tmp_path / "work" / ".venv" / "lib" / "python3.12" / "site-packages"
    project.mkdir(parents=True)
    targets = _env_targets(tmp_path, (str(venv),), path_dirs=(str(venv_site),))
    assert not _sealed(project, targets)


# --------------------------------------------------------------------------- #
# A pyenv base that also holds other environments is narrowed.
# --------------------------------------------------------------------------- #


def _tree(root: Path, *dirs: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


@_POSIX_ONLY
def test_a_pyenv_base_leaves_its_other_virtualenvs_writable(tmp_path: Path) -> None:
    base = _tree(
        tmp_path / ".pyenv" / "versions" / "3.12.1",
        "bin",
        "include/python3.12",
        "lib/python3.12/site-packages",
        "share",
        "envs/crew/lib/python3.12/site-packages",
        "envs/other",
    )
    venv = base / "envs" / "crew"
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    site_dir = venv / "lib" / "python3.12" / "site-packages"
    targets = _env_targets(
        tmp_path,
        (str(venv), str(venv), str(base), str(base)),
        path_dirs=(str(base / "lib" / "python3.12"), str(site_dir)),
        site_dirs=(str(site_dir),),
    )
    assert set(targets) == {str(venv), str(base / "lib" / "python3.12"), str(site_dir)}
    assert not _sealed(base / "envs" / "other", targets)


@_POSIX_ONLY
@pytest.mark.parametrize("mapped", ["loaded", "static", "no-proc"])
def test_a_base_prefix_seals_only_what_its_interpreter_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped: str
) -> None:
    # A venv gateway's base: its stdlib dir (start-up path), its binary and its
    # libpython; not bin/, include/, share/ or the prefix itself.
    base = _tree(tmp_path / "uv" / "cpython-3.12", "bin", "include", "share", "lib/python3.12")
    binary = base / "bin" / "python3.12"
    binary.write_text("", encoding="utf-8")
    (base / "bin" / "python3").symlink_to("python3.12")
    libpython = base / "lib" / f"libpython{sys.version_info[0]}.{sys.version_info[1]}.so.1.0"
    libpython.write_text("", encoding="utf-8")
    stdlib = base / "lib" / "python3.12"
    # The libpython this process mapped (Linux), none for a static build (uv, mise),
    # or, with no /proc to ask (macOS), every one the prefix holds.
    maps = {"loaded": [str(libpython)], "static": [], "no-proc": None}[mapped]
    monkeypatch.setattr(sandbox_env_scan, "_mapped_libpython", lambda: maps)
    targets = _env_targets(
        tmp_path,
        (str(base),),
        path_dirs=(str(stdlib),),
        executables=(str(base / "bin" / "python3"),),
    )
    expected = {str(binary), str(stdlib)} | ({str(libpython)} if mapped != "static" else set())
    assert set(targets) == expected
    for free in ("bin", "include", "share"):
        assert not _sealed(base / free / "new-file", targets)


# --------------------------------------------------------------------------- #
# A conda gateway seals only its own env; the install and other envs stay writable.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_only_the_gateway_s_own_conda_env_is_sealed(tmp_path: Path, no_conda_env: Path) -> None:
    # The gateway runs from envs/kc. Its own env is sealed whole; the install root,
    # its pkgs cache and a sibling env (envs/other) stay writable.
    conda = _tree(tmp_path / "miniconda3", "bin", "conda-meta", "pkgs")
    env = _tree(conda / "envs" / "kc", "bin", "conda-meta", "lib")
    other = _tree(conda / "envs" / "other", "bin", "conda-meta")
    targets = _env_targets(tmp_path, (str(env),) * 4, homes=(str(no_conda_env),))
    assert _sealed(env / "bin" / "python", targets)
    assert not _sealed(other / "bin" / "python", targets)
    assert not _sealed(conda / "pkgs", targets)
    assert not _sealed(conda / "bin" / "conda", targets)


@_POSIX_ONLY
def test_a_conda_base_a_venv_is_built_on_is_not_sealed_as_a_tree(
    tmp_path: Path, no_conda_env: Path
) -> None:
    # A venv on a conda base: the venv is sealed whole, but the conda base stays
    # writable as a tree. Only the base's loaded start-up files (through the search
    # path and interpreter_files) are sealed, which this plan-level check does not add.
    conda = _tree(tmp_path / "miniconda3", "bin", "conda-meta", "pkgs")
    venv = _tree(tmp_path / "venv", "bin")
    (venv / "pyvenv.cfg").write_text(f"home = {conda}/bin\n", encoding="utf-8")
    targets = _env_targets(
        tmp_path, (str(venv), str(venv), str(conda), str(conda)), homes=(str(no_conda_env),)
    )
    assert str(venv) in targets
    assert not _sealed(conda / "pkgs", targets)
    assert not _sealed(conda / "bin" / "conda", targets)


@_POSIX_ONLY
def test_a_nul_byte_path_entry_drops_only_itself(tmp_path: Path) -> None:
    good = _tree(tmp_path / "base" / "lib")
    targets = _env_targets(tmp_path, (), path_dirs=("/x\0y", str(good)))
    assert targets == (str(good),)


@_POSIX_ONLY
def test_a_pyenv_base_is_not_read_as_conda(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _tree(tmp_path / "versions" / "3.12.1", "bin", "lib", "envs/other", "pkgs")
    monkeypatch.setattr(sandbox_tiers, "_conda_seal_dirs", lambda *_a: pytest.fail("conda"))
    targets = _env_targets(tmp_path, (str(base),))
    assert not _sealed(base / "pkgs", targets) and not _sealed(base / "envs" / "other", targets)


# --------------------------------------------------------------------------- #
# A hard-linked sealed file is reported once, at boot.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_hard_linked_sealed_files_are_counted_once(tmp_path: Path) -> None:
    sealed = _tree(tmp_path / "venv", "lib")
    (sealed / "lib" / "plain.py").write_text("", encoding="utf-8")
    linked = sealed / "lib" / "six.py"
    linked.write_text("", encoding="utf-8")
    os.link(linked, tmp_path / "cache-six.py")
    # A nested target (site-packages inside the venv) is not walked twice.
    assert sandbox_env_scan._hard_linked_sealed_files([str(sealed), str(sealed / "lib")]) == (
        1,
        str(linked),
        False,
    )
    assert sandbox_env_scan._hard_linked_sealed_files([str(sealed)], cap=1)[2] is True


@_POSIX_ONLY
def test_a_framework_build_base_seals_only_what_its_start_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A venv on a python.org framework build: bin/python3.12 is a stub that runs
    # the Python.app launcher, which loads the framework binary. Those, the stdlib
    # and lib-dynload are sealed; bin/, include/, share/ and the prefix are not.
    import sysconfig

    real_var = sysconfig.get_config_var
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        sysconfig, "get_config_var", lambda n: "Python" if n == "PYTHONFRAMEWORK" else real_var(n)
    )
    monkeypatch.setattr(sandbox_env_scan, "_mapped_libpython", lambda: None)
    base = _tree(
        tmp_path / "Python.framework" / "Versions" / "3.12",
        "bin",
        "include",
        "share",
        "lib/python3.12/lib-dynload",
        "Resources/Python.app/Contents/MacOS",
    )
    files = [base / "bin" / "python3.12", base / "Python"]
    files.append(base / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python")
    for path in files:
        path.write_text("", encoding="utf-8")
    version = "%d.%d" % sys.version_info[:2]
    (base / "lib" / f"libpython{version}.dylib").symlink_to("../Python")
    venv = _mkvenv(tmp_path / "venv")
    stdlib = base / "lib" / "python3.12"
    targets = _env_targets(
        tmp_path,
        (str(venv), str(base)),
        path_dirs=(str(stdlib), str(stdlib / "lib-dynload")),
        executables=(str(base / "bin" / "python3.12"),),
    )
    loaded = (venv, stdlib, stdlib / "lib-dynload", *files)
    assert set(targets) == set(map(str, loaded))
    for free in ("bin", "include", "share"):
        assert not _sealed(base / free / "sitecustomize.py", targets)
    # A base gateway whose script sits in bin/ starts with bin/ on sys.path: sealed.
    started = _env_targets(tmp_path, (str(base),), path_dirs=(str(base / "bin"),))
    assert _sealed(base / "bin" / "json.py", started)


@_POSIX_ONLY
def test_a_sealed_file_with_a_second_name_is_counted(tmp_path: Path) -> None:
    # The interpreter binary is sealed as a file; a hard link to it is still reported.
    binary = tmp_path / "base" / "bin" / "python3.12"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    os.link(binary, tmp_path / "cache-python3.12")
    assert sandbox_env_scan._hard_linked_sealed_files([str(binary)]) == (1, str(binary), False)


@_POSIX_ONLY
def test_the_boot_prime_warns_once_about_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sealed = _tree(tmp_path / "venv", "lib")
    linked = sealed / "lib" / "six.py"
    linked.write_text("", encoding="utf-8")
    os.link(linked, tmp_path / "cache-six.py")
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_targets", lambda: [str(sealed)])
    with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
        sandbox.prime_interpreter_env_seal()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1 file(s)" in warnings[0].getMessage() and str(linked) in warnings[0].getMessage()
    caplog.clear()
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_targets", lambda: [])
    sandbox.prime_interpreter_env_seal()
    assert not caplog.records


def test_the_start_path_is_snapshotted(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox._gateway_start_sys_path.cache_clear()
    try:
        first = sandbox._gateway_start_sys_path()
        monkeypatch.setattr(sys, "path", [*sys.path, "/inserted/later"])
        assert sandbox._gateway_start_sys_path() == first
    finally:
        sandbox._gateway_start_sys_path.cache_clear()


# --------------------------------------------------------------------------- #
# The links the gateway reaches its interpreter through are guarded.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def linked_venv(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A real venv built through a link to the base Python, as uv, Homebrew ``opt/``
    and a mise version alias lay it out. Returns ``(venv, link)``."""
    import subprocess

    root = tmp_path_factory.mktemp("linked")
    link = root / "py-minor"
    link.symlink_to(os.path.realpath(sys.base_prefix), target_is_directory=True)
    python = link / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    venv = root / "venv"
    subprocess.run(
        [str(python), "-I", "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    return venv, link


@_POSIX_ONLY
@pytest.mark.skipif(not os.access(sys.base_prefix, os.R_OK), reason="needs a base Python")
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_the_link_to_the_base_python_is_guarded(
    linked_venv: tuple[Path, Path], backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv, link = linked_venv
    paths = sandbox._interpreter_link_paths_cached.__wrapped__(
        (str(venv),), (str(venv / "bin" / "python3"), ""), str(venv / "pyvenv.cfg")
    )
    if sys.platform == "darwin" and not any(p.startswith(str(link)) for p in paths):
        pytest.skip("this macOS venv records the resolved base Python, not the link")
    assert any(p.startswith(str(link)) for p in paths)
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_targets", lambda: [str(venv)])
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [])
    monkeypatch.setattr(sandbox, "_resolved_interpreter_link_paths", lambda: list(paths))
    host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=backend))
    assert str(link) in host.readonly_ancestor_guards
    lifted = sandbox._live_plan_host(
        SandboxRequest(tier="standard", backend=backend, gateway_env_writable=True)
    )
    assert str(link) not in lifted.readonly_ancestor_guards


def test_no_link_is_guarded_without_a_sealed_env(tmp_path: Path) -> None:
    cfg = tmp_path / "pyvenv.cfg"
    cfg.write_text("home = /x/bin\n", encoding="utf-8")
    assert sandbox._interpreter_link_paths_cached.__wrapped__((), ("/x",), str(cfg)) == ()
    assert sandbox._interpreter_link_paths_cached.__wrapped__(("/v",), ("",), str(cfg)) == (
        os.path.abspath("/x/bin"),
    )


# --------------------------------------------------------------------------- #
# The plan seals them on both backends, unless the gateway lifts them.
# --------------------------------------------------------------------------- #


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_targets", lambda: [ENV, BASE])
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [INSTALL])
    monkeypatch.setattr(sandbox, "_resolved_interpreter_link_paths", lambda: [])
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_holes", lambda: [])


BASE_SITE = f"{BASE}/lib/python3.12/site-packages"


@_POSIX_ONLY
@pytest.mark.usefixtures("packaged")
def test_an_unused_base_site_dir_is_writable_on_both_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A venv gateway never reads its base's site-packages (proof: a .pth planted there
    # does not run), so the base's own pip keeps working inside the sandbox.
    monkeypatch.setattr(
        sandbox, "_resolved_interpreter_env_targets", lambda: [ENV, f"{BASE}/lib/python3.12"]
    )
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_holes", lambda: [BASE_SITE])
    payload = sandbox_plan.namespace_payload(sandbox._spawn_plan(BACKEND_NAMESPACE, "standard"))
    assert BASE_SITE in payload["writable_dirs"]
    assert f"{BASE}/lib/python3.12" in payload["readonly_dirs"]
    profile = sandbox._build_seatbelt_profile("standard")
    deny = profile.index(f'(deny file-write* (subpath "{BASE}/lib/python3.12"))')
    assert profile.index(f'(allow file-write* (subpath "{BASE_SITE}"))') > deny
    lifted = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard", gateway_env_writable=True)
    assert BASE_SITE not in lifted.writable


@_POSIX_ONLY
def test_a_hole_is_dropped_when_it_holds_another_rule_or_leaves_the_seal() -> None:
    import dataclasses

    host = dataclasses.replace(
        sandbox._live_plan_host(SandboxRequest(tier="standard")),
        install_root_targets=("/p/lib/python3.12",),
        install_root_holes=("/p/lib/python3.12/site-packages", "/elsewhere/site-packages"),
    )
    assert sandbox_plan.install_root_holes(host, []) == ["/p/lib/python3.12/site-packages"]
    assert sandbox_plan.install_root_holes(host, ["/p/lib/python3.12/site-packages/x"]) == []
    # An operator's agent.sandbox_readonly_paths at, around or inside the hole wins.
    for operator in ("/p/lib/python3.12/site-packages", "/p", "/p/lib/python3.12/site-packages/x"):
        assert sandbox_plan.install_root_holes(host, [], (operator,)) == []


@_POSIX_ONLY
def test_only_an_unused_site_dir_inside_a_seal_is_a_hole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import site

    stdlib = _tree(tmp_path / "base" / "lib" / "python3.12", "site-packages")
    base_site = stdlib / "site-packages"
    monkeypatch.setattr(site, "getsitepackages", lambda prefixes=None: [str(base_site)])
    holes = sandbox_env_scan.unused_site_holes
    assert holes((str(tmp_path / "base"),), (), (str(stdlib),)) == [str(base_site)]
    # In use (a base gateway, or a venv with system site-packages): sealed, no hole.
    assert holes((str(tmp_path / "base"),), (str(base_site),), (str(stdlib),)) == []
    # Not inside a seal, or holding one: no hole.
    assert holes((str(tmp_path / "base"),), (), ()) == []
    assert holes((str(tmp_path / "base"),), (), (str(stdlib), str(base_site / "pkg"))) == []


@_POSIX_ONLY
def test_the_console_script_and_the_interpreter_file_are_sealed(tmp_path: Path) -> None:
    script = tmp_path / ".local" / "bin" / "kirocrew"
    script.parent.mkdir(parents=True)
    script.write_text("#!/x/python\n", encoding="utf-8")
    targets = _env_targets(tmp_path, (), executables=(str(script), ""))
    assert targets == (str(script),)
    assert not _sealed(script.parent / "other-cli", targets)


def test_the_console_script_link_is_a_guarded_link_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pipx ~/.local/bin/kirocrew link, re-pointed, would pick the next start's code.
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_targets", lambda: ["/sealed"])
    monkeypatch.setattr(
        sandbox_tiers, "_interpreter_link_paths_cached", lambda t, paths, cfg: seen.append(paths)
    )
    monkeypatch.setattr(sys, "argv", ["/home/u/.local/bin/kirocrew", "gateway"])
    sandbox_tiers._resolved_interpreter_link_paths()
    monkeypatch.setattr(sys, "argv", ["/usr/bin/other-tool"])
    sandbox_tiers._resolved_interpreter_link_paths()
    assert "/home/u/.local/bin/kirocrew" in seen[0]
    assert "/usr/bin/other-tool" not in seen[1]


@_POSIX_ONLY
def test_no_hole_on_a_conda_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_resolved_interpreter_env_targets", lambda: ["/c/envs/gw"])
    monkeypatch.setattr(sandbox_env_scan, "_conda_root", lambda p: "/c")
    monkeypatch.setattr(sandbox_env_scan, "unused_site_holes", lambda *a: pytest.fail("holes"))
    assert sandbox._resolved_interpreter_env_holes() == []


@_POSIX_ONLY
@pytest.mark.usefixtures("packaged")
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("tier", ["strict", "cc", "standard"])
def test_every_tier_seals_the_env_unless_the_gateway_lifts_it(backend: str, tier: str) -> None:
    sealed = sandbox._spawn_plan(backend, tier).readonly
    assert {ENV, BASE, INSTALL} <= set(sealed)
    lifted = sandbox._spawn_plan(backend, tier, gateway_env_writable=True).readonly
    assert ENV not in lifted and BASE not in lifted
    assert INSTALL in lifted


@_POSIX_ONLY
@pytest.mark.usefixtures("packaged")
def test_the_namespace_launcher_gets_the_env_before_the_package_dir() -> None:
    # The package directory's seal must mount on top of the env's, not under it.
    host = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert host.install_root_targets == (ENV, BASE, INSTALL)
    readonly = sandbox_plan.namespace_payload(sandbox._spawn_plan(BACKEND_NAMESPACE, "standard"))[
        "readonly_dirs"
    ]
    assert {ENV, BASE, INSTALL} <= set(readonly)


@_POSIX_ONLY
@pytest.mark.usefixtures("packaged")
def test_the_seatbelt_profile_denies_writes_below_each_prefix() -> None:
    profile = sandbox._build_seatbelt_profile("standard")
    for target in (ENV, BASE):
        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-link (subpath "{target}"))' in profile
        assert f'(allow file-write* (subpath "{target}' not in profile
    lifted = sandbox._build_seatbelt_profile("standard", gateway_env_writable=True)
    assert f'(deny file-write* (subpath "{BASE}"))' not in lifted
    assert f'(deny file-write* (subpath "{INSTALL}"))' in lifted


@_POSIX_ONLY
@pytest.mark.usefixtures("packaged")
def test_renaming_a_parent_of_the_base_prefix_is_guarded() -> None:
    host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT))
    assert {"/opt/python", "/opt"} <= set(host.readonly_ancestor_guards)
    lifted = sandbox._live_plan_host(
        SandboxRequest(tier="standard", backend=BACKEND_SEATBELT, gateway_env_writable=True)
    )
    assert "/opt/python" not in lifted.readonly_ancestor_guards


# --------------------------------------------------------------------------- #
# The flag reaches the plan from both wrap entry points.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flag", [False, True])
def test_wrap_argv_passes_the_flag_to_the_launcher(
    flag: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def namespace_argv(argv, level, **kwargs):
        seen.update(kwargs)
        return [sys.executable, *sandbox._LAUNCHER_INTERPRETER_FLAGS, "launcher.py", *argv]

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "namespace")
    monkeypatch.setattr(sandbox, "namespace_argv", namespace_argv)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "_spawns_kiro_cli", lambda argv: False)
    sandbox.wrap_argv(["/bin/true"], mode="standard", gateway_env_writable=flag)
    assert seen["gateway_env_writable"] is flag


@pytest.mark.asyncio
async def test_wrap_argv_async_passes_the_flag() -> None:
    seen: dict = {}

    def prepare(argv, **kwargs):
        seen.update(kwargs)
        return list(argv), None

    await sandbox.wrap_argv_async(["/bin/true"], gateway_env_writable=True, _prepare=prepare)
    assert seen["gateway_env_writable"] is True


# --------------------------------------------------------------------------- #
# Only the gateway's own fixed-argv installs set it.
# --------------------------------------------------------------------------- #

#: ``path relative to the package -> number of ``gateway_env_writable=True`` sites``.
_ALLOWED_SETTERS = {
    # `ensurepip` and `pip install faiss-cpu`, both fixed argv. Nothing else: the
    # registry refuses every app build that would pip into the gateway env.
    "dashboard/handlers/memory_embeddings.py": 2,
}
#: Modules that only forward the caller's value.
_PLUMBING = {
    "sandbox_argv.py",
    "sandbox_plan.py",
    "sandbox_spawn.py",
    "sandbox_wrap.py",
}
_FLAG = "gateway_env_writable"


def _is_flag_name(node: ast.AST) -> bool:
    """``gateway_env_writable`` or ``bool(gateway_env_writable)``: a forwarded parameter."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bool":
        return len(node.args) == 1 and not node.keywords and _is_flag_name(node.args[0])
    return isinstance(node, ast.Name) and node.id == _FLAG


def _flag_values(tree: ast.AST) -> list[ast.AST]:
    """Every value a module hands the flag: a keyword, a dict key (``{...}`` or
    ``**{...}``), a subscript assignment, ``setattr``. A ``"gateway_env_writable"``
    string anywhere else is refused, so no other spelling slips past."""
    values: list[ast.AST] = []
    consumed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == _FLAG:
            values.append(node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == _FLAG:
                    values.append(value)
                    consumed.add(id(key))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                key = target.slice if isinstance(target, ast.Subscript) else None
                if isinstance(key, ast.Constant) and key.value == _FLAG:
                    values.append(node.value)
                    consumed.add(id(key))
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
            if len(node.args) == 3 and getattr(node.args[1], "value", None) == _FLAG:
                values.append(node.args[2])
                consumed.add(id(node.args[1]))
    stray = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == _FLAG]
    assert all(id(n) in consumed for n in stray), "unrecognised flag spelling"
    return values


def _flag_setters(text: str, relative: str) -> int:
    count = 0
    tree = ast.parse(text)
    for value in _flag_values(tree):
        if isinstance(value, ast.Constant) and value.value is False:
            continue
        if relative in _PLUMBING and _is_flag_name(value):
            continue
        # Anything else -- a computed value, a config read, ``True`` elsewhere.
        assert isinstance(value, ast.Constant) and value.value is True, relative
        count += 1
    for node in ast.walk(tree):
        # Every function taking the flag takes it keyword-only, and the request is
        # never built positionally, so no positional argument can carry it.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
            assert _FLAG not in positional, f"{relative}:{node.name}"
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "SandboxRequest":
            assert not node.args, relative
    return count


def test_only_the_allowed_sites_set_the_flag() -> None:
    root = Path(sandbox.__file__).resolve().parent
    found: dict[str, int] = {}
    for source in root.rglob("*.py"):
        relative = source.relative_to(root).as_posix()
        text = source.read_text(encoding="utf-8")
        if _FLAG not in text:
            continue
        count = _flag_setters(text, relative)
        if count:
            found[relative] = count
    assert found == _ALLOWED_SETTERS


@pytest.mark.parametrize(
    "snippet",
    [
        'options["gateway_env_writable"] = True',
        'wrap_argv(cmd, **{"gateway_env_writable": True})',
        "wrap_argv(cmd, gateway_env_writable=flag)",
        'setattr(request, "gateway_env_writable", True)',
        'dict([("gateway_env_writable", True)])',
        "SandboxRequest(1, 2, 3)",
        "def wrap(argv, gateway_env_writable=False): pass",
    ],
)
def test_the_tripwire_catches_each_spelling(snippet: str) -> None:
    with pytest.raises(AssertionError):
        assert _flag_setters(snippet, "apps/other.py") == 0


@pytest.mark.parametrize(
    ("snippet", "ok"),
    [
        ("f(gateway_env_writable=x)", False),
        ('o["gateway_env_writable"] = x', False),
        ("f(gateway_env_writable=gateway_env_writable)", True),
        ("f(gateway_env_writable=bool(gateway_env_writable))", True),
        ('o["gateway_env_writable"] = gateway_env_writable', True),
    ],
)
def test_plumbing_forwards_only_the_parameter_name(snippet: str, ok: bool) -> None:
    if ok:
        assert _flag_setters(snippet, "sandbox_wrap.py") == 0
    else:
        with pytest.raises(AssertionError):
            _flag_setters(snippet, "sandbox_wrap.py")


def _flags(**over: int) -> types.SimpleNamespace:
    """``sys.flags`` with *over* changed: every real field kept, so code reading
    another flag (pytest's own teardown reads ``verbose``) still works."""
    names = [n for n in dir(sys.flags) if not n.startswith("_") and n not in ("count", "index")]
    return types.SimpleNamespace(**{**{n: getattr(sys.flags, n) for n in names}, **over})


def test_the_service_keeps_the_script_dir_off_sys_path(tmp_path: Path) -> None:
    # A --user install puts the console script in ~/.local/bin; with safe_path that
    # directory is not on the gateway's sys.path, so the seal does not take it.
    from kiro_crew.service.common import service_environment

    assert service_environment(str(tmp_path))["PYTHONSAFEPATH"] == "1"


def test_an_agent_child_does_not_inherit_safe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gateway's own safe-path must not change how an agent's scripts import.
    from kiro_crew.sandbox_spawn import scrub_agent_subprocess_env

    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    env = scrub_agent_subprocess_env(dict(os.environ))
    assert "PYTHONSAFEPATH" not in env


def test_a_configured_script_dir_stays_a_start_path_under_safe_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Safe path keeps Python from adding the script dir; one a PYTHONPATH or .pth
    # names is still searched, so it is still sealed.
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "kirocrew"), "gateway"])
    monkeypatch.setattr(sys, "path", [str(bin_dir), str(tmp_path / "site")])
    monkeypatch.setattr(sys, "flags", _flags(safe_path=True))
    assert sandbox._gateway_start_sys_path.__wrapped__() == (str(bin_dir), str(tmp_path / "site"))


@_POSIX_ONLY
def test_safe_path_keeps_the_script_dir_off_a_real_interpreter_s_path(tmp_path: Path) -> None:
    import subprocess

    script = tmp_path / "bin" / "kirocrew-probe"
    script.parent.mkdir()
    script.write_text("import sys; print(repr(sys.path))\n", encoding="utf-8")
    run = lambda *flags: subprocess.run(  # noqa: E731
        [sys.executable, "-E", "-s", *flags, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    ).stdout
    assert str(script.parent) in run()
    assert str(script.parent) not in run("-P")


@_POSIX_ONLY
def test_a_top_level_home_dir_on_sys_path_is_left_writable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    (home / "projects" / "ws").mkdir(parents=True)
    (home / ".local").mkdir()
    entries = (str(home / "projects"), str(home / ".local"))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        targets = _env_targets(tmp_path, (), homes=(str(home),), path_dirs=entries)
    assert targets == ()
    assert sum("left writable" in r.getMessage() for r in caplog.records) == 2


@_POSIX_ONLY
def test_a_dir_holding_the_workspace_is_left_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import loader_locations

    work = tmp_path / "code"
    (work / "crew-workspace").mkdir(parents=True)
    monkeypatch.setattr(
        loader_locations, "workspace_root", lambda create=True: work / "crew-workspace"
    )
    assert _env_targets(tmp_path, (), path_dirs=(str(work),)) == ()
    # A nested link to it is the same user tree: neither spelling is sealed.
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "ws").symlink_to(work)
    assert _env_targets(tmp_path, (), path_dirs=(str(tmp_path / "links" / "ws"),)) == ()


def _service_reexec(monkeypatch: pytest.MonkeyPatch, *, safe_path: bool, ignore_env: bool):
    from kiro_crew.service import common

    calls: list[tuple] = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(sys, "orig_argv", ["python", "/u/.local/bin/kirocrew", "gateway"])
    monkeypatch.setattr(sys, "flags", _flags(safe_path=safe_path, ignore_environment=ignore_env))
    common._reexec_service_with_safe_path_original()
    return calls


def test_an_old_service_unit_restarts_the_gateway_once_with_safe_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")
    calls = _service_reexec(monkeypatch, safe_path=False, ignore_env=False)
    assert len(calls) == 1
    assert calls[0][1][1:] == ["-P", "/u/.local/bin/kirocrew", "gateway"]
    # The replacement has safe_path set, so it does not exec again.
    assert _service_reexec(monkeypatch, safe_path=True, ignore_env=False) == []


def test_no_safe_path_restart_under_dash_e_or_in_the_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")
    assert _service_reexec(monkeypatch, safe_path=False, ignore_env=True) == []
    monkeypatch.delenv("KIROCREW_SERVICE_MANAGED")
    assert _service_reexec(monkeypatch, safe_path=False, ignore_env=False) == []


@_POSIX_ONLY
def test_a_child_of_a_dash_e_gateway_imports_its_sibling_module(tmp_path: Path) -> None:
    # pipx's shebang is `python -E`: the gateway ignores PYTHONSAFEPATH, but its
    # children would not, so it is dropped for them anyway.
    import subprocess

    (tmp_path / "helpers.py").write_text("OK = 1\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("import helpers\n", encoding="utf-8")
    parent = (
        "import os, subprocess, sys\n"
        "from kiro_crew.service.common import drop_safe_path_for_children\n"
        "drop_safe_path_for_children()\n"
        f"sys.exit(subprocess.run([sys.executable, {str(tmp_path / 'run.py')!r}]).returncode)\n"
    )
    env = {**os.environ, "PYTHONSAFEPATH": "1"}
    src = str(Path(sandbox.__file__).resolve().parents[1])
    proc = subprocess.run(
        # -E drops PYTHONDONTWRITEBYTECODE, so -B keeps .pyc files out of src/.
        [sys.executable, "-E", "-B", "-c", f"import sys; sys.path.insert(0, {src!r})\n" + parent],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert proc.returncode == 0, proc.stderr
