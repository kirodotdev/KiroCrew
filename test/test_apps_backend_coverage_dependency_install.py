"""Unit coverage for :mod:`kiro_crew.apps.backend`: the spawn body's ``requirements.txt`` install.

Provisioning ``requirements.txt`` into the per-app deps dir (the digest stamp, the deps
lock, the staging swap and the pins against a linked or swapped directory), the
``deps_boot`` shim that puts those deps first on the path, how a failed pip run
reaches the backend log, redacted, and how the crash tail decodes the child's bytes.
The npm install is covered with the Node dispatch in ``_dispatch``.

One topic of the ``test_apps_backend_coverage*.py`` family; the doubles and fixtures
the family shares come from ``apps_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import builtins
import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import _capture_popen, _manifest, _record_runs, _StopSpawn

import kiro_crew.apps.backend as bmod

# ---------------------------------------------------------------------------
# Spawn body: dependency installation
# ---------------------------------------------------------------------------


class TestDependencyInstall:
    def test_requirements_txt_provisions_the_deps_dir_without_a_venv(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provisioning is a single `pip install --target` into a staging dir
        that is swapped live on success (with the requirements hash stamped).

        Never `-m venv`: a packaged install's bundled interpreter ships pip
        but no ensurepip, so venv creation dies after building the directory
        skeleton - which the venv-first interpreter policy then prefers while
        it holds none of the app's dependencies."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        assert not any("venv" in argv for argv in runs), runs
        pip_argv = next(argv for argv in runs if "install" in argv)
        target_idx = pip_argv.index("--target")
        # staging names are UNIQUE per transaction (pid+nonce suffix), so a
        # data/ swap can never redirect a fixed name's cleanup or fill
        _target = pip_argv[target_idx + 1]
        assert _target.startswith(str(spawn_root / "data" / bmod._DEPS_STAGING_NAME)), pip_argv
        # Success swapped the staging dir live and stamped the requirements
        # hash, so the next start with an unchanged file can skip pip.
        deps_dir = app_deps_dir(spawn_root)
        assert deps_dir.is_dir()
        assert not list((spawn_root / "data").glob(f"{bmod._DEPS_STAGING_NAME}*"))
        assert (deps_dir / bmod._DEPS_STAMP_NAME).read_text() == bmod._deps_digest(b"requests\n")

    def test_an_out_of_root_requirements_symlink_is_refused_without_reading_it(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app dir is app-writable, so requirements.txt can be a planted
        symlink to a file outside the app root. Provisioning must refuse it:
        hashing the target's bytes would turn the on-disk stamp into a
        sha256 content oracle for that file, and pip would install from a
        path the app cannot otherwise reach. The refusal is surfaced as a
        provisioning error, not swallowed."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        outside = tmp_path / "outside-secret.txt"
        outside.write_bytes(b"host-secret==1.0\n")
        (spawn_root / "server.py").write_text("x = 1\n")
        try:
            _os.symlink(outside, spawn_root / "requirements.txt")
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        # No pip ran, no deps dir was created, and no stamp exists anywhere
        # that could disclose a digest of the outside file's bytes.
        assert not any("install" in argv for argv in runs), runs
        from kiro_crew.apps.interpreter import app_deps_dir

        assert not app_deps_dir(spawn_root).exists()
        assert not list((spawn_root / "data").glob(f"{bmod._DEPS_STAGING_NAME}*"))

    def test_an_in_tree_requirements_symlink_provisions_normally(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """requirements.txt -> requirements/prod.txt is legitimate app
        layout: a link whose strict resolution stays inside the app root is
        accepted, read via its resolved target (itself no-follow-bound), and
        provisions normally."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        (spawn_root / "server.py").write_text("x = 1\n")
        reqs = spawn_root / "requirements"
        reqs.mkdir()
        (reqs / "prod.txt").write_bytes(b"requests\n")
        try:
            _os.symlink(
                _os.path.join("requirements", "prod.txt"),
                spawn_root / "requirements.txt",
            )
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-inlink", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_pip_runs_from_the_app_root_on_the_apps_own_requirements(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip runs with cwd=app root, so a relative reference (`-e ./lib`)
        resolves inside the app dir rather than the gateway's working
        directory - and for VOLATILE requirements (the only kind that can
        carry file references) -r points at the app's OWN requirements.txt,
        so a nested include resolves beside it (a relocated copy would
        break includes)."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "base.txt").write_bytes(b"requests\n")
        (spawn_root / "requirements.txt").write_bytes(b"-r base.txt\n")
        _record_runs(monkeypatch)
        seen: dict[str, Any] = {}

        def _spy_run(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                seen["cwd"] = kwargs.get("cwd")
                seen["r_path"] = argv[list(argv).index("-r") + 1]
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _spy_run)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))
        assert seen["cwd"] == str(spawn_root), seen
        assert seen["r_path"] == str(spawn_root / "requirements.txt"), seen

    def test_an_editable_install_is_retained_and_swapped_live(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editable installs materialise as __editable__*.pth hooks, which
        the deps_boot shim processes via site.addsitedir - so provisioning
        RETAINS them and swaps the tree live (the refusal that predated the
        shim is gone)."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"-e ./lib\n")
        _record_runs(monkeypatch)

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                argv = list(argv)
                staging = argv[argv.index("--target") + 1]
                (__import__("pathlib").Path(staging) / "__editable__.lib-1.0.pth").write_text(
                    "hook\n"
                )
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _fake_pip)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-editable", _manifest("server.py"))
        deps_dir = app_deps_dir(spawn_root)
        assert (deps_dir / "__editable__.lib-1.0.pth").is_file()
        log_path = spawn_root / "data" / "logs" / "backend.log"
        if log_path.exists():
            assert "editable requirements" not in log_path.read_text()

    def test_volatile_requirement_forms_disable_the_stamp(self) -> None:
        """Any line whose resolution can change while the line does not
        (file refs attached or spaced, editables, local paths, VCS/URL,
        direct references) must disable stamp reuse - the digest cannot
        prove the resolved set unchanged for them."""
        vol = bmod._requirements_volatile
        assert vol(b"-rbase.txt\n")
        assert vol(b"-r base.txt\n")
        assert vol(b"-c pins.txt\n")
        assert vol(b"--requirement=base.txt\n")
        assert vol(b"-e ./lib\n")
        assert vol(b"./lib\n")
        assert vol(b"~/wheels/pkg.whl\n")
        assert vol(b"pkg @ https://host/pkg.whl\n")
        assert vol(b"git+https://host/repo.git#egg=pkg\n")
        assert vol(b"wheels/pkg.whl\n")  # bare relative path, no ./ prefix
        assert vol(b"vendor.whl\n")  # bare archive FILENAME, no separator
        assert vol(b"-f wheelhouse\n")  # find-links: local wheel content can change
        assert vol(b"--find-links wheelhouse\n")
        assert vol(b"--no-index\n")  # resolution-location options
        assert vol(b"Vendor-1.0.TAR.GZ\n")  # case-insensitive suffix
        assert not vol(b"requests==2.31.0\n")  # index spec stays stampable
        assert vol(b"wheels\\pkg.whl\n")
        assert not vol(b"requests==2.32.0\n# -r not-a-directive in a comment\n")
        assert not vol(b"")

    def test_deps_boot_pth_imports_resolve_deps_first(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A .pth import line executes DURING site processing and caches its
        resolution in sys.modules - so the deps dir must already sit at its
        final front position when addsitedir runs. A gateway copy of a
        colliding name earlier on sys.path must NOT win (no later reordering
        can evict a wrongly cached module)."""
        from kiro_crew.apps import deps_boot

        gateway = tmp_path / "gateway"
        gateway.mkdir()
        (gateway / "collide_probe_7901.py").write_text("MARKER = 'gateway'\n")
        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "collide_probe_7901.py").write_text("MARKER = 'dep'\n")
        pth_proof = tmp_path / "pth_proof.txt"
        (deps / "hook.pth").write_text(
            "import collide_probe_7901, pathlib; "
            f"pathlib.Path({str(pth_proof)!r}).write_text(collide_probe_7901.MARKER)\n"
        )
        script = tmp_path / "noop.py"
        script.write_text("pass\n")
        patched = [str(gateway), *sys.path]  # gateway-like entry ahead of the shim's work
        monkeypatch.setattr(sys, "path", patched)
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        try:
            deps_boot.main([str(deps), str(script)])
        finally:
            sys.modules.pop("collide_probe_7901", None)
        assert pth_proof.read_text() == "dep", pth_proof.read_text()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX arm of the exe gate")
    def test_deps_boot_posix_exe_named_script_keeps_deps(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On POSIX a *.exe name is just a python script (only python targets
        reach the shim): it must take the script dispatch - the embedded-
        launcher arm's execv fallback would restart its shebang interpreter
        WITHOUT the deps path."""
        from kiro_crew.apps import deps_boot

        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "exe_dep_7901.py").write_text("MARKER = 'dep'\n")
        exe_proof = tmp_path / "exe_proof.txt"
        exe = tmp_path / "tool.exe"
        exe.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib\n"
            "import exe_dep_7901\n"
            f"pathlib.Path({str(exe_proof)!r}).write_text(exe_dep_7901.MARKER)\n"
        )
        execvs: list = []
        monkeypatch.setattr(deps_boot.os, "execv", lambda *a: execvs.append(a))
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        try:
            deps_boot.main([str(deps), str(exe)])
        finally:
            sys.modules.pop("exe_dep_7901", None)
        assert execvs == [], "POSIX exe-named script must not be handed to execv"
        assert exe_proof.read_text() == "dep"

    def test_deps_boot_windows_launcher_arm(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pip embedded-launcher arm behind the Windows gate, driven on
        any platform through an os proxy: a zip-appended exe's __main__.py
        stub runs in-process with the deps placed, and a non-zip exe is
        handed to execv exactly as an unshimmed launch would."""
        import zipfile as _zf

        from kiro_crew.apps import deps_boot

        class _ExecvCalled(Exception):
            pass

        class _NtOs:
            name = "nt"

            @staticmethod
            def execv(path: str, args: list) -> None:
                raise _ExecvCalled(path)

            def __getattr__(self, attr: str) -> Any:
                return getattr(os, attr)

        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "zip_dep_7901.py").write_text("MARKER = 'dep'\n")
        proof = tmp_path / "zip_proof.txt"
        exe = tmp_path / "console.exe"
        with _zf.ZipFile(exe, "w") as z:
            z.writestr(
                "__main__.py",
                "import pathlib, sys\n"
                "import zip_dep_7901\n"
                f"pathlib.Path({str(proof)!r}).write_text(zip_dep_7901.MARKER + sys.argv[1])\n",
            )
        monkeypatch.setattr(deps_boot, "os", _NtOs())
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        importlib.invalidate_caches()
        try:
            deps_boot.main([str(deps), str(exe), "zargv"])
        finally:
            sys.modules.pop("zip_dep_7901", None)
        assert proof.read_text() == "depzargv"

        # Not a zip after all (the resolver gates on is_zipfile but the file
        # can change between resolution and launch): handed straight to
        # execv, never dispatched as python.
        native = tmp_path / "native.exe"
        native.write_bytes(b"MZx90")
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        with pytest.raises(_ExecvCalled):
            deps_boot.main([str(deps), str(native), "n"])

    def test_deps_boot_drops_its_own_script_dir(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path-launch shape: CPython puts the shim script's OWN directory at
        sys.path[0], which would let an app import gateway modules by bare
        name - the shim must drop it before anything resolves."""
        from kiro_crew.apps import deps_boot

        own = os.path.dirname(os.path.abspath(deps_boot.__file__))
        deps = tmp_path / "deps"
        deps.mkdir()
        script = tmp_path / "noop2.py"
        script.write_text("pass\n")
        monkeypatch.setattr(sys, "path", [own, *sys.path])
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main([str(deps), str(script)])
        assert sys.path[0] != own

    def test_deps_boot_main_runs_a_script_and_a_module_in_process(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-process coverage of the shim's three arms: script dispatch
        (with the script dir at sys.path[0]), -m module dispatch (module
        resolved through the addsitedir'd deps dir, moved to the front),
        and the usage error."""
        from kiro_crew.apps import deps_boot

        _clean_path = list(sys.path)  # arms mutate the patched list; reset from this
        deps = tmp_path / "deps"
        deps.mkdir()
        (deps / "dep_mod.py").write_text("MARKER = 'dep'\n")
        proof = tmp_path / "proof.txt"
        script = tmp_path / "sub" / "entry.py"
        script.parent.mkdir()
        (script.parent / "sib.py").write_text("S = 'sib'\n")
        script.write_text(
            "import pathlib, sys\n"
            "import dep_mod, sib\n"
            f"pathlib.Path({str(proof)!r}).write_text("
            "dep_mod.MARKER + sib.S + sys.argv[1])\n"
        )
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main([str(deps), str(script), "argved"])
        assert proof.read_text() == "depsibargved"
        assert sys.path[0] == str(script.parent)

        mod_proof = tmp_path / "mod_proof.txt"
        (deps / "runnable_mod.py").write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(mod_proof)!r}).write_text('ran' + sys.argv[1])\n"
        )
        # The first arm's import built a FileFinder for the deps dir whose
        # listing cache has mtime granularity - a module written within the
        # same granule is invisible to the second arm without this.
        importlib.invalidate_caches()
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main([str(deps), "-m", "runnable_mod", "x"])
        assert mod_proof.read_text() == "ranx"

        # -c arm: python -c parity (argv[0] is "-c", cwd at sys.path[0]),
        # module resolved through the addsitedir'd deps dir. The arm
        # installs a fresh __main__ module (production runs in a dedicated
        # process); restore the test runner's afterwards.
        c_proof = tmp_path / "c_proof.txt"
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        deps_boot.main(
            [
                str(deps),
                "-c",
                "import pathlib, sys\n"
                "import dep_mod\n"
                f"pathlib.Path({str(c_proof)!r}).write_text(dep_mod.MARKER + sys.argv[1])",
                "cargv",
            ]
        )
        assert c_proof.read_text() == "depcargv"

        # PLACEMENT parity: the launch entry precedes the deps entries, so
        # an app-local module colliding with a dependency name resolves to
        # the app's own file exactly like a plain launch
        (deps / "sib.py").write_text("S = 'DEPSIB'\n")  # collides with app sib.py
        order_proof = tmp_path / "order_proof.txt"
        script2 = tmp_path / "sub" / "entry2.py"
        script2.write_text(
            "import pathlib\n"
            "import sib\n"
            f"pathlib.Path({str(order_proof)!r}).write_text(sib.S)\n"
        )
        monkeypatch.setattr(sys, "path", list(_clean_path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        deps_boot.main([str(deps), str(script2)])
        assert order_proof.read_text() == "sib", order_proof.read_text()

        # safe_path (-P/-I): the shim must not restore the launch entry.
        # A proxy, not a bare object: sys.flags is process-global and other
        # code (runpy) reads .verbose etc. during the run.
        _real_flags = sys.flags

        class _Flags:
            safe_path = True

            def __getattr__(self, attr):
                return getattr(_real_flags, attr)

        monkeypatch.setattr(sys, "path", list(_clean_path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        monkeypatch.setattr(deps_boot.sys, "flags", _Flags())
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        sp_proof = tmp_path / "sp_proof.txt"
        deps_boot.main(
            [
                str(deps),
                "-c",
                "import pathlib, sys\n"
                f"pathlib.Path({str(sp_proof)!r}).write_text("
                "'|'.join(sys.path[:2]))",
            ]
        )
        parts = sp_proof.read_text().split("|")
        assert parts[0] == str(deps), parts  # deps first, NO '' cwd entry
        monkeypatch.setattr(deps_boot.sys, "flags", _real_flags)

        # embedded-exe arm: pip's Windows launcher carries the console
        # script as an appended ZIP; the shim extracts __main__.py and
        # dispatches it after addsitedir
        import zipfile as _zipfile

        exe_proof = tmp_path / "exe_proof.txt"
        fake_exe = tmp_path / "embtool.exe"
        with open(fake_exe, "wb") as fh:
            fh.write(b"MZ fake native prefix\n")
            with _zipfile.ZipFile(fh, "a") as zf:
                zf.writestr(
                    "__main__.py",
                    "import pathlib, sys\n"
                    "import dep_mod\n"
                    f"pathlib.Path({str(exe_proof)!r}).write_text(dep_mod.MARKER + sys.argv[1])\n",
                )
        monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
        monkeypatch.setattr(sys, "path", list(_clean_path))
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        importlib.invalidate_caches()
        deps_boot.main([str(deps), str(fake_exe), "exearg"])
        assert exe_proof.read_text() == "depexearg"

        with pytest.raises(SystemExit) as exc1:
            deps_boot.main([])
        assert exc1.value.code == 2
        with pytest.raises(SystemExit) as exc2:
            deps_boot.main([str(deps), "-m"])
        assert exc2.value.code == 2

    def test_the_deps_boot_shim_processes_pth_files_with_deps_first(self, tmp_path: Any) -> None:
        """The whole reason the shim exists: PYTHONPATH never processes .pth
        files, site.addsitedir does. A deps tree whose package arrives ONLY
        via a .pth redirect must import under the shim - and the deps
        entries must sit ahead of the gateway env on sys.path (the
        precedence the PYTHONPATH transport had)."""
        deps = tmp_path / "deps"
        hidden = deps / "hidden"
        hidden.mkdir(parents=True)
        (hidden / "pth_only_pkg.py").write_text("VIA_PTH = True\n")
        (deps / "redirect.pth").write_text("hidden\n")
        subdir = tmp_path / "app"
        subdir.mkdir()
        (subdir / "sibling.py").write_text("S = 1\n")
        script = subdir / "entry.py"
        script.write_text(
            "import json, sys\n"
            "import pth_only_pkg\n"
            "import sibling  # direct-script parity: script dir on sys.path[0]\n"
            "print(json.dumps({'via_pth': pth_only_pkg.VIA_PTH,"
            " 'sibling': sibling.S}))\n"
        )
        proc = subprocess.run(
            [
                sys.executable,
                # Absolute SCRIPT path, not -m: the -m spelling imports the
                # kiro_crew package and Python writes __pycache__ into the
                # CHECKOUT - a test must not leave artifacts outside
                # tmp_path. (Production spells it by path for its own
                # reason: session-cwd shadowing.)
                os.path.abspath(bmod._deps_boot_module.__file__),
                str(deps),
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        assert '"via_pth": true' in proc.stdout, proc.stdout
        assert '"sibling": 1' in proc.stdout, proc.stdout

    def test_a_python_backend_with_deps_launches_through_the_shim(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_with_user_site,
    ) -> None:
        """A provisioned backend spawns via deps_boot (which addsitedir()s
        the deps dir, processing .pth) rather than a raw interpreter+entry
        argv - and only for the gateway interpreter."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))
        _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-shim", _manifest("server.py"))
        argv = seen["argv"]
        assert argv[0] == sys.executable, argv
        # absolute-path spelling: an app-root kiro_crew.py must not be able
        # to shadow the shim for -m resolution under cwd=app root
        assert argv[1] == "-s", argv
        assert argv[2].endswith("deps_boot.py"), argv
        assert argv[3] == str(deps_dir), argv
        assert argv[4].endswith("server.py"), argv

    def test_non_volatile_requirements_install_from_a_snapshot(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip re-opens the requirements path; a concurrent rewrite after
        hashing would install replacement contents under the ORIGINAL
        digest's stamp. When the stamp will be trusted, pip installs from an
        immutable snapshot of the very bytes the digest covers."""
        (spawn_root / "server.py").write_text("x = 1\n")
        req = b"requests==2.31.0\n"
        (spawn_root / "requirements.txt").write_bytes(req)
        _record_runs(monkeypatch)
        seen: dict[str, Any] = {}

        def _spy_run(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                argv = list(argv)
                r_path = argv[argv.index("-r") + 1]
                seen["r_path"] = r_path
                seen["r_bytes"] = __import__("pathlib").Path(r_path).read_bytes()
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _spy_run)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-snap", _manifest("server.py"))
        assert "snapshot" in seen["r_path"], seen
        assert str(spawn_root / "data") in seen["r_path"], seen
        assert seen["r_bytes"] == req, seen

    def test_pip_reads_the_resolved_target_of_an_in_tree_symlink(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip resolves a nested include (`-r base.txt`) relative to the
        requirements FILE - handing it the symlink path would resolve
        includes beside the LINK instead of its target. The -r argument must
        be the validated resolved path."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        (spawn_root / "server.py").write_text("x = 1\n")
        real = spawn_root / "config"
        real.mkdir()
        (real / "base.txt").write_bytes(b"requests\n")
        # an include makes the requirements VOLATILE, so pip reads the live
        # validated path (where the include resolves) instead of a snapshot
        (real / "requirements.txt").write_bytes(b"-r base.txt\n")
        try:
            _os.symlink(real / "requirements.txt", spawn_root / "requirements.txt")
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-reqlink", _manifest("server.py"))
        pip_argv = next(argv for argv in runs if "install" in argv)
        r_val = pip_argv[pip_argv.index("-r") + 1]
        assert r_val == str(real / "requirements.txt"), pip_argv

    @pytest.mark.parametrize("existing", [False, True])
    @pytest.mark.parametrize("pinned", [False, True])
    def test_deps_lock_is_created_exclusively_or_opened_without_create(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, existing: bool, pinned: bool
    ) -> None:
        import os

        if pinned and not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("directory-relative opens are unavailable")
        if not pinned:
            monkeypatch.setattr(bmod.pinned_fs, "supports_pinned_walk", lambda: False)
        parent = spawn_root / "data"
        parent.mkdir()
        lock = parent / ".kirocrew-deps.lock"
        (spawn_root / "requirements.txt").write_text("requests\n")
        if existing:
            lock.write_text("lock contents stay intact\n")
        calls = []
        real_open = os.open

        def record_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock"):
                calls.append((flags, dir_fd))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        # Wrapping os.open must not turn off the pinned branch under test.
        if real_open in os.supports_dir_fd:
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {record_open})
        monkeypatch.setattr(os, "open", record_open)
        monkeypatch.setattr(bmod, "_provision_app_deps_locked", lambda *args: "")
        assert bmod.provision_app_deps("deps-create", spawn_root) == ""
        assert len(calls) == (2 if existing else 1)
        assert (calls[0][1] is not None) == pinned
        assert calls[0][0] & os.O_CREAT
        assert calls[0][0] & os.O_EXCL
        for flags, _fd in calls:
            assert flags & os.O_RDWR
            assert not flags & os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                assert flags & os.O_NOFOLLOW
        if existing:
            assert not calls[1][0] & (os.O_CREAT | os.O_EXCL)
            assert calls[1][1] == calls[0][1]
            assert lock.read_text() == "lock contents stay intact\n"

    def test_a_lock_that_disappears_before_reopen_fails_closed(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        (spawn_root / "requirements.txt").write_text("requests\n")
        calls = []
        provisioned = []
        real_open = os.open

        def race_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock"):
                calls.append(flags)
                if len(calls) == 1:
                    # A contender created the file, then removed it before reopen.
                    raise FileExistsError("lock existed at exclusive create")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        if real_open in os.supports_dir_fd:
            monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {race_open})
        monkeypatch.setattr(os, "open", race_open)
        monkeypatch.setattr(
            bmod, "_provision_app_deps_locked", lambda *args: provisioned.append(args)
        )
        error = bmod.provision_app_deps("deps-vanished", spawn_root)
        assert "Failed to serialize dependency provisioning" in error
        assert len(calls) == 2
        assert not calls[1] & (os.O_CREAT | os.O_EXCL)
        assert provisioned == []
        assert not (spawn_root / "data" / ".kirocrew-deps.lock").exists()

    def test_an_existing_lock_symlink_is_not_followed(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("this case exercises O_NOFOLLOW")
        parent = spawn_root / "data"
        parent.mkdir()
        target = spawn_root / "unchanged.txt"
        target.write_text("keep this content", encoding="utf-8")
        lock = parent / ".kirocrew-deps.lock"
        lock.symlink_to(target)
        (spawn_root / "requirements.txt").write_text("requests\n", encoding="utf-8")
        provisioned = []
        monkeypatch.setattr(
            bmod, "_provision_app_deps_locked", lambda *args: provisioned.append(args)
        )
        assert bmod.provision_app_deps("deps-link", spawn_root)
        assert provisioned == []
        assert target.read_text(encoding="utf-8") == "keep this content"
        assert lock.is_symlink()

    def _stub_pip(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Record each --target pip is pointed at, and populate it.

        ``wrap_argv`` is stubbed for the same reason the ``spawn_root`` fixture
        stubs it: it fail-closes where no OS sandbox backend is available, which
        is a property of the host rather than of the paths under test.
        """
        targets: list[str] = []
        monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **_kw: (list(argv), None))

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                argv = list(argv)
                target = argv[argv.index("--target") + 1]
                targets.append(target)
                (Path(target) / "pkg.py").write_text("x = 1\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _fake_pip)
        return targets

    def test_provisioning_through_a_linked_home_reaches_the_staging_pin(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole transaction has to survive a linked ancestor, not just the data pin.

        Provisioning pins twice - the data directory and, one level deeper, the
        staging tree pip installs into. Both walks refuse any link they meet, so
        a home reached through one fails at whichever pin the caller did not
        canonicalise for. Driving the real transaction is what covers the second.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        real_home = tmp_path / "real-home"
        app_root = real_home / "crew" / "apps" / "demo"
        app_root.mkdir(parents=True)
        (app_root / "requirements.txt").write_bytes(b"requests\n")
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home)
        targets = self._stub_pip(monkeypatch)

        error = bmod.provision_app_deps("demo", linked_home / "crew" / "apps" / "demo")

        assert error == "", error
        assert targets, "pip never ran, so the staging pin was never reached"
        assert (bmod.app_deps_dir(app_root) / "pkg.py").is_file()

    def test_provisioning_refuses_a_link_at_the_app_directory(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A link at the app's own name must be refused, at every pin in the run.

        ``apps/<name>`` is app-writable, so canonicalising it would let a link
        planted there redirect the gateway's own staging writes into another
        app's tree. The refusal has to hold for the staging pin too, which sits
        a level deeper than the data pin and so splits the path differently.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        apps = tmp_path / "crew" / "apps"
        victim = apps / "victim"
        (victim / "data").mkdir(parents=True)
        (victim / "requirements.txt").write_bytes(b"requests\n")
        (apps / "attacker").symlink_to(victim)
        targets = self._stub_pip(monkeypatch)

        error = bmod.provision_app_deps("attacker", apps / "attacker")

        assert error, "provisioning through a linked app directory must be refused"
        assert not targets, "pip ran, so a write was already aimed through the link"
        assert not list(
            (victim / "data").glob(".kirocrew-deps*")
        ), "the refusal came too late: the victim tree already carries staging"

    def test_concurrent_provisioning_is_serialized_by_the_deps_lock(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backend spawn and a backend-less registration (or two
        registrations) can provision the same app concurrently; unserialized
        they delete each other's staging tree and both fail. The per-app
        file lock admits one transaction at a time - and the stamp check
        runs inside it, so the waiter skips pip on the winner's stamp."""
        import threading
        import time as _time

        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        inside = []
        overlap = []
        installs = []
        ready = threading.Barrier(2)
        real_open = bmod.os.open

        def concurrent_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith(".kirocrew-deps.lock") and flags & bmod.os.O_CREAT:
                ready.wait(timeout=5)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        if real_open in bmod.os.supports_dir_fd:
            monkeypatch.setattr(
                bmod.os, "supports_dir_fd", bmod.os.supports_dir_fd | {concurrent_open}
            )
        monkeypatch.setattr(bmod.os, "open", concurrent_open)

        def _fake_pip(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                installs.append(True)
                if inside:
                    overlap.append(True)
                inside.append(True)
                # flake-ok: a pre-existing 150 ms hold inside the fake pip so two installs would overlap if the lock failed; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- a pre-existing hold inside the fake pip, moved verbatim; tracked for a de-flake pass
                _time.sleep(0.15)
                argv = list(argv)
                staging = argv[argv.index("--target") + 1]
                (__import__("pathlib").Path(staging) / "pkg.py").write_text("x = 1\n")
                inside.pop()
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _fake_pip)
        errs: list[str] = []

        def _call() -> None:
            errs.append(bmod.provision_app_deps("deps-race", spawn_root))

        threads = [threading.Thread(target=_call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(not t.is_alive() for t in threads), "dependency provisioning deadlocked"
        assert errs == ["", ""], errs
        assert not overlap, "two pip transactions ran concurrently"
        assert len(installs) == 1, "the waiter must reuse the completed install"
        from kiro_crew.apps.interpreter import app_deps_dir

        assert (app_deps_dir(spawn_root) / "pkg.py").is_file()

    def test_the_pinned_dir_detects_a_mid_transaction_swap(self, tmp_path: Any) -> None:
        """The link pre-check is a TOCTOU window: a running app can swap
        data/ AFTER validation. The pin holds the directory open and verify()
        refuses once the path names a different inode - the renames go
        through the held fd and cannot be redirected at all."""
        if bmod.platform_compat.IS_WINDOWS:
            pytest.skip("dir pinning is POSIX-only (symlink creation is privileged on Windows)")
        real = tmp_path / "data"
        real.mkdir()
        pin = bmod._PinnedDir(real)
        try:
            pin.verify()  # untouched: passes
            real.rename(tmp_path / "moved-away")
            (tmp_path / "other").mkdir()
            (tmp_path / "other").rename(real)
            with pytest.raises(OSError, match="replaced mid-provisioning"):
                pin.verify()
        finally:
            pin.close()

    def test_a_data_dir_under_a_linked_ancestor_is_pinned_not_refused(self, tmp_path: Any) -> None:
        """A home directory reached through a symlink is ordinary, not an attack.

        pin_parent walks every component with O_NOFOLLOW and refuses a link,
        which is why its contract makes the CALLER resolve the path once. An
        ancestor that has always been a link is indistinguishable from a
        swapped one to that walk, so an unresolved path turns every install
        and uninstall into a refusal for anyone whose home contains one.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        real_home = tmp_path / "real-home"
        data = real_home / "crew" / "apps" / "demo" / "data"
        data.mkdir(parents=True)
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home)
        app_root = linked_home / "crew" / "apps" / "demo"

        pin = bmod._PinnedDir(bmod._pinned_ancestors(app_root) / "data")
        try:
            assert pin.fd is not None, "POSIX must pin by descriptor"
            pinned = os.fstat(pin.fd)
            target = os.stat(str(data))
            assert (pinned.st_dev, pinned.st_ino) == (target.st_dev, target.st_ino)
            pin.verify()
        finally:
            pin.close()

    def test_a_data_dir_swapped_after_resolution_is_still_refused(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving on the caller side must not retire the guard.

        The swap is performed at the pin_parent seam - after the path was
        resolved, before the walk reads it - which is the check-to-use window
        made deterministic. O_NOFOLLOW on the final component has to refuse
        it, and the refusal has to say a swap happened, because here one did.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        holder = tmp_path / "holder"
        data = holder / "data"
        data.mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.mkdir()

        real_pin_parent = bmod.pinned_fs.pin_parent

        def swap_then_pin(parent: str, **kwargs: Any) -> int:
            data.rmdir()
            data.symlink_to(victim)
            return real_pin_parent(parent, **kwargs)

        monkeypatch.setattr(bmod.pinned_fs, "pin_parent", swap_then_pin)
        with pytest.raises(OSError, match="became a symbolic link after the path was checked"):
            bmod._PinnedDir(data)

    def test_a_link_at_the_app_directory_itself_is_still_refused(self, tmp_path: Any) -> None:
        """The canonical prefix must stop above the app's own directory.

        ``apps/<name>`` is written by the app, so canonicalising that component
        would hand the walk whatever a link planted there points at - another
        app's tree - and the O_NOFOLLOW refusal would never fire. Only the
        operator-controlled part above it may be made canonical.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        apps = tmp_path / "apps"
        victim = apps / "victim"
        (victim / "data").mkdir(parents=True)
        (apps / "attacker").symlink_to(victim)

        with pytest.raises(OSError):
            bmod._PinnedDir(bmod._pinned_ancestors(apps / "attacker") / "data")

    def test_an_ancestor_cycle_is_refused_as_an_oserror_not_a_runtimeerror(
        self, tmp_path: Any
    ) -> None:
        """Canonicalising the ancestors must not raise past the refusal type.

        Every caller of this class treats OSError as "refused" and lets nothing
        else through, so a helper that raises another type on a hostile tree
        turns a refusal into an unhandled crash. ``Path.resolve`` raises
        RuntimeError on a cycle; realpath hands the unresolved path to the walk,
        which refuses it as the configured OSError like any other link.
        """
        if not bmod.pinned_fs.supports_pinned_walk():
            pytest.skip("requires O_NOFOLLOW + dir_fd support; the pinned walk is the subject")
        first = tmp_path / "ring-a"
        second = tmp_path / "ring-b"
        first.symlink_to(second)
        second.symlink_to(first)

        with pytest.raises(OSError):
            bmod._PinnedDir(bmod._pinned_ancestors(first / "data"))

    def test_provisioning_refuses_a_linked_data_directory(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every provisioning operation under data/ would FOLLOW a planted
        link - an app pointing data/ at another app's tree would have the
        swap install attacker-chosen dependencies into the victim's dir.
        Refused before anything is touched through it, same shape as the
        uninstall purge's guard."""
        import os as _os
        import shutil as _shutil

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        victim = tmp_path / "victim-data"
        victim.mkdir()
        data = spawn_root / "data"
        if data.exists():
            _shutil.rmtree(data)
        try:
            _os.symlink(victim, data)
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        err = bmod.provision_app_deps("deps-datalink", spawn_root)
        assert "symlink/junction" in err, err
        assert not any("install" in argv for argv in runs), runs
        assert not any(victim.iterdir()), list(victim.iterdir())

    def test_an_oversized_requirements_file_is_refused_bounded(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gateway buffers app-controlled inputs in its own memory: an
        oversized requirements.txt reads at most cap+1 bytes and refuses -
        it must never be slurped whole or handed to pip."""
        (spawn_root / "requirements.txt").write_bytes(b"#" + b"x" * (bmod._DEPS_REQ_MAX_BYTES + 10))
        runs = _record_runs(monkeypatch)
        err = bmod.provision_app_deps("deps-huge", spawn_root)
        assert "Refusing requirements.txt" in err, err
        assert not any("install" in argv for argv in runs), runs

    def test_every_provisioning_failure_arm_emits_the_sel_event(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """deps_provision_failed is the audit contract for provisioning
        failures; the requirements-read refusal (out-of-root symlink) and
        lock failures go through the same wrapper emit as pip failures."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        outside = spawn_root.parent / "outside-req.txt"
        outside.write_bytes(b"requests\n")
        try:
            _os.symlink(outside, spawn_root / "requirements.txt")
        except OSError:
            pytest.skip("symlink not permitted")
        events: list = []
        monkeypatch.setattr(
            bmod,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        _record_runs(monkeypatch)
        err = bmod.provision_app_deps("deps-selpin", spawn_root)
        assert "Refusing requirements.txt" in err, err
        assert [e.get("outcome") for e in events] == ["deps_provision_failed"], events

    def test_a_planted_stamp_symlink_reads_as_unprovisioned(
        self, spawn_root: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stamp lives in the app-writable tree, so its read is
        no-follow-bound like the requirements read: a planted symlink at the
        stamp name must never make the gateway read an arbitrary path - it
        reads as "unprovisioned" and pip simply runs (safe direction)."""
        import os as _os

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        req = b"requests\n"
        (spawn_root / "requirements.txt").write_bytes(req)
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        target = tmp_path / "protected.txt"
        target.write_text(bmod._deps_digest(req))  # even a matching target
        try:
            _os.symlink(target, deps_dir / bmod._DEPS_STAMP_NAME)
        except OSError:
            pytest.skip("symlink not permitted")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-stamplink", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_include_bearing_requirements_never_skip_pip(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stamp digest covers the top-level file's bytes only, so a
        change confined to an included file (-r base.txt) would preserve the
        stamp and serve stale dependencies. Include-bearing requirements
        therefore disable the skip: pip runs even when the stamp matches."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        req = b"-r base.txt\n"
        (spawn_root / "requirements.txt").write_bytes(req)
        (spawn_root / "base.txt").write_bytes(b"requests\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(req))
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-inc", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_an_unchanged_requirements_file_skips_the_pip_call(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip --target cannot answer "already satisfied" the way a venv
        install could, so the stamp is what keeps a restart with unchanged
        requirements off the network - and keeps an OFFLINE restart of a
        healthy backend from raising a false provisioning alarm."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-skip", _manifest("server.py"))
        assert not any("install" in argv for argv in runs), runs

    def test_a_changed_requirements_file_reinstalls(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests==2.32.0\n")
        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(
            bmod._deps_digest(b"requests\n")  # stamp of the OLD file
        )
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-changed", _manifest("server.py"))
        assert any("install" in argv for argv in runs), runs

    def test_the_stamp_digest_changes_with_the_interpreter_abi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wheels installed by pip --target are ABI-specific, so a gateway
        Python upgrade must reprovision even when requirements.txt is
        byte-identical - a requirements-only stamp would skip pip and leave
        old-ABI wheels live. A PATCH upgrade must reprovision too:
        requirements can carry python_full_version markers that flip on it."""
        before = bmod._deps_digest(b"requests\n")
        monkeypatch.setattr(
            bmod.sys,
            "implementation",
            SimpleNamespace(
                cache_tag="cpython-399",
                name=sys.implementation.name,
                version=sys.implementation.version,
            ),
        )
        assert bmod._deps_digest(b"requests\n") != before
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- a pre-existing reset between two digest probes, moved verbatim; tracked for a de-flake pass
        monkeypatch.undo()
        monkeypatch.setattr(bmod.platform, "python_version", lambda: "3.99.99")
        assert bmod._deps_digest(b"requests\n") != before

    def test_an_interrupted_swap_is_recovered_on_the_next_start(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash between the two swap renames leaves the good tree under the
        prior name only. The next start must put it back - and, with a
        matching stamp inside, skip pip - so an offline restart keeps its
        dependencies instead of spawning bare."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        prior = spawn_root / "data" / bmod._DEPS_PRIOR_NAME
        prior.mkdir(parents=True)
        (prior / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests\n"))
        (prior / "marker.py").write_text("recovered = True\n")
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-recover", _manifest("server.py"))
        deps_dir = app_deps_dir(spawn_root)
        assert (deps_dir / "marker.py").is_file()
        assert not prior.exists()
        assert not any("install" in argv for argv in runs), runs
        assert any(str(a).endswith("deps_boot.py") for a in seen["argv"]), seen["argv"]

    @pytest.mark.skipif(sys.platform == "win32", reason="dir_fd staging pin is POSIX-only")
    def test_a_staging_swap_after_pip_cannot_redirect_marker_writes_or_publish(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """A build hook that replaces staging with a symlink after pip exits
        must neither have the gateway's marker writes land in its target nor
        get its tree PUBLISHED as the live install: the staging descriptor
        pinned at creation makes the writes un-redirectable and the
        pre-publish identity verify refuses the swapped entry."""
        import os as _os
        from pathlib import Path

        from kiro_crew.apps.interpreter import app_deps_dir

        victim = tmp_path / "victim-outside-app"
        victim.mkdir()
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests==2.32.0\n")

        def swapping_run(cmd: Any, **kwargs: Any) -> Any:
            target = None
            for i, tok in enumerate(cmd):
                if str(tok) == "--target" and i + 1 < len(cmd):
                    target = Path(str(cmd[i + 1]))
                    break
            assert target is not None, cmd
            aside = target.parent / (target.name + "-aside")
            _os.rename(str(target), str(aside))
            _os.symlink(str(victim), str(target))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(bmod, "run_limited", swapping_run)
        err = bmod.provision_app_deps("swap-app", spawn_root)
        assert err, "provisioning must FAIL when staging identity is lost"
        assert not (victim / bmod._DEPS_STAMP_NAME).exists(), "marker escaped!"
        assert not (victim / bmod._DEPS_ABI_NAME).exists(), "marker escaped!"
        deps_dir = app_deps_dir(spawn_root)
        assert not deps_dir.is_symlink(), "the swapped tree must not be published"

    def test_a_failed_reinstall_leaves_the_prior_deps_dir_intact(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip fills a staging dir that is swapped in only on success, so a
        failed or interrupted (re)install can never corrupt the live deps dir
        in place - the prior good install keeps serving the spawn."""
        from kiro_crew.apps.interpreter import app_deps_dir

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests==2.32.0\n")
        deps_dir = app_deps_dir(spawn_root)
        (deps_dir / "requests").mkdir(parents=True)
        (deps_dir / "requests" / "__init__.py").write_text("prior = True\n")
        # A REAL prior good install carries its stamp (written at swap time);
        # activation requires it to name the current interpreter's digest,
        # so the fixture plants it too. A stampless (or stale-ABI) tree is
        # deliberately NOT activated - see _deps_tree_stamp_current.
        (deps_dir / bmod._DEPS_STAMP_NAME).write_text(bmod._deps_digest(b"requests==2.32.0\n"))
        _record_runs(monkeypatch, exc=RuntimeError("no network"))
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-keep", _manifest("server.py"))
        assert (deps_dir / "requests" / "__init__.py").read_text() == "prior = True\n"
        assert not list((spawn_root / "data").glob(f"{bmod._DEPS_STAGING_NAME}*"))
        # And the surviving install still reaches the child.
        assert any(str(a).endswith("deps_boot.py") for a in seen["argv"]), seen["argv"]

    def test_the_installer_never_shells_out_to_a_bare_interpreter(
        self,
        spawn_root: Any,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        """pip must run as `sys.executable -m pip` - a bare `python3` relies
        on PATH (absent on some hosts, a Store stub on Windows), and any
        venv-relative pip path is POSIX-only."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-argv", _manifest("server.py"))
        pip_argv = next(argv for argv in runs if "install" in argv)
        # Assert on the argv TOKEN, never on a substring of the joined command.
        # sys.executable's own basename is frequently `python3` (any mise- or
        # pyenv-managed interpreter, and /usr/bin/python3 itself), so a
        # substring check matches the correct absolute form and fails on
        # exactly the hosts it is meant to pass on.
        assert pip_argv[0] == sys.executable, pip_argv
        assert pip_argv[0] != "python3", pip_argv
        assert pip_argv[1:4] == ["-s", "-m", "pip"], pip_argv
        # `.venv/bin/pip` is POSIX-only; the interpreter must run pip as a module.
        assert not pip_argv[0].replace("\\", "/").endswith("/bin/pip"), pip_argv

    def test_a_nonzero_pip_exit_is_checked_not_discarded(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pip call passes check=True: a non-zero exit must raise into the
        failure path rather than being silently discarded (the original defect
        left the backend to die on an import error pointing away from
        provisioning)."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        kwargs_seen: list[dict[str, Any]] = []

        def _run(argv: Any, **kwargs: Any) -> Any:
            kwargs_seen.append(kwargs)
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _run)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-check", _manifest("server.py"))
        assert kwargs_seen and kwargs_seen[0].get("check") is True, kwargs_seen

    def test_a_failed_dependency_install_does_not_block_the_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The spawn is still attempted (the deps dir may hold a previous
        successful install, and an offline host must not lose a working
        backend to a failed refresh) - but the failure now surfaces as an
        ERROR naming provisioning, not a swallowed warning."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        _record_runs(monkeypatch, exc=RuntimeError("no network"))
        _capture_popen(monkeypatch)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(_StopSpawn):
                bmod._start_app_backend_body("deps-fail", _manifest("server.py"))
        assert any(
            "Failed to install requirements.txt dependencies" in r.message
            and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_the_backend_log_is_preserved_across_a_restart(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A respawn must not erase the prior session's log.

        The log opened in mode ``"w"`` truncated on every (re)start, so the one
        artifact that records why the backend last crashed was gone the moment
        it respawned. Driving the spawn path twice -- each start writes a
        distinct provision line into the log before the limiter freezes it --
        the first start's line must still be present after the second, which it
        is only with an append open.
        """

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        log_path = spawn_root / "data" / "logs" / "backend.log"

        def _start_once(marker: str) -> None:
            _record_runs(
                monkeypatch,
                exc=subprocess.CalledProcessError(1, ["pip"], stderr=marker.encode()),
            )
            _capture_popen(monkeypatch)
            with pytest.raises(_StopSpawn):
                bmod._start_app_backend_body("restart-log", _manifest("server.py"))

        _start_once("FIRST-SESSION-CRASH-CAUSE")
        assert "FIRST-SESSION-CRASH-CAUSE" in log_path.read_text()
        _start_once("SECOND-SESSION-LINE")
        log_text = log_path.read_text()
        assert "SECOND-SESSION-LINE" in log_text
        assert (
            "FIRST-SESSION-CRASH-CAUSE" in log_text
        ), "the previous session's crash cause must survive the respawn"

    def test_the_backend_log_is_rotated_when_it_reaches_the_size_cap(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Append alone grows the file without bound; a full log is rolled
        aside to ``backend.log.1`` on open so the current file starts fresh and
        on-disk history stays bounded (one previous generation kept)."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        log_dir = spawn_root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "backend.log"
        # Seed a log at the cap so the next start must rotate it.
        log_path.write_bytes(b"O" * bmod._BACKEND_LOG_MAX_BYTES)

        _record_runs(
            monkeypatch,
            exc=subprocess.CalledProcessError(1, ["pip"], stderr=b"NEW-SESSION"),
        )
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("rotate-log", _manifest("server.py"))

        rolled = log_dir / "backend.log.1"
        assert rolled.exists(), "the full log must be rolled aside"
        assert rolled.stat().st_size == bmod._BACKEND_LOG_MAX_BYTES
        # The current file is a fresh append holding only the new session.
        current = log_path.read_text()
        assert "NEW-SESSION" in current
        assert "O" * 64 not in current

    def test_rotation_keeps_one_bounded_generation(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only one previous generation is kept: a start with the log at the
        cap replaces whatever was in ``backend.log.1`` with the current full
        file, so on-disk history stays bounded at about twice the cap and never
        accumulates generations across restarts."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        log_dir = spawn_root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "backend.log"
        log_path.write_bytes(b"O" * bmod._BACKEND_LOG_MAX_BYTES)
        (log_dir / "backend.log.1").write_text("STALE-OLD-GENERATION")

        _record_runs(
            monkeypatch,
            exc=subprocess.CalledProcessError(1, ["pip"], stderr=b"NEW"),
        )
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("rotate-drop", _manifest("server.py"))

        # backend.log (the full current log) replaces .1; the previous .1 is
        # gone and no second generation is created, so disk use is bounded.
        assert (log_dir / "backend.log.1").stat().st_size == bmod._BACKEND_LOG_MAX_BYTES
        assert not (log_dir / "backend.log.2").exists()
        for p in log_dir.iterdir():
            if p.is_file():
                assert "STALE-OLD-GENERATION" not in p.read_text(errors="replace")

    def test_a_small_backend_log_is_not_rotated(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common case: a restart with a log below the cap only appends --
        no rotation file is created and the prior content is kept inline."""

        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        log_dir = spawn_root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "backend.log"
        log_path.write_text("PRIOR-SMALL-SESSION\n")

        _record_runs(
            monkeypatch,
            exc=subprocess.CalledProcessError(1, ["pip"], stderr=b"APPENDED"),
        )
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("small-log", _manifest("server.py"))

        assert not (log_dir / "backend.log.1").exists()
        text = log_path.read_text()
        assert "PRIOR-SMALL-SESSION" in text and "APPENDED" in text

    def test_a_provisioning_failure_is_written_into_the_backend_log(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user-visible surface: the backend's own log opens with the
        provisioning failure, so the import error the missing deps produce
        points back at the real cause instead of reading as an app bug."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        err = subprocess.CalledProcessError(
            1, ["pip"], stderr=b"No matching distribution found for requests"
        )
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-log", _manifest("server.py"))
        log_text = (spawn_root / "data" / "logs" / "backend.log").read_text()
        assert "Failed to install requirements.txt dependencies" in log_text
        assert "No matching distribution found" in log_text

    def test_a_non_ascii_provisioning_failure_is_written_under_a_narrow_locale_codec(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A text handle opened without ``encoding=`` takes the platform default,
        which on Windows is cp1252. The provision-error line can carry a
        Unicode traceback glyph or an accented path; under cp1252 that write
        raises UnicodeEncodeError and the spawn aborts on the branch meant to
        record the failure. The open must name UTF-8 so the line always lands.

        The narrow default is simulated by giving every text-mode open that
        omits ``encoding=`` the cp1252 codec, which is what Windows does."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        glyphs = "\u2192 C:\\Users\\Ren\u00e9\\pkg \u2014 \u2019quoted\u2019"
        err = subprocess.CalledProcessError(
            1, ["pip"], stderr=f"ERROR: build failed {glyphs}".encode("utf-8")
        )
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)

        real_open = builtins.open

        def _windows_default_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if "b" not in mode and "encoding" not in kwargs and len(args) < 4:
                kwargs["encoding"] = "cp1252"
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _windows_default_open)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-utf8", _manifest("server.py"))
        raw = (spawn_root / "data" / "logs" / "backend.log").read_bytes()
        assert b"[kiro-crew] " in raw
        assert glyphs.encode("utf-8") in raw, "the non-ASCII text must round-trip as UTF-8"

    def test_the_crash_tail_decodes_the_childs_bytes_with_replacement(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The child appends raw bytes after the UTF-8 header. A stray byte in
        its output must not turn the whole tail into "(no output)"; the read
        decodes with replacement and the error still names what the child said."""
        (spawn_root / "server.py").write_text("x = 1\n")
        log_path = spawn_root / "data" / "logs" / "backend.log"

        def _popen(*_a: Any, **kwargs: Any) -> Any:
            kwargs["stdout"].write("[kiro-crew] header\n")
            kwargs["stdout"].flush()
            with open(log_path, "ab") as raw:
                raw.write(b"Error: address already in use \xff\xfe on bind\n")
            # A pid no platform allocates (the ``_UNALLOCATABLE_PID`` spelling in
            # test/test_update_provider.py): the crash branch may signal it.
            return SimpleNamespace(pid=99_999_999_999, returncode=1, poll=lambda: 1)

        monkeypatch.setattr(bmod, "popen_limited", _popen)
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: False)
        with caplog.at_level(logging.ERROR):
            result = bmod._start_app_backend_body("tail-bytes", _manifest("server.py"))
        assert result is None
        message = next(r.message for r in caplog.records if "exited immediately" in r.message)
        assert "(no output)" not in message
        assert "address already in use" in message
        assert "[PORT COLLISION]" in message

    def test_a_silent_exit_does_not_inherit_a_prior_sessions_port_collision(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The immediate-exit diagnostic must read only THIS session's output.

        backend.log is opened in append mode, so it can already hold a prior
        session's lines. A child that exits silently (a launcher failure, a
        kill, a crash before its first flush) writes nothing new, so reading the
        tail of the whole file would surface the previous session's last lines.
        If one of those was an ``errno 98`` / ``address already in use`` from an
        earlier spawn, every later silent exit would be mislabelled
        ``[PORT COLLISION]`` with a stale "cause" -- the exact misdiagnosis this
        change exists to prevent. Reading from the recorded start offset makes
        the tail "(no output)" and drops the stale collision flag.
        """
        (spawn_root / "server.py").write_text("x = 1\n")
        log_dir = spawn_root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "backend.log"
        # A prior session that really did hit a port collision.
        log_path.write_text(
            "[kiro-crew] previous session\n"
            "Error: [Errno 98] address already in use: bind failed\n"
        )

        def _popen(*_a: Any, **_kwargs: Any) -> Any:
            # The child writes nothing to the log before it exits.
            return SimpleNamespace(pid=99_999_999_999, returncode=1, poll=lambda: 1)

        monkeypatch.setattr(bmod, "popen_limited", _popen)
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _proc, _port=None: False)
        with caplog.at_level(logging.ERROR):
            result = bmod._start_app_backend_body("silent-exit", _manifest("server.py"))
        assert result is None
        message = next(r.message for r in caplog.records if "exited immediately" in r.message)
        assert (
            "[PORT COLLISION]" not in message
        ), "a silent exit must not inherit the previous session's errno-98 tail"
        assert "address already in use" not in message.lower()
        assert "(no output)" in message

    def test_tokenized_url_query_strings_are_stripped_from_pip_stderr(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """redact_credentials catches user:pass@ forms; a signed or
        tokenized URL carries its secret in the query string, which pip
        echoes verbatim. The strip runs on the FULL stderr before the tail
        cut, so a truncation can never split the URL from its query and let
        the token's tail through."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        token = "X-Amz-Signature=" + "s" * 32
        stderr = (
            "x" * 380 + f"ERROR: fetch https://bucket.example/pkg.whl?{token} failed"
        ).encode()
        err = subprocess.CalledProcessError(1, ["pip"], stderr=stderr)
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-qs", _manifest("server.py"))
        log_text = (spawn_root / "data" / "logs" / "backend.log").read_text()
        assert token not in log_text
        assert "s" * 32 not in log_text
        # WHICH layer catches it is not the contract (the exfiltration-URL
        # pass now runs first and can swallow the whole URL); the property
        # is that some redaction fired and the secret is gone.
        assert "<redacted-query>" in log_text or "[REDACTED" in log_text

    def test_pip_stderr_is_redacted_before_the_tail_truncation(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redaction runs on the FULL stderr, then the tail is taken: a
        suffix cut applied first can split a credential from the marker the
        redactor matches on, letting the secret's tail reach the logs."""
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        secret = "AKIA" + "X" * 16
        # Position the credential so a truncate-first implementation would
        # cut through it: padding pushes all but the secret's tail out of the
        # final 400 characters.
        stderr = (
            "x" * 380 + f"ERROR: fetch https://user:{secret}@pypi.example/simple failed"
        ).encode()
        err = subprocess.CalledProcessError(1, ["pip"], stderr=stderr)
        _record_runs(monkeypatch, exc=err)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-redact", _manifest("server.py"))
        log_text = (spawn_root / "data" / "logs" / "backend.log").read_text()
        assert secret not in log_text
        assert "Failed to install requirements.txt dependencies" in log_text

    def test_a_shimmed_child_gets_no_deps_pythonpath(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shim XOR PYTHONPATH: `python -m kiro_crew.apps.deps_boot` resolves
        kiro_crew through sys.path, so a deps-provided kiro_crew copy on
        PYTHONPATH would SHADOW the gateway's shim - app code running as the
        "shim". A shimmed child therefore launches WITHOUT the deps dir on
        PYTHONPATH; addsitedir supplies the deps only after the trusted shim
        has imported. The operator's own PYTHONPATH still passes through."""
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_dir = app_deps_dir(spawn_root)
        deps_dir.mkdir(parents=True)
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.setenv("PYTHONPATH", "/operator/own")
        _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-env", _manifest("server.py"))
        assert any(str(a).endswith("deps_boot.py") for a in seen["argv"]), seen["argv"]
        child_pp = seen["kwargs"]["env"].get("PYTHONPATH", "")
        assert str(deps_dir) not in child_pp.split(os.pathsep), child_pp
        assert "/operator/own" in child_pp.split(os.pathsep), child_pp

    def test_no_deps_dir_means_no_pythonpath_injection(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        monkeypatch.delenv("PYTHONPATH", raising=False)
        _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps-none", _manifest("server.py"))
        assert "PYTHONPATH" not in seen["kwargs"]["env"]
