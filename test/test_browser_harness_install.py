"""The Harness installer (``browser_harness/install.py``) with a fake subprocess runner."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks
from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.browser_harness import HARNESS_VERSION, build_deps, chromium, install
from kiro_crew.security import paths_lists
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _no_old_glibc(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs as on a host whose glibc the locked wheels cover, unless it says
    otherwise: without the pin an old-glibc test host would refuse every install below."""
    _floor_monkeypatch.setattr(build_deps, "refusal_for_this_host", lambda: None)


#: The distributions the gateway's interpreter must carry for its own copy to be used.
_HARNESS_FAMILY = ("browser-harness", "cdp-use", "fetch-use")

#: The search-path helper as the module defines it, read before the autouse fixture below
#: replaces it, and looked up by name so a tree without it still collects this file.
_REAL_RUNTIME_SEARCH_PATH = getattr(install, "_runtime_search_path", None)


@pytest.fixture(autouse=True)
def runtime_site(_floor_monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Resolve lock distributions only from fake runtime sites, never the test venv."""
    dirs: list[Path] = []
    _floor_monkeypatch.setattr(
        install, "_runtime_search_path", lambda: [str(d) for d in dirs], raising=False
    )
    return dirs


def _lock_pin(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}==(\S+) \\$", install.LOCK_FILE.read_text(encoding="utf-8"), re.M
    )
    assert match, name
    return match.group(1)


def _runtime_ships(site: Path, **versions: str) -> Path:
    """A runtime carrying recorded package files for every lock distribution.

    A keyword (``cdp_use="9.9.9"``) moves one off its pin; an empty one omits it.
    """
    for name, pin in install._lock_pins().items():
        module = name.replace("-", "_")
        version = versions.get(module, pin)
        if not version:
            continue
        info = site / f"{module}-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
        )
        files = {f"{module}/__init__.py": ""}
        if name == "browser-harness":
            files["browser_harness/daemon.py"] = 'print("original daemon")\n'
        for relative, text in files.items():
            target = site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        recorded = [*files, f"{info.name}/METADATA", f"{info.name}/RECORD"]
        (info / "RECORD").write_text(
            "".join(f"{relative},,\n" for relative in recorded), encoding="utf-8"
        )
    return site


def _ok(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # ``python -m venv <prefix>/venv`` creates the interpreter the next step runs;
    # ``--clear`` empties the directory first.
    if argv[1:4] == ["-I", "-m", "venv"]:
        if "--clear" in argv:
            shutil.rmtree(argv[-1], ignore_errors=True)
        python = install.venv_python(Path(argv[-1]).parent)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _gateway_ships(monkeypatch, *present: str) -> None:
    """Pin which of ``pip`` / ``ensurepip`` the gateway interpreter can import."""
    real = importlib.util.find_spec

    def find_spec(name: str, *args, **kwargs):
        if name in ("pip", "ensurepip"):
            return real("json") if name in present else None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def test_the_lock_pins_the_harness_release_with_hashes_on_every_line() -> None:
    text = install.LOCK_FILE.read_text(encoding="utf-8")
    assert f"browser-harness=={HARNESS_VERSION}" in text
    requirements = [ln for ln in text.splitlines() if ln and not ln.startswith(("#", " ", "-"))]
    assert requirements, "the lock names no requirement"
    for req in requirements:
        assert re.match(r"^[A-Za-z0-9_.-]+==[0-9][^ ]* \\$", req), req
    assert text.count("--hash=sha256:") >= len(requirements)


def test_absent_home_reads_as_not_installed(tmp_path: Path) -> None:
    state = install.install_state(tmp_path)
    assert state.installed is False
    assert state.version is None and state.python is None
    assert state.prefix == tmp_path / install.PREFIX_LEAF
    assert state.as_dict()["wanted_version"] == HARNESS_VERSION


def test_install_runs_venv_then_hash_pinned_pip_and_writes_the_marker(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    monkeypatch.setattr(
        chromium,
        "download_chromium",
        lambda prefix, progress=None, **kw: chromium.chromium_state(prefix),
    )
    # The packaged app's interpreter: ``pip`` present (pinned, not the test venv's).
    _gateway_ships(monkeypatch, "pip")
    stages: list[str] = []
    state = install.install_sync(tmp_path, lambda s, d: stages.append(s), runner=runner)
    prefix = tmp_path / install.PREFIX_LEAF
    # Both interpreters run isolated (``-I``): the gateway's cwd (an agent's
    # workspace on a pod) and ``PYTHONPATH`` never reach ``sys.path``, so a
    # ``venv/`` or ``pip/`` package written there cannot shadow the real one.
    assert calls[0][:5] == [sys.executable, "-I", "-m", "venv", "--clear"]
    # ``--clear`` empties the directory it is given: the venv's own, never the
    # prefix that also holds the downloaded Chromium.
    assert calls[0][-1] == str(install.venv_dir(prefix)) == str(prefix / "venv")
    # The packaged interpreter has no ``ensurepip``: the venv is made without pip
    # and the gateway's pip installs into it.
    assert "--without-pip" in calls[0]
    assert str(install.venv_python(prefix)).startswith(str(prefix / "venv"))
    assert calls[1][:7] == [
        sys.executable,
        "-I",
        "-m",
        "pip",
        "--python",
        str(install.venv_python(prefix)),
        "install",
    ]
    assert "--require-hashes" in calls[1] and calls[1][-1] == str(install.LOCK_FILE)
    marker = json.loads((prefix / install.MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["version"] == HARNESS_VERSION
    assert marker["lock_sha256"] == install.lock_digest()
    assert state.installed is True and state.version == HARNESS_VERSION
    assert stages[:3] == [
        install.STAGE_PREPARING,
        install.STAGE_CREATING_VENV,
        install.STAGE_INSTALLING_HARNESS,
    ]
    assert stages[-1] == install.STAGE_FINISHING


def test_without_gateway_pip_the_venv_bootstraps_its_own_and_installs_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    # The Toolbox runtime ships ``ensurepip`` but no ``pip`` module.
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    prefix = tmp_path / install.PREFIX_LEAF
    # A finished earlier install made ``--without-pip``: the venv is still rebuilt.
    (prefix / "venv").mkdir(parents=True)
    install.venv_python(prefix).parent.mkdir(parents=True, exist_ok=True)
    install.venv_python(prefix).write_text("", encoding="utf-8")
    (prefix / install.MARKER_FILE).write_text(
        json.dumps({"version": HARNESS_VERSION}), encoding="utf-8"
    )
    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--clear",
        str(install.venv_dir(prefix)),
    ]
    assert calls[1][:5] == [str(install.venv_python(prefix)), "-I", "-m", "pip", "install"]
    assert "--python" not in calls[1] and calls[1][-1] == str(install.LOCK_FILE)


def _current_install(prefix: Path, *, venv_pip: bool) -> None:
    """A finished install on disk: interpreter, marker, and optionally the venv's pip."""
    install.venv_python(prefix).parent.mkdir(parents=True, exist_ok=True)
    install.venv_python(prefix).write_text("", encoding="utf-8")
    if venv_pip:
        lib = "Lib" if install.IS_WINDOWS else "lib/python3.12"
        (install.venv_dir(prefix) / lib / "site-packages" / "pip").mkdir(parents=True)
    (prefix / install.MARKER_FILE).write_text(
        json.dumps({"version": HARNESS_VERSION}), encoding="utf-8"
    )


def test_without_gateway_pip_a_venv_with_its_own_pip_is_kept(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=True)
    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert len(calls) == 1 and calls[0][:4] == [str(install.venv_python(prefix)), "-I", "-m", "pip"]


def _pruned_install(prefix: Path, *, minor: int) -> Path:
    """A finished install whose base Python is gone: the interpreter links dangle.

    The venv was built by Python 3.<minor> and holds its own pip and the locked
    packages; returns a sentinel file in its site-packages.
    """
    venv = install.venv_dir(prefix)
    lib = "Lib" if install.IS_WINDOWS else f"lib/python3.{minor}"
    site = venv / lib / "site-packages"
    (site / "pip").mkdir(parents=True)
    (site / "browser_harness").mkdir()
    sentinel = site / "browser_harness" / "__init__.py"
    sentinel.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text(
        f"home = {prefix.parent / 'pruned'}\nversion = 3.{minor}.4\n", encoding="utf-8"
    )
    bindir = install.venv_python(prefix).parent
    bindir.mkdir(parents=True)
    gone = prefix.parent / "pruned" / f"python3.{minor}"
    for name in ("python", "python3", f"python3.{minor}"):
        (bindir / name).symlink_to(gone)
    (prefix / install.MARKER_FILE).write_text(
        json.dumps({"version": HARNESS_VERSION, "lock_sha256": install.lock_digest()}),
        encoding="utf-8",
    )
    return sentinel


@requires_symlinks
@pytest.mark.parametrize("gateway_has", ["pip", "ensurepip"])
def test_a_venv_whose_base_python_was_removed_is_repaired_in_place(
    tmp_path: Path, monkeypatch, gateway_has: str
) -> None:
    """Toolbox prunes the version the venv was built on: the links dangle, the packages
    stay. The venv is re-pointed at this interpreter instead of cleared, so pip finds
    every pin already there and needs no index."""
    _gateway_ships(monkeypatch, gateway_has)
    prefix = tmp_path / install.PREFIX_LEAF
    sentinel = _pruned_install(prefix, minor=sys.version_info[1])
    assert install.install_state(tmp_path).installed is False
    assert install.harness_current(tmp_path) is False
    calls: list[list[str]] = []
    links_at_venv: list[str] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:4] == ["-I", "-m", "venv"]:
            bindir = install.venv_python(prefix).parent
            links_at_venv.extend(p.name for p in bindir.iterdir() if p.is_symlink())
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--upgrade",
        "--without-pip",
        str(install.venv_dir(prefix)),
    ]
    # ``venv --upgrade`` keeps an entry that is already a symlink, so the dangling
    # ones are gone before it runs.
    assert links_at_venv == []
    assert "pip" in calls[1] and calls[1][-1] == str(install.LOCK_FILE)
    assert len(calls) == 2
    assert sentinel.is_file()
    marker = json.loads((prefix / install.MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["python"] == str(install.venv_python(prefix))
    assert install.harness_current(tmp_path) is True


@requires_symlinks
def test_a_venv_built_by_another_python_minor_is_still_rebuilt(tmp_path: Path, monkeypatch) -> None:
    """Its compiled wheels only fit the minor version that built it."""
    _gateway_ships(monkeypatch, "ensurepip")
    prefix = tmp_path / install.PREFIX_LEAF
    _pruned_install(prefix, minor=sys.version_info[1] - 1)
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0][:5] == [sys.executable, "-I", "-m", "venv", "--clear"]


@requires_symlinks
def test_a_pruned_runtime_filled_venv_is_rebuilt_not_repaired(tmp_path: Path, monkeypatch) -> None:
    """The runtime marker requires a rebuild even when pip and the minor match."""
    _gateway_ships(monkeypatch, "pip")
    prefix = tmp_path / install.PREFIX_LEAF
    sentinel = _pruned_install(prefix, minor=sys.version_info[1])
    marker = prefix / install.MARKER_FILE
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["source"] = "runtime"
    marker.write_text(json.dumps(data), encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert calls[0][:6] == [sys.executable, "-I", "-m", "venv", "--clear", "--without-pip"]
    assert not sentinel.exists()


def test_a_rebuild_whose_pip_fails_reads_as_not_installed(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch, "ensurepip")

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "pip" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "network unreachable")
        return _ok(argv, timeout)

    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=False)
    assert install.install_state(tmp_path).installed is True
    with pytest.raises(install.InstallError):
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert install.install_state(tmp_path).installed is False


