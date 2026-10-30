"""Provisioning a pod's worktree: the venv, the dist build and the dependency installs.

The tests cover the interpreter search, the probe that vets a candidate before it is chosen,
the uv and pip venv paths, the dist build, and the npm and dev-extra installs. The fixtures
come from ``pod_helpers.py``.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from pod_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    NODE_BIN,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _ready_worktree,
    _systemd_backend,
)

from conftest import host_abs
from kiro_crew import env as env_mod
from kiro_crew import platform_compat
from kiro_crew.pod import provision as prov


def _npm(*args: str) -> list[str]:
    """The argv provisioning is expected to spawn for an npm step."""
    return [f"{NODE_BIN}/npm", *args]


class TestProvision:
    def test_detectors(self, tmp_path: Path) -> None:
        full = _ready_worktree(tmp_path, "full", venv=True, dist=True)
        assert prov.has_venv(full) and prov.has_dist(full)
        novenv = _ready_worktree(tmp_path, "novenv", venv=False, dist=True)
        assert not prov.has_venv(novenv) and prov.has_dist(novenv)

    def test_venv_bin_follows_the_platform_venv_layout(self, tmp_path: Path) -> None:
        """``has_venv`` is called on every platform to report build state in the
        Dev Fleet view (pods themselves stay Linux-only), so a POSIX-only path
        here would report a built Windows worktree as unbuilt."""
        got = prov.venv_bin(tmp_path)
        if platform_compat.IS_WINDOWS:
            assert got == tmp_path / ".venv" / "Scripts" / "kirocrew.exe"
        else:
            assert got == tmp_path / ".venv" / "bin" / "kirocrew"

    def test_provision_venv_only_skips_build(self, tmp_path: Path) -> None:
        co = _ready_worktree(tmp_path, "be-only", venv=True, dist=False)
        with patch.object(prov, "build_dist", side_effect=AssertionError("must not build")):
            assert prov.provision(co, build=False) is True


class TestProvisionBuildPaths:
    def test_ensure_venv_builds_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        monkeypatch.setattr(prov, "_find_uv", lambda: None)

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            if cmd[1:3] == ["-m", "venv"]:
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True

    def test_opted_in_without_uv_runs_the_pip_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIROCREW_PROVISION_USE_UV=1`` on a host where neither the wheel nor
        ``PATH`` has uv is not an error: the ladder returns ``None`` through the
        real ``_find_uv`` and the unchanged ``python -m venv`` + pip sequence
        builds the venv, with no uv command ever attempted."""
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setenv(prov.USE_UV_ENV, "1")
        monkeypatch.setattr(env_mod, "_uv_package", None)
        monkeypatch.setattr(env_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if cmd[1:3] == ["-m", "venv"]:
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True
        assert calls[0][1:3] == ["-m", "venv"], "pip path must run first, not uv"
        assert not any(Path(c[0]).name in ("uv", "uv.exe") for c in calls), calls

    def test_a_uv_that_cannot_be_executed_falls_back_to_pip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wheel locator trusts a path after ``isfile`` only, so a uv with its
        mode bits stripped or on a ``noexec`` mount reaches ``Popen`` and raises
        ``OSError``. That must be the pip fallback, not a traceback out of
        ``ensure_venv``, and nothing may be deleted on the way."""
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        monkeypatch.setattr(prov, "_find_uv", lambda: str(tmp_path / "uv"))
        monkeypatch.setattr(prov.shutil, "rmtree", lambda *a, **k: pytest.fail("deleted"))
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if Path(cmd[0]).name == "uv":
                raise PermissionError(13, "Permission denied", cmd[0])
            if cmd[1:3] == ["-m", "venv"]:
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True
        assert Path(calls[0][0]).name == "uv", "uv was attempted first"
        assert calls[1][1:3] == ["-m", "venv"], "pip path ran after the spawn failure"

    def test_ensure_venv_prefers_uv_with_shared_cache_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``uv`` on the host the venv is built by uv, never by pip, and the
        install carries the two flags that make the shared cache actually share:
        an explicit clone (copy-on-write) link-mode and ``--project`` so ``--group dev``
        resolves against the worktree's pyproject regardless of cwd."""
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        monkeypatch.setattr(prov, "_find_uv", lambda: "/opt/bin/uv")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if cmd[:2] == ["/opt/bin/uv", "venv"]:
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True
        assert [c[:2] for c in calls] == [["/opt/bin/uv", "venv"], ["/opt/bin/uv", "pip"]]
        assert "--seed" in calls[0], "uv venv must seed pip so make/.venv/bin/pip keep working"
        assert (
            "--allow-existing" in calls[0]
        ), "uv venv removes an existing target by default; a racing provisioner's venv must survive"
        assert (
            calls[0][calls[0].index("--link-mode") + 1] == "clone"
        ), "the seeded pip/setuptools come from the same cache and must be clones too"
        assert calls[0][-3:] == ["--python", "/usr/bin/python3.12", str(co / ".venv")]
        install = calls[1]
        assert install[install.index("--link-mode") + 1] == "clone"
        assert install[install.index("--project") + 1] == str(co)
        assert install[install.index("--editable") + 1] == str(co)
        assert install[install.index("--group") + 1] == "dev"

    def test_ensure_venv_uv_failure_falls_back_to_pip_without_deleting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A uv install that dies part-way hands the directory to ``python -m venv``
        as-is. Nothing is deleted: a second provisioner racing on the same
        checkout (CLI + Dev Fleet) may have just finished a good venv there, and
        the pip path already takes over a half-built ``.venv`` from an interrupted
        pip run in exactly the same way."""
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        monkeypatch.setattr(prov, "_find_uv", lambda: "/opt/bin/uv")
        monkeypatch.setattr(
            prov.shutil, "rmtree", lambda *a, **k: pytest.fail("must not delete .venv")
        )
        seen: list[tuple[str, bool]] = []  # (step, uv's marker file still present)
        marker = co / ".venv" / "bin" / "python"

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            if cmd[:2] == ["/opt/bin/uv", "venv"]:
                marker.parent.mkdir(parents=True)
                marker.write_text("")
                seen.append(("uv-venv", marker.exists()))
                return 0
            if cmd[:2] == ["/opt/bin/uv", "pip"]:
                seen.append(("uv-pip", marker.exists()))
                return 1  # e.g. resolver failure
            if cmd[1:3] == ["-m", "venv"]:
                seen.append(("py-venv", marker.exists()))
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
                return 0
            seen.append((Path(cmd[0]).name, marker.exists()))
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_venv(co) is True
        assert [s for s, _ in seen][:3] == ["uv-venv", "uv-pip", "py-venv"]
        assert seen[2] == (
            "py-venv",
            True,
        ), "pip path must take over the existing .venv, not a deleted one"

    def test_find_uv_is_opt_in_and_pip_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv runs only when ``KIROCREW_PROVISION_USE_UV`` is truthy; unset,
        ``0`` and ``false`` all keep the pip default even with uv on PATH. The
        default is unchanged by this change on purpose — flipping it is Phase 2
        of the shared-dependency-cache RFC and waits on that decision."""
        monkeypatch.setattr(env_mod, "_uv_package", None)  # no wheel -> ladder falls to which()
        monkeypatch.setattr(env_mod.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.delenv(prov.USE_UV_ENV, raising=False)
        assert prov._find_uv() is None, "unset must mean pip"
        for off in ("0", "false", ""):
            monkeypatch.setenv(prov.USE_UV_ENV, off)
            assert prov._find_uv() is None, repr(off)
        for on in ("1", "true", "YES"):
            monkeypatch.setenv(prov.USE_UV_ENV, on)
            assert prov._find_uv() == "/usr/bin/uv", on

    def test_find_uv_is_the_shared_ladder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pod provisioning resolves uv through ``kiro_crew.env.resolve_uv`` — the
        one ladder pptx-maker also consumes — so the declared wheel wins over
        PATH and a systemd/launchd gateway with a minimal PATH still finds it."""
        monkeypatch.setenv(prov.USE_UV_ENV, "1")
        wheel_uv = tmp_path / "site" / "uv"
        wheel_uv.parent.mkdir()
        wheel_uv.write_text("")
        monkeypatch.setattr(
            env_mod, "_uv_package", types.SimpleNamespace(find_uv_bin=lambda: str(wheel_uv))
        )
        monkeypatch.setattr(env_mod.shutil, "which", lambda name: "/usr/bin/uv")
        assert prov._find_uv() == str(wheel_uv)

        def missing() -> str:
            raise FileNotFoundError("repackaged without the binary")

        monkeypatch.setattr(env_mod, "_uv_package", types.SimpleNamespace(find_uv_bin=missing))
        assert prov._find_uv() == "/usr/bin/uv"

    def test_ensure_venv_no_python(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": None)
        assert prov.ensure_venv(co) is False

    def test_build_dist_npm_then_stages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        (co / "website").mkdir(parents=True)

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            (co / "website" / "dist").mkdir(parents=True, exist_ok=True)
            (co / "website" / "dist" / "index.html").write_text("<html>")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        # website/dist staged into the served static/dist.
        assert (co / "src" / "kiro_crew" / "static" / "dist" / "index.html").is_file()

    def test_build_dist_restages_over_a_dangling_dist_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DANGLING link at `static/dist` must be replaced, not tripped over.

        `static/dist` is a link on a source install, and Kiro Crew makes
        it itself: `frontend._ensure_tree_dist` publishes exactly this path via
        `platform_compat.symlink_or_junction`, which falls back to a directory
        JUNCTION on Windows because a directory symlink there needs
        SeCreateSymbolicLinkPrivilege. So on an ordinary unelevated box the link
        is a junction.

        The staging block already handles a dangling link — that is what
        `dst.is_symlink()` is doing ahead of `is_dir()`, mirroring the same
        ordering in `frontend._stage_dist`. It handles the dangling SYMLINK. A
        dangling JUNCTION answers False to `is_symlink()`, `is_file()` AND
        `is_dir()` (measured), so every branch is skipped and
        `shutil.copytree` lands on a directory entry that still exists:
        `FileExistsError [WinError 183]`, with no handler up the provisioning
        chain.

        The link is created with the product's own `symlink_or_junction` rather
        than a bare `os.symlink`, so this exercises whichever shape the running
        platform actually produces.
        """
        co = tmp_path / "wt"
        (co / "website").mkdir(parents=True)
        served = co / "src" / "kiro_crew" / "static"
        served.mkdir(parents=True)
        gone = tmp_path / "removed-dist"
        gone.mkdir()
        platform_compat.symlink_or_junction(str(gone), str(served / "dist"))
        gone.rmdir()

        # Guard the guard, through oracles OUTSIDE the module under test: the
        # entry must still BE a link, and must NOT resolve — otherwise
        # `has_dist` short-circuits and nothing below is under test.
        dangling = served / "dist"
        assert platform_compat.is_link_or_junction(dangling)
        assert not dangling.is_dir()
        assert prov.has_dist(co) is False

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            (co / "website" / "dist").mkdir(parents=True, exist_ok=True)
            (co / "website" / "dist" / "index.html").write_text("<html>new")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        assert (served / "dist" / "index.html").is_file()

    def test_build_dist_still_short_circuits_on_a_LIVE_dist_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: a link that still RESOLVES is the common path.

        `has_dist` follows the link, so `build_dist` returns True before it
        reaches the staging block at all. The junction arm added below must not
        change that — a provision on a working source install still does no
        work and touches nothing.
        """
        co = tmp_path / "wt"
        served = co / "src" / "kiro_crew" / "static"
        served.mkdir(parents=True)
        target = tmp_path / "linked-dist"
        target.mkdir()
        (target / "keep-me.html").write_text("<html>theirs", encoding="utf-8")
        platform_compat.symlink_or_junction(str(target), str(served / "dist"))
        assert platform_compat.is_link_or_junction(served / "dist")

        monkeypatch.setattr(prov, "_run", lambda cmd, cwd, env=None: pytest.fail("must not build"))
        assert prov.build_dist(co) is True
        assert platform_compat.is_link_or_junction(served / "dist")
        assert (target / "keep-me.html").read_text(encoding="utf-8") == "<html>theirs"

    def test_build_dist_no_website_dir(self, tmp_path: Path) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        assert prov.build_dist(co) is False

    def test_build_dist_short_circuits_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = _ready_worktree(tmp_path, "wt", venv=False, dist=True)
        monkeypatch.setattr(prov, "_run", lambda cmd, cwd, env=None: 99)  # must not be called
        assert prov.build_dist(co) is True

    def test_provision_full_chain(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "ensure_venv", lambda c: True)
        monkeypatch.setattr(prov, "build_dist", lambda c: True)
        assert prov.provision(co, build=True) is True

    # --- interpreter selection: probed, not guessed --------------------------
    @staticmethod
    def _only_candidates(monkeypatch: pytest.MonkeyPatch, *cands: str) -> None:
        monkeypatch.setattr(prov, "_python_candidates", lambda: iter(cands))

    def test_find_python_takes_the_first_candidate_that_passes_the_probe(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shim = host_abs("shim", "python3.12")
        ok = host_abs("ok", "python3.13")
        self._only_candidates(monkeypatch, shim, ok, host_abs("later"))
        probed: list[str] = []

        def probe(c: str) -> str | None:
            probed.append(c)
            return "exit 127: no such version" if c == shim else None

        monkeypatch.setattr(prov, "_probe_python", probe)
        assert prov._find_python() == ok
        assert probed == [shim, ok]
        # The rejection is said, so a failed provision names what was tried.
        assert f"skipping {shim}: exit 127: no such version" in capsys.readouterr().err

    def test_find_python_probes_each_path_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        a = host_abs("a", "python3")
        b = host_abs("b", "python3")
        self._only_candidates(monkeypatch, a, a, b)
        probed: list[str] = []
        monkeypatch.setattr(prov, "_probe_python", lambda c: probed.append(c) or "no")
        assert prov._find_python() is None
        assert probed == [a, b]

    def test_find_python_is_none_when_every_candidate_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._only_candidates(monkeypatch, host_abs("x"))
        probed: list[str] = []
        monkeypatch.setattr(prov, "_probe_python", lambda c: probed.append(c) or "broken")
        assert prov._find_python() is None
        assert probed == [host_abs("x")]

    def test_ensure_venv_failure_names_the_floor_not_one_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(prov, "_find_python", lambda: None)
        assert prov.ensure_venv(tmp_path) is False
        err = capsys.readouterr().err
        assert "no Python >= 3.12 that can build a venv" in err
        assert "uv python install 3.12" in err

    def test_min_python_is_pyproject_requires_python(self) -> None:
        """The floor the probe enforces is the one the package declares."""
        import tomllib

        root = Path(__file__).resolve().parents[1]
        declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "requires-python"
        ]
        assert declared == f">={prov.MIN_PYTHON[0]}.{prov.MIN_PYTHON[1]}"

    @staticmethod
    def _quiet_host(monkeypatch: pytest.MonkeyPatch, *, windows: bool = False) -> None:
        """No interpreter anywhere but what a test adds."""
        monkeypatch.setattr(prov.platform_compat, "IS_WINDOWS", windows)
        monkeypatch.setattr(prov.sys, "_base_executable", "", raising=False)
        monkeypatch.setattr(prov.sys, "executable", "")
        monkeypatch.setattr(prov.Path, "exists", lambda self: False)
        monkeypatch.setattr(prov.platform_compat, "trusted_system_bin_quiet", lambda name: None)
        monkeypatch.setattr(prov.shutil, "which", lambda exe: None)
        monkeypatch.setattr(prov, "_uv_found_python", lambda: None)

    def test_candidates_take_system_dirs_from_the_platform_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The system bin directories are the platform layer's to know, Windows
        included, so a versioned name is asked of it before ``PATH``."""
        self._quiet_host(monkeypatch)
        asked: list[str] = []
        monkeypatch.setattr(
            prov.platform_compat,
            "trusted_system_bin_quiet",
            lambda name: asked.append(name) or f"/sys/{name}",
        )
        monkeypatch.setattr(prov.shutil, "which", lambda exe: f"/path/{exe}")
        got = list(prov._python_candidates())
        own = f"python3.{sys.version_info[1]}"
        assert asked[0] == own
        assert got[:2] == [f"/sys/{own}", f"/path/{own}"]

    def test_find_python_skips_a_relative_candidate_without_probing_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        relative = os.path.join("bin", "python3.13")
        ok = host_abs("ok", "python3.13")
        self._only_candidates(monkeypatch, relative, ok)
        probed: list[str] = []
        monkeypatch.setattr(prov, "_probe_python", lambda c: probed.append(c) or None)
        assert prov._find_python() == ok
        assert probed == [ok]
        assert f"skipping {relative}: found through a relative PATH entry" in (
            capsys.readouterr().err
        )

    def test_candidates_start_with_this_processes_own_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._quiet_host(monkeypatch)
        monkeypatch.setattr(prov.sys, "_base_executable", "/gw/bin/python3.12", raising=False)
        monkeypatch.setattr(prov.shutil, "which", lambda exe: f"/path/{exe}")
        monkeypatch.setattr(prov, "_uv_found_python", lambda: "/uv/python3.13")
        got = list(prov._python_candidates())
        assert got[0] == "/gw/bin/python3.12"
        assert got[-1] == "/uv/python3.13"
        assert "/path/python3" in got

    def test_candidates_ask_for_the_gateway_minor_first_then_the_floor_upward(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._quiet_host(monkeypatch)
        monkeypatch.setattr(prov.shutil, "which", lambda exe: f"/path/{exe}")
        own = f"/path/python3.{sys.version_info[1]}"
        got = [c for c in prov._python_candidates() if c != "/path/python3"]
        assert got[0] == own
        rest = [c for c in got[1:]]
        assert rest == sorted(rest, key=lambda c: int(c.rsplit(".", 1)[1]))
        assert rest[0] == ("/path/python3.12" if own != "/path/python3.12" else "/path/python3.13")

    def test_find_python_on_a_host_with_no_python_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._quiet_host(monkeypatch)
        monkeypatch.setattr(prov, "_probe_python", lambda c: pytest.fail("nothing to probe"))
        assert prov._find_python() is None

    def test_candidates_ask_the_py_launcher_per_minor_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """python.org on Windows ships ``python.exe`` only, so the launcher is the
        index of installed versions; it is asked for each minor, own one first."""
        self._quiet_host(monkeypatch, windows=True)
        asked: list[str] = []
        monkeypatch.setattr(
            prov, "_find_python_via_launcher", lambda v: asked.append(v) or rf"C:\Py\{v}.exe"
        )
        got = list(prov._python_candidates())
        assert asked[0] == f"3.{sys.version_info[1]}"
        assert "3.12" in asked
        assert got[0] == rf"C:\Py\3.{sys.version_info[1]}.exe"

    def test_candidates_never_run_the_launcher_off_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._quiet_host(monkeypatch)
        monkeypatch.setattr(
            prov, "_find_python_via_launcher", lambda v: pytest.fail("POSIX must not probe py")
        )
        assert list(prov._python_candidates()) == []

    def test_the_launcher_answer_must_name_a_real_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(prov.shutil, "which", lambda exe: r"C:\Windows\py.exe")
        gone = tmp_path / "uninstalled" / "python.exe"
        monkeypatch.setattr(prov.subprocess, "check_output", lambda cmd, **kw: f"{gone}\n")
        assert prov._find_python_via_launcher("3.12") is None
        real = tmp_path / "python.exe"
        real.write_bytes(b"")
        calls: list[list[str]] = []
        monkeypatch.setattr(
            prov.subprocess, "check_output", lambda cmd, **kw: calls.append(cmd) or f"{real}\n"
        )
        assert prov._find_python_via_launcher("3.13") == str(real)
        # EXACTLY the wanted version, never whichever is the launcher's default.
        assert calls[0][:2] == [r"C:\Windows\py.exe", "-3.13"]

    def test_uv_lookup_never_downloads_an_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(prov, "resolve_uv", lambda: "/bin/uv")
        seen: list[list[str]] = []
        monkeypatch.setattr(
            prov.subprocess, "check_output", lambda cmd, **kw: seen.append(cmd) or "/uv/py\n"
        )
        assert prov._uv_found_python() == "/uv/py"
        assert seen == [["/bin/uv", "python", "find", "--no-python-downloads", ">=3.12"]]
        monkeypatch.setattr(prov, "resolve_uv", lambda: None)
        assert prov._uv_found_python() is None


@pytest.mark.skipif(sys.platform == "win32", reason="fake interpreters are POSIX shell scripts")
class TestProbePythonAgainstRealProcesses:
    """``_probe_python`` runs the candidate; these candidates are real executables."""

    @staticmethod
    def _script(path: Path, body: str) -> str:
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def test_a_working_interpreter_passes(self) -> None:
        assert prov._probe_python(sys.executable) is None

    def test_a_shim_with_no_install_behind_it_is_refused_with_its_reason(
        self, tmp_path: Path
    ) -> None:
        """The mise shape: the file exists and is executable, and running it fails."""
        shim = self._script(
            tmp_path / "python3.12",
            'echo "mise ERROR No version is set for python3.12" >&2\nexit 127\n',
        )
        reason = prov._probe_python(shim)
        assert reason is not None and reason.startswith("exit 127")
        assert "No version is set" in reason

    def test_an_interpreter_that_starts_only_with_a_loader_path_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The relocatable shape: it runs in an environment carrying a library path,
        which the provision steps do not carry, so the probe must not either."""
        cand = self._script(
            tmp_path / "python3",
            f'[ -n "$LD_LIBRARY_PATH" ] || {{ echo "libpython3.12.so.1.0: cannot open" >&2; exit 127; }}\n'
            f'exec {sys.executable} "$@"\n',
        )
        monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path))
        reason = prov._probe_python(cand)
        assert reason is not None and "libpython3.12.so.1.0" in reason

    def test_a_venv_whose_interpreter_fails_is_refused(self, tmp_path: Path) -> None:
        """The base starts, the venv built from it does not: the Toolbox case."""
        broken_venv_python = (
            'echo "libpython3.12.so.1.0: cannot open shared object file" >&2; exit 127'
        )
        cand = self._script(
            tmp_path / "python3",
            'for a; do last="$a"; done\n'
            'mkdir -p "$last/bin"\n'
            f"printf '#!/bin/sh\\n{broken_venv_python}\\n' > \"$last/bin/python\"\n"
            'chmod +x "$last/bin/python"\n',
        )
        reason = prov._probe_python(cand)
        assert reason is not None and reason.startswith("exit 127")
        assert "libpython3.12.so.1.0" in reason

    def test_an_interpreter_below_the_floor_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            prov, "_PROBE_SNIPPET", prov._PROBE_SNIPPET.replace("(3, 12)", "(9, 99)")
        )
        assert prov._probe_python(sys.executable) == "older than 3.12"

    def test_a_missing_file_is_refused_not_raised(self, tmp_path: Path) -> None:
        reason = prov._probe_python(str(tmp_path / "nope"))
        assert reason is not None and reason.startswith("could not run")

    def test_a_broken_versioned_shim_on_path_loses_to_a_working_newer_python(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported host: mise's ``python3.12`` shim exists with no 3.12 behind
        it, a 3.13 works, and the gateway's own interpreter cannot build a venv.
        Selecting by name picked the shim and provisioning died in ensurepip."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        self._script(bindir / "python3.12", 'echo "No version is set" >&2\nexit 127\n')
        newer = self._script(bindir / "python3.13", f'exec {sys.executable} "$@"\n')
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(prov.Path, "home", lambda: tmp_path / "nohome")
        monkeypatch.setattr(prov.sys, "_base_executable", str(bindir / "python3.12"), raising=False)
        # raising=False: the same test runs against the name-only lookup it replaced.
        monkeypatch.setattr(prov, "_uv_found_python", lambda: None, raising=False)
        monkeypatch.setattr(prov, "_NAMED_MINORS", (12, 13), raising=False)
        # The host's own system interpreters are not part of this scenario.
        monkeypatch.setattr(prov.Path, "exists", lambda self: False)
        monkeypatch.setattr(prov.platform_compat, "trusted_system_bin_quiet", lambda name: None)
        assert prov._find_python() == newer

    def test_a_python_found_through_a_relative_path_entry_is_not_selected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PATH=bin`` makes ``shutil.which`` answer ``bin/python3.13``. The probe
        passes from the caller's cwd, and the provision steps then run it from
        the checkout, where it does not resolve."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        self._script(bindir / "python3.13", f'exec {sys.executable} "$@"\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", "bin")
        monkeypatch.setattr(prov.Path, "home", lambda: tmp_path / "nohome")
        monkeypatch.setattr(prov.sys, "_base_executable", "", raising=False)
        monkeypatch.setattr(prov.sys, "executable", "")
        monkeypatch.setattr(prov, "_uv_found_python", lambda: None, raising=False)
        monkeypatch.setattr(prov, "_NAMED_MINORS", (13,), raising=False)
        monkeypatch.setattr(prov.Path, "exists", lambda self: False)
        monkeypatch.setattr(prov.platform_compat, "trusted_system_bin_quiet", lambda name: None)
        found = prov._find_python()
        assert found is None or os.path.isabs(found)


class TestProvisionDependencyInstall:
    """npm deps install before the dist build, and dev extras install in
    the venv with a graceful fallback for old pip."""

    def _venv_seeding_run(self, co: Path, calls: list[list[str]], group_fails: bool = False):
        """Return a fake _run that records calls and materializes the venv bin on
        `python -m venv`, so has_venv() passes. Optionally fail `--group` cmds."""

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if cmd[1:3] == ["-m", "venv"]:
                # Materialize the venv entry point at the layout THIS platform
                # actually uses, so has_venv() and the pod runtime agree.
                b = prov.venv_bin(co)
                b.parent.mkdir(parents=True, exist_ok=True)
                b.write_text("#!/bin/sh\n")
                b.chmod(0o755)
            if group_fails and "--group" in cmd:
                return 1
            return 0

        return fake_run

    # ---- #229: website npm deps ----

    def test_npm_ci_when_node_modules_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", lambda cmd, cwd, env=None: calls.append(cmd) or 0)
        assert prov.ensure_node_modules(website) is True
        assert _npm("ci") in calls
        # ci succeeded → no fallback (non-mutating install never runs)
        assert _npm("install", "--no-package-lock") not in calls

    def test_node_modules_skipped_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        (website / "node_modules" / ".bin").mkdir(parents=True)
        (website / "node_modules" / ".bin" / "tsc").write_text("#!/bin/sh\n")

        def boom(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            raise AssertionError(f"must not run npm when node_modules present: {cmd}")

        monkeypatch.setattr(prov, "_run", boom)
        assert prov.ensure_node_modules(website) is True

    def test_npm_install_fallback_when_ci_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        website = tmp_path / "wt" / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            return 1 if cmd == _npm("ci") else 0  # ci fails, install succeeds

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.ensure_node_modules(website) is True
        # Fallback must be NON-MUTATING: --no-package-lock so the tracked
        # website/package-lock.json is never rewritten (would dirty the worktree).
        assert _npm("ci") in calls
        assert _npm("install", "--no-package-lock") in calls
        assert _npm("install") not in calls  # plain (mutating) install never runs

    def test_build_dist_installs_node_modules_before_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        website = co / "website"
        website.mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
            calls.append(cmd)
            if cmd == _npm("run", "build"):
                (website / "dist").mkdir(parents=True, exist_ok=True)
                (website / "dist" / "index.html").write_text("<html>")
            return 0

        monkeypatch.setattr(prov, "_run", fake_run)
        assert prov.build_dist(co) is True
        assert calls.index(_npm("ci")) < calls.index(_npm("run", "build"))

    # ---- #230: venv dev extras ----

    def test_pip_group_dev_attempted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", self._venv_seeding_run(co, calls))
        assert prov.ensure_venv(co) is True
        assert any(c[-2:] == ["--group", "dev"] for c in calls)

    def test_pip_group_dev_fallback_on_old_pip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = tmp_path / "wt"
        co.mkdir()
        monkeypatch.setattr(prov, "_find_python", lambda version="3.12": "/usr/bin/python3.12")
        calls: list[list[str]] = []
        monkeypatch.setattr(prov, "_run", self._venv_seeding_run(co, calls, group_fails=True))
        assert prov.ensure_venv(co) is True
        assert any("--group" in c for c in calls)  # attempted --group dev
        pip = str(prov.venv_bin_dir(co) / ("pip.exe" if platform_compat.IS_WINDOWS else "pip"))
        assert [pip, "install", "--editable", str(co)] in calls  # then fell back
