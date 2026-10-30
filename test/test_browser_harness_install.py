"""The Harness installer (``browser_harness/install.py``) with a fake subprocess runner."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.browser_harness import HARNESS_VERSION, chromium, install
from kiro_crew.security import paths_lists


def _ok(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # ``python -m venv <prefix>/venv`` creates the interpreter the next step runs.
    if argv[1:4] == ["-I", "-m", "venv"]:
        python = install.venv_python(Path(argv[-1]).parent)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, "", "")


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
    assert str(install.venv_python(prefix)).startswith(str(prefix / "venv"))
    assert calls[1][:4] == [str(install.venv_python(prefix)), "-I", "-m", "pip"]
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
        if k not in ("PYTHONIOENCODING", "PIP_NO_INPUT")
    )
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PIP_NO_INPUT"] == "1"

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