def test_neither_pip_nor_ensurepip_fails_before_any_spawn(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected spawn: {argv}")

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_PREPARING
    assert "ensurepip" in excinfo.value.detail


def test_a_failing_pip_raises_with_the_stage_and_the_output_tail(
    tmp_path: Path, monkeypatch
) -> None:
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "pip" in argv:
            return subprocess.CompletedProcess(
                argv, 1, "", "THESE PACKAGES DO NOT MATCH THE HASHES"
            )
        return _ok(argv, timeout)

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_INSTALLING_HARNESS
    assert "HASHES" in excinfo.value.detail
    assert install.install_state(tmp_path).installed is False


def test_a_timed_out_step_is_an_install_error(tmp_path: Path) -> None:
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_CREATING_VENV


def test_chromium_only_skips_the_venv(tmp_path: Path, monkeypatch) -> None:
    seen: list[Path] = []

    def fake_download(prefix: Path, progress=None, **kw) -> chromium.ChromiumState:
        seen.append(prefix)
        return chromium.chromium_state(prefix)

    monkeypatch.setattr(chromium, "download_chromium", fake_download)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise AssertionError("no subprocess for a chromium-only install")

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_CHROMIUM,))
    assert seen == [tmp_path / install.PREFIX_LEAF]


def test_a_chromium_download_failure_is_an_install_error(tmp_path: Path, monkeypatch) -> None:
    def boom(prefix: Path, progress=None, **kw) -> chromium.ChromiumState:
        raise chromium.ChromiumDownloadError("offline")

    monkeypatch.setattr(chromium, "download_chromium", boom)
    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, components=(install.COMPONENT_CHROMIUM,))
    assert excinfo.value.stage == chromium.STAGE_DOWNLOADING


def test_the_prefix_leaf_is_spelled_the_same_in_every_fence() -> None:
    leaf = install.PREFIX_LEAF
    assert leaf == "browser-harness"
    assert leaf in sandbox_leaves._CREW_READONLY_LEAVES
    assert leaf in sandbox_leaves._CREW_CHILD_WITHHELD_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._DELEGATED_OVERLAP_LEAF_REASONS
    assert leaf in paths_lists._CREW_SECRET_LEAVES


def test_the_stage_order_covers_chromium_too() -> None:
    assert chromium.STAGE_DOWNLOADING in install.STAGES
    assert (
        install.STAGES[0] == install.STAGE_PREPARING
        and install.STAGES[-1] == install.STAGE_FINISHING
    )


def test_the_installers_children_get_a_scrubbed_env_and_the_prefix_as_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """The second and third fence beside ``-I``: no ``PYTHON*``/``PIP_*`` from the
    gateway's shell (``PYTHONPATH``, ``PIP_INDEX_URL``, a ``PIP_CONFIG_FILE``), and a
    working directory that is the owner-only prefix, never the gateway's own cwd."""
    env = install._child_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/o",
            "HTTPS_PROXY": "http://proxy:3128",
            "PYTHONPATH": "/workspace/evil",
            "PYTHONSTARTUP": "/workspace/evil/startup.py",
            "PIP_INDEX_URL": "http://mirror.evil/simple",
            "pip_config_file": "/workspace/pip.conf",
        }
    )
    assert "PATH" in env and "HOME" in env and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert not any(
        k.upper().startswith(("PYTHON", "PIP_"))
        for k in env
        if k not in ("PYTHONIOENCODING", "PIP_NO_INPUT", "PIP_CONFIG_FILE")
    )
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PIP_NO_INPUT"] == "1"
    # The null device, not the inherited file: pip then loads no config file at all.
    assert env["PIP_CONFIG_FILE"] == os.devnull

    seen: dict[str, object] = {}

    class FakeChild:
        returncode = 0

        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(kwargs)

        def communicate(self, timeout):  # type: ignore[no-untyped-def]
            return "", ""

    monkeypatch.setattr(install.subprocess, "Popen", FakeChild)
    prefix = tmp_path / install.PREFIX_LEAF
    install._run([sys.executable, "-I", "-c", "pass"], 1.0, cwd=prefix)
    assert seen["cwd"] == str(prefix)
    assert "PYTHONPATH" not in seen["env"]  # type: ignore[operator]


def test_the_installers_pip_reads_no_index_from_the_owners_pip_config(
    tmp_path: Path, monkeypatch
) -> None:
    """A real pip under the installer's own spawn never sees the owner's pip config.

    An ``index-url`` there (a CodeArtifact repository whose login expired answers 401
    for every locked wheel) would otherwise replace the index the lock was audited
    against, and the Settings page would show pip's raw failure.
    """
    if importlib.util.find_spec("pip") is None:
        pytest.skip("this interpreter ships no pip")
    owner_home = tmp_path / "owner"
    index = "https://owner-pip-config.invalid/simple/"
    if install.IS_WINDOWS:
        conf = owner_home / "pip" / "pip.ini"
    else:
        conf = owner_home / ".pip" / "pip.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"[global]\nindex-url = {index}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("USERPROFILE", str(owner_home))
    argv = [sys.executable, "-I", "-m", "pip", "config", "list"]
    # The file is one pip loads: with the installer's environment minus its
    # config-file override, pip reports the owner's index.
    control_env = {k: v for k, v in install._child_env().items() if k != "PIP_CONFIG_FILE"}
    control = subprocess.run(
        argv,
        cwd=str(tmp_path),
        env=control_env,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert index in control.stdout, control.stdout + control.stderr
    prefix = tmp_path / install.PREFIX_LEAF
    prefix.mkdir()
    result = install._run(argv, FALLBACK_TIMEOUT_SECS, cwd=prefix)
    assert result.returncode == 0, result.stderr
    assert index not in result.stdout + result.stderr


def _old_glibc_missing(monkeypatch, *missing: str) -> None:
    text = build_deps.refusal(list(missing), glibc=(2, 26))
    monkeypatch.setattr(build_deps, "refusal_for_this_host", lambda: text)


def test_an_old_glibc_host_without_the_build_deps_is_refused_before_any_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _old_glibc_missing(monkeypatch, "jpeglib.h")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    with pytest.raises(install.InstallError) as info:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert info.value.stage == install.STAGE_PREPARING
    assert "pillow 12.3.0" in info.value.detail and "jpeglib.h" in info.value.detail
    assert calls == []


def test_a_refused_rebuild_leaves_the_existing_install_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    """The refusal comes before the marker is removed and the venv cleared."""
    _gateway_ships(monkeypatch, "ensurepip")
    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=False)
    assert install.install_state(tmp_path).installed is True
    _old_glibc_missing(monkeypatch, build_deps.MISSING_COMPILER)
    with pytest.raises(install.InstallError):
        install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    assert install.install_state(tmp_path).installed is True


def test_a_venv_that_already_holds_the_locked_pillow_needs_no_build(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-pressing Install on a host that once built pillow does not ask for a compiler."""
    _gateway_ships(monkeypatch, "ensurepip")
    prefix = tmp_path / install.PREFIX_LEAF
    _current_install(prefix, venv_pip=True)
    lib = "Lib" if install.IS_WINDOWS else "lib/python3.12"
    dist_info = f"pillow-{build_deps.LOCKED_PILLOW}.dist-info"
    (install.venv_dir(prefix) / lib / "site-packages" / dist_info).mkdir(parents=True)
    _old_glibc_missing(monkeypatch, build_deps.MISSING_COMPILER, "jpeglib.h")
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert len(calls) == 1 and "pip" in calls[0]


def _recording_runner(calls: list[list[str]]):
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok(argv, timeout)

    return runner


@pytest.mark.parametrize("safe_path", [False, True])
def test_runtime_search_path_keeps_ordinary_entries(
    tmp_path: Path, monkeypatch, safe_path: bool
) -> None:
    launch = str(tmp_path / "launch")
    runtime = str(tmp_path / "runtime")
    bundle = str(tmp_path / "bundle-from-pth")
    paths = [launch, "", runtime, bundle, str(tmp_path), str(tmp_path / ".")]
    monkeypatch.setattr(
        install, "sys", SimpleNamespace(path=paths, flags=SimpleNamespace(safe_path=safe_path))
    )
    monkeypatch.chdir(tmp_path)
    assert _REAL_RUNTIME_SEARCH_PATH() == ([launch] if safe_path else []) + [runtime, bundle]
    assert install.sys.path == paths


@pytest.mark.parametrize("failure", ["getcwd", "realpath"])
def test_runtime_search_path_keeps_nonempty_nonlaunch_entries_on_oserror(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    paths = [str(tmp_path / "launch"), "", str(tmp_path / "runtime"), str(tmp_path)]
    monkeypatch.setattr(
        install, "sys", SimpleNamespace(path=paths, flags=SimpleNamespace(safe_path=False))
    )

    def unavailable(*args):
        raise OSError("directory unavailable")

    with monkeypatch.context() as unavailable_path:
        if failure == "getcwd":
            unavailable_path.setattr(os, "getcwd", unavailable)
        else:
            unavailable_path.setattr(os.path, "realpath", unavailable)
        actual = _REAL_RUNTIME_SEARCH_PATH()
    assert actual == paths[2:]


@pytest.mark.parametrize("placement", ["launch", "cwd", "empty"])
def test_workspace_distributions_are_not_copied(
    tmp_path: Path, monkeypatch, placement: str
) -> None:
    workspace = _runtime_ships(tmp_path / "workspace")
    ordinary = str(tmp_path / "runtime")
    paths = {
        "launch": [str(workspace), ordinary],
        "cwd": [ordinary, str(workspace)],
        "empty": [ordinary, ""],
    }[placement]
    _gateway_ships(monkeypatch, "pip")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        install,
        "sys",
        SimpleNamespace(
            path=paths,
            flags=SimpleNamespace(safe_path=placement != "launch"),
            executable=sys.executable,
            version_info=sys.version_info,
        ),
    )
    monkeypatch.setattr(install, "_runtime_search_path", _REAL_RUNTIME_SEARCH_PATH, raising=False)
    calls: list[list[str]] = []
    home = tmp_path / "home"
    with monkeypatch.context() as search:
        search.setattr(sys, "path", paths)
        install.install_sync(
            home, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
        )
    assert any("pip" in argv and "--require-hashes" in argv for argv in calls)
    copied = install._venv_site_packages(install.harness_prefix(home))
    assert not (copied / "browser_harness").exists()


def test_a_runtime_that_ships_the_pinned_harness_is_copied_and_pip_never_runs(
    tmp_path: Path, monkeypatch, runtime_site: list[Path]
) -> None:
    """The gateway's interpreter already carries the harness at the lock's pins (a
    distribution that bundles it): pip, and so the host's index, is never asked, which is
    what lets a host whose index refuses an expired credential install at all."""
    site = _runtime_ships(tmp_path / "runtime" / "site-packages")
    runtime_site.append(site)
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []
    home = tmp_path / "home"
    state = install.install_sync(
        home, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    prefix = install.harness_prefix(home)
    assert not [argv for argv in calls if "pip" in argv], calls
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--clear",
        "--without-pip",
        str(install.venv_dir(prefix)),
    ]
    # The venv's own interpreter proves it imports the copy at the lock's pins.
    assert calls[1][:3] == [str(install.venv_python(prefix)), "-I", "-c"]
    assert calls[1][4:] == [f"{name}=={_lock_pin(name)}" for name in _HARNESS_FAMILY]
    assert len(calls) == 2
    copied = install._venv_site_packages(prefix)
    expected = {p.relative_to(site) for p in site.rglob("*") if p.is_file()}
    assert {p.relative_to(copied) for p in copied.rglob("*") if p.is_file()} == expected
    assert all((copied / relative).resolve().is_relative_to(prefix) for relative in expected)
    assert all(
        (copied / relative).read_bytes() == (site / relative).read_bytes() for relative in expected
    )
    assert not list(copied.rglob("*.pth"))
    marker = json.loads((prefix / install.MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["source"] == "runtime"
    assert state.installed is True and install.harness_current(home) is True


def test_a_runtime_copy_needs_neither_pip_nor_the_pillow_build_deps(
    tmp_path: Path, monkeypatch, runtime_site: list[Path]
) -> None:
    """Copying requires no build, so a runtime without pip or ensurepip and an old
    glibc without pillow's build deps is not refused."""
    runtime_site.append(_runtime_ships(tmp_path / "runtime"))
    _gateway_ships(monkeypatch)
    _old_glibc_missing(monkeypatch, build_deps.MISSING_COMPILER, "jpeglib.h")
    state = install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    assert state.installed is True


@pytest.mark.parametrize("off_lock", [{"cdp_use": "0.0.1"}, {"fetch_use": ""}])
def test_a_runtime_off_the_lock_keeps_the_hash_pinned_pip_install(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], off_lock: dict[str, str]
) -> None:
    runtime_site.append(_runtime_ships(tmp_path / "runtime", **off_lock))
    _gateway_ships(monkeypatch, "ensurepip")
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    prefix = install.harness_prefix(tmp_path)
    assert calls[0] == [
        sys.executable,
        "-I",
        "-m",
        "venv",
        "--clear",
        str(install.venv_dir(prefix)),
    ]
    assert calls[1][:5] == [str(install.venv_python(prefix)), "-I", "-m", "pip", "install"]
    assert "--require-hashes" in calls[1] and calls[1][-1] == str(install.LOCK_FILE)
    assert not list(install.venv_dir(prefix).rglob("*.pth"))


def test_a_change_of_source_and_a_repeat_runtime_install_rebuild_the_venv(
    tmp_path: Path, monkeypatch, runtime_site: list[Path]
) -> None:
    """Every runtime install starts empty; the pip path clears runtime-filled packages."""
    prefix = install.harness_prefix(tmp_path)
    _current_install(prefix, venv_pip=True)
    runtime_site.append(_runtime_ships(tmp_path / "runtime"))
    _gateway_ships(monkeypatch, "pip")
    harness_only = (install.COMPONENT_HARNESS,)

    calls: list[list[str]] = []
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    assert calls[0][3:6] == ["venv", "--clear", "--without-pip"] and len(calls) == 2

    calls.clear()
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    assert calls[0][3:6] == ["venv", "--clear", "--without-pip"] and len(calls) == 2

    runtime_site.clear()
    calls.clear()
    install.install_sync(tmp_path, runner=_recording_runner(calls), components=harness_only)
    assert calls[0][3:5] == ["venv", "--clear"] and calls[1][2:4] == ["-m", "pip"]


@pytest.mark.parametrize("update", ["moved", "gone", "off_lock"])
def test_a_copied_venv_stays_current_after_runtime_removal(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], update: str
) -> None:
    """Runtime pruning or replacement does not affect the venv's package files."""
    old = _runtime_ships(tmp_path / "crew-venv-0.13.0" / "site-packages")
    runtime_site.append(old)
    _gateway_ships(monkeypatch, "ensurepip")
    harness_only = (install.COMPONENT_HARNESS,)
    install.install_sync(tmp_path, runner=_ok, components=harness_only)
    assert install.harness_current(tmp_path) is True

    runtime_site.clear()
    shutil.rmtree(old)
    new = tmp_path / "crew-venv-0.14.0" / "site-packages"
    if update == "moved":
        runtime_site.append(_runtime_ships(new))
    elif update == "off_lock":
        runtime_site.append(_runtime_ships(new, cdp_use="0.0.1"))
    assert install.install_state(tmp_path).installed is True
    assert install.harness_current(tmp_path) is True
    copied = install._venv_site_packages(install.harness_prefix(tmp_path))
    for name in _HARNESS_FAMILY:
        assert (copied / name.replace("-", "_") / "__init__.py").is_file()
    assert (copied / "browser_harness" / "daemon.py").read_text(
        encoding="utf-8"
    ) == 'print("original daemon")\n'


def test_runtime_daemon_edits_do_not_change_the_sealed_copy(
    tmp_path: Path, runtime_site: list[Path]
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    (site / "browser_harness" / "daemon.py").write_text(
        'print("edited daemon")\n', encoding="utf-8"
    )
    prefix = install.harness_prefix(tmp_path)
    copied = install._venv_site_packages(prefix) / "browser_harness" / "daemon.py"
    assert copied.is_file(), "the daemon must be copied into the sealed prefix"
    assert copied.resolve().is_relative_to(prefix)
    assert copied.read_text(encoding="utf-8") == 'print("original daemon")\n'


def test_failed_runtime_reinstall_is_not_installed_or_current(
    tmp_path: Path, runtime_site: list[Path]
) -> None:
    runtime_site.append(_runtime_ships(tmp_path / "runtime"))
    install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    assert install.harness_current(tmp_path)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "broken harness import")
        return _ok(argv, timeout)

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_INSTALLING_HARNESS
    assert install.install_state(tmp_path).installed is False
    assert install.harness_current(tmp_path) is False


@pytest.mark.parametrize("name", ["browser-harness", "pillow"])
@pytest.mark.parametrize("record_kind", ["pth", "missing"])
def test_runtime_with_startup_hook_or_missing_record_uses_pip(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], name: str, record_kind: str
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    record = site / f"{name.replace('-', '_')}-{_lock_pin(name)}.dist-info" / "RECORD"
    if record_kind == "missing":
        record.unlink()
    else:
        (site / "startup.pth").write_text("import startup_hook\n", encoding="utf-8")
        with record.open("a", encoding="utf-8") as stream:
            stream.write("startup.pth,,\n")
    _gateway_ships(monkeypatch, "pip")
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    assert any("pip" in argv and "--require-hashes" in argv for argv in calls)
    assert not list(install.venv_dir(install.harness_prefix(tmp_path)).rglob("*.pth"))


def test_runtime_copy_skips_external_paths_and_bytecode(
    tmp_path: Path, runtime_site: list[Path]
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    record = site / f"browser_harness-{_lock_pin('browser-harness')}.dist-info" / "RECORD"
    excluded = [
        str(tmp_path / "absolute.py"),
        "../script",
        "browser_harness/__pycache__/cached.pyc",
    ]
    for relative in excluded:
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("excluded", encoding="utf-8")
    with record.open("a", encoding="utf-8") as stream:
        stream.writelines(f"{relative},,\n" for relative in excluded)
    recorded = next(
        iter(importlib.metadata.distributions(name="browser-harness", path=[str(site)]))
    ).files
    assert recorded is not None
    assert set(excluded).issubset({str(path) for path in recorded})
    files = install._runtime_harness_files()
    assert files
    assert all(
        not path.is_absolute() and ".." not in path.parts and "__pycache__" not in path.parts
        for _, path, _, _ in files
    )
    install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    assert install.harness_current(tmp_path)


@pytest.mark.parametrize("version", ["", "0.0.1"])
def test_runtime_copy_allows_absent_or_different_non_harness_dependencies(
    tmp_path: Path, runtime_site: list[Path], version: str
) -> None:
    site = _runtime_ships(tmp_path / "runtime", pillow=version)
    runtime_site.append(site)
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    assert not any("pip" in argv for argv in calls)
    copied = install._venv_site_packages(install.harness_prefix(tmp_path))
    assert (copied / "pillow" / "__init__.py").is_file() == bool(version)


def test_runtime_copy_failure_clears_the_marker_before_venv_creation(
    tmp_path: Path, runtime_site: list[Path]
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    install.install_sync(tmp_path, runner=_ok, components=(install.COMPONENT_HARNESS,))
    prefix = install.harness_prefix(tmp_path)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert not (prefix / install.MARKER_FILE).exists()
        (site / "browser_harness" / "daemon.py").unlink()
        return _ok(argv, timeout)

    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    assert excinfo.value.stage == install.STAGE_INSTALLING_HARNESS
    assert install.install_state(tmp_path).installed is False
    assert install.harness_current(tmp_path) is False


@requires_symlinks
@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_runtime_source_outside_distribution_uses_pip(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], link_kind: str
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    outside = tmp_path / "runtime-outside"
    outside.mkdir()
    secret = b"fixture-only private content"
    (outside / "private.py").write_bytes(secret)
    if link_kind == "file":
        relative = "browser_harness/private.py"
        (site / relative).symlink_to(outside / "private.py")
    else:
        relative = "browser_harness/private/private.py"
        (site / "browser_harness" / "private").symlink_to(outside, target_is_directory=True)
    record = site / f"browser_harness-{_lock_pin('browser-harness')}.dist-info" / "RECORD"
    with record.open("a", encoding="utf-8") as stream:
        stream.write(f"{relative},,\n")
    _gateway_ships(monkeypatch, "pip")
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    prefix = install.harness_prefix(tmp_path)
    assert all(secret not in path.read_bytes() for path in prefix.rglob("*") if path.is_file())
    assert any("pip" in argv and "--require-hashes" in argv for argv in calls)
    assert not (install._venv_site_packages(prefix) / "browser_harness").exists()


@requires_symlinks
def test_runtime_source_symlink_inside_distribution_is_copied_as_regular_file(
    tmp_path: Path, runtime_site: list[Path]
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    source = site / "browser_harness" / "daemon.py"
    relative = Path("browser_harness") / "alias.py"
    (site / relative).symlink_to(source)
    record = site / f"browser_harness-{_lock_pin('browser-harness')}.dist-info" / "RECORD"
    with record.open("a", encoding="utf-8") as stream:
        stream.write(f"{relative.as_posix()},,\n")
    files = install._runtime_harness_files()
    assert files is not None
    info = source.stat()
    assert (source.resolve(), relative, (info.st_dev, info.st_ino), site.resolve()) in files
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    copied = install._venv_site_packages(install.harness_prefix(tmp_path)) / relative
    assert copied.is_file() and not copied.is_symlink()
    assert copied.read_bytes() == source.read_bytes()
    assert not any("pip" in argv for argv in calls)


@pytest.mark.parametrize("entry", ["C:foo", "\\foo"])
def test_runtime_copy_skips_windows_anchored_entries(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], entry: str
) -> None:
    """Exercise Windows anchor semantics without creating Windows paths on the host."""
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    distributions = importlib.metadata.distributions
    located: list[str] = []

    def locate_file(file):
        located.append(str(file))
        return site / str(file)

    dist = SimpleNamespace(
        version=_lock_pin("browser-harness"),
        files=[importlib.metadata.PackagePath(entry)],
        locate_file=locate_file,
    )
    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda *, name, path: (
            [dist] if name == "browser-harness" else distributions(name=name, path=path)
        ),
    )
    monkeypatch.setattr(
        install, "Path", lambda path: PureWindowsPath(path) if str(path) == entry else Path(path)
    )
    assert not PureWindowsPath(entry).is_absolute() and PureWindowsPath(entry).anchor
    files = install._runtime_harness_files()
    assert files is not None
    assert located == [""]
    assert all(not relative.anchor for _, relative, _, _ in files)


@pytest.mark.parametrize(
    "swap",
    [
        pytest.param("file_symlink", marks=requires_symlinks),
        pytest.param("directory_symlink", marks=requires_symlinks),
        "regular_file",
    ],
)
def test_runtime_source_swap_after_collection_is_refused(
    tmp_path: Path, runtime_site: list[Path], swap: str
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    relative = Path("browser_harness/private/data.bin")
    source = site / relative
    source.parent.mkdir()
    source.write_bytes(b"original package data")
    record = site / f"browser_harness-{_lock_pin('browser-harness')}.dist-info" / "RECORD"
    with record.open("a", encoding="utf-8") as stream:
        stream.write(f"{relative.as_posix()},,\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = b"fixture-only hidden secret\r\n\x1a"
    replacement = outside / source.name
    replacement.write_bytes(secret)
    harness_only = (install.COMPONENT_HARNESS,)
    install.install_sync(tmp_path, runner=_ok, components=harness_only)
    assert install.harness_current(tmp_path)
    prefix = install.harness_prefix(tmp_path)

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[1:4] == ["-I", "-m", "venv"]:
            assert not (prefix / install.MARKER_FILE).exists()
            # Keep the original inode alive so replacement cannot reuse it.
            if swap == "directory_symlink":
                source.parent.rename(site / "original-private")
                source.parent.symlink_to(outside, target_is_directory=True)
            else:
                source.rename(source.with_name("original.bin"))
                if swap == "file_symlink":
                    source.symlink_to(replacement)
                else:
                    replacement.replace(source)
        return _ok(argv, timeout)

    error = None
    try:
        install.install_sync(tmp_path, runner=runner, components=harness_only)
    except install.InstallError as exc:
        error = exc
    assert all(secret not in path.read_bytes() for path in prefix.rglob("*") if path.is_file())
    assert error is not None, "a replaced source must fail the install"
    assert error.stage == install.STAGE_INSTALLING_HARNESS
    assert str(relative) in error.detail
    assert not install.install_state(tmp_path).installed
    assert not install.harness_current(tmp_path)


@pytest.mark.parametrize("source_kind", ["stat_failure", "directory"])
def test_runtime_source_not_regular_at_collection_uses_pip(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], source_kind: str
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    source = site / "browser_harness" / "daemon.py"
    if source_kind == "directory":
        source.unlink()
        source.mkdir()
    else:
        # Metadata can filter missing entries; fail only the installer's own stat.
        def unavailable(path):
            if Path(path) == source:
                raise FileNotFoundError("runtime source disappeared")
            return os.stat(path)

        monkeypatch.setattr(install, "os", SimpleNamespace(**{**vars(os), "stat": unavailable}))
    assert install._runtime_harness_files() is None
    _gateway_ships(monkeypatch, "pip")
    calls: list[list[str]] = []
    install.install_sync(
        tmp_path, runner=_recording_runner(calls), components=(install.COMPONENT_HARNESS,)
    )
    assert any("pip" in argv and "--require-hashes" in argv for argv in calls)


@requires_symlinks
@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="requires descriptor-relative open")
@pytest.mark.parametrize("restore_before_copy", [False, True])
def test_runtime_parent_swap_during_collection_is_refused(
    tmp_path: Path, monkeypatch, runtime_site: list[Path], restore_before_copy: bool
) -> None:
    site = _runtime_ships(tmp_path / "runtime")
    runtime_site.append(site)
    relative = Path("browser_harness/private/data.bin")
    source = site / relative
    source.parent.mkdir()
    source.write_bytes(b"original package data")
    record = site / f"browser_harness-{_lock_pin('browser-harness')}.dist-info" / "RECORD"
    with record.open("a", encoding="utf-8") as stream:
        stream.write(f"{relative.as_posix()},,\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = b"fixture-only hidden secret\r\n\x1a"
    replacement = outside / source.name
    replacement.write_bytes(secret)
    saved = site / "original-private"
    captured: list[os.stat_result] = []

    def swapped_stat(path):
        # Only the installer's stat runs here, after its realpath containment check.
        if Path(path) == source and not captured:
            source.parent.rename(saved)
            source.parent.symlink_to(outside, target_is_directory=True)
            captured.append(os.stat(path))
            return captured[-1]
        return os.stat(path)

    monkeypatch.setattr(install, "os", SimpleNamespace(**{**vars(os), "stat": swapped_stat}))

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[1:4] == ["-I", "-m", "venv"] and restore_before_copy:
            source.parent.unlink()
            saved.rename(source.parent)
        return _ok(argv, timeout)

    error = None
    try:
        install.install_sync(tmp_path, runner=runner, components=(install.COMPONENT_HARNESS,))
    except install.InstallError as exc:
        error = exc
    assert len(captured) == 1
    info = replacement.stat()
    assert (captured[0].st_dev, captured[0].st_ino) == (info.st_dev, info.st_ino)
    prefix = install.harness_prefix(tmp_path)
    assert all(secret not in path.read_bytes() for path in prefix.rglob("*") if path.is_file())
    assert error is not None, "a source checked through a replaced parent must fail the install"
    assert error.stage == install.STAGE_INSTALLING_HARNESS
    assert str(relative) in error.detail
    assert not install.install_state(tmp_path).installed
    assert not install.harness_current(tmp_path)
