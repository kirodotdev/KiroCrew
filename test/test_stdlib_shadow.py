"""The stdlib-shadow guard and the ``-P`` launch contract.

Background
----------
``python -m kiro_crew`` puts the working directory at ``sys.path[0]``, ahead
of the standard library. A ``~/concurrent/`` directory in a user's home (an
unpacked Python 2 ``futures`` backport) was therefore imported INSTEAD of the
stdlib package by ``kirocrew doctor`` and ``kirocrew gateway`` run from the
home directory, and the only symptom was ``TypeError:
ThreadPoolExecutor.__init__() got an unexpected keyword argument
'thread_name_prefix'`` from deep inside ``kiro_crew.executors`` -- three rounds
of misdiagnosis (PYTHONPATH, a Python 2 site-packages, ``.pth`` files) before
``concurrent.__file__`` was printed.

Two things are pinned here:

* The bundled launchers pass ``-P`` (``PYTHONSAFEPATH``), so the launch
  directory never reaches ``sys.path`` and the whole class of stdlib-named
  stray directories is inert.
* The process entries (``python -m kiro_crew``, the console script) refuse to
  start with a named cause when a probed stdlib module still resolves from a
  launch directory, ``PYTHONPATH`` or site-packages -- the paths ``-P`` does
  not cover, and every install that is not the bundle.

The stray package in these tests is a VERBATIM copy of the interpreter's own
``concurrent`` package: it works, so nothing downstream fails, and the only
way a test can go red is the guard noticing where the module came from. That
is the shape that matters -- the shadow that imports cleanly is the one that
breaks at an arbitrary later call.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import __version__, stdlib_shadow

_SRC = Path(__file__).resolve().parent.parent / "src"
_BUILD_DESKTOP = _SRC.parent / "packaging" / "build-desktop.sh"
_GATEWAY_SUPERVISOR = _SRC.parent / "website" / "electron" / "gateway-supervisor.js"
_BUILD_YML = _SRC.parent / ".github" / "workflows" / "build.yml"
_WIN_INSTALLER_TEST = _SRC.parent / ".github" / "scripts" / "test-windows-installer.ps1"
_WIN_SMOKE = _SRC.parent / "scripts" / "smoke-windows-install.ps1"
_CLI_SERVER = _SRC / "kiro_crew" / "cli_server.py"
SHIM_IN_BUILD_YML = "'\"%~dp0..\\python.exe\" -s -P -m kiro_crew %*'"


def _import_roots(path: str) -> set[str]:
    """Top-level names a module's static imports reach."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _stdlib_concurrent_dir() -> Path:
    import concurrent

    return Path(concurrent.__file__).resolve().parent


@pytest.fixture
def shadowing_home(tmp_path: Path) -> Path:
    """A directory holding a verbatim copy of the stdlib ``concurrent`` package.

    Run an interpreter with this as its cwd under ``-m`` or ``-c`` and, without
    ``-P``, ``import concurrent`` resolves here.
    """
    home = tmp_path / "home"
    home.mkdir()
    shutil.copytree(
        _stdlib_concurrent_dir(),
        home / "concurrent",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return home


def _child_env(tmp_path: Path) -> dict[str, str]:
    # PYTHONPATH names this checkout's src so the child runs the code under
    # test even when the installed package is another checkout; a PYTHONPATH
    # entry is also one of the roots the guard classifies, so it must NOT be
    # reported for holding kiro_crew.
    return {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "KIROCREW_HOME": str(tmp_path / "kc-home"),
        "PYTHONIOENCODING": "utf-8",
    }


def _run(argv: list[str], cwd: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=cwd,
        env=_child_env(tmp_path),
    )


_PROBE = (
    "from kiro_crew.stdlib_shadow import find_shadowed_stdlib; "
    "print(repr([(s.name, s.entry_kind) for s in find_shadowed_stdlib()]))"
)


class TestDetection:
    def test_stray_stdlib_copy_in_launch_dir_is_reported(
        self, shadowing_home: Path, tmp_path: Path
    ) -> None:
        res = _run(["-c", _PROBE], shadowing_home, tmp_path)
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == [("concurrent", "launch directory")]

    def test_safe_path_launch_sees_no_shadow(self, shadowing_home: Path, tmp_path: Path) -> None:
        """``-P`` is the fix the launchers ship: the same directory is inert under it."""
        res = _run(["-P", "-c", _PROBE], shadowing_home, tmp_path)
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == []

    def test_empty_pythonpath_component_is_the_cwd(
        self, shadowing_home: Path, tmp_path: Path
    ) -> None:
        """``PYTHONPATH=:`` inserts the absolute cwd even under ``-P``; the empty
        component must classify as PYTHONPATH or the shadow sits on sys.path
        unclassified and the check reports nothing."""
        env = _child_env(tmp_path)
        env["PYTHONPATH"] = os.pathsep + env["PYTHONPATH"]
        res = subprocess.run(
            [sys.executable, "-P", "-c", _PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=shadowing_home,
            env=env,
        )
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == [("concurrent", "PYTHONPATH")]

    def test_wholly_empty_pythonpath_adds_nothing(
        self, shadowing_home: Path, tmp_path: Path
    ) -> None:
        """``PYTHONPATH=`` (set, empty) inserts no entry, so under ``-P`` the same
        directory stays inert and must not be reported as a PYTHONPATH root.
        The checkout's own src is put on sys.path via ``-c`` instead."""
        env = _child_env(tmp_path)
        src = env.pop("PYTHONPATH")
        env["PYTHONPATH"] = ""
        code = f"import sys; sys.path.insert(0, {src!r}); " + _PROBE
        res = subprocess.run(
            [sys.executable, "-P", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=shadowing_home,
            env=env,
        )
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == []

    def test_clean_launch_dir_reports_nothing(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean"
        clean.mkdir()
        res = _run(["-c", _PROBE], clean, tmp_path)
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == []

    def test_stdlib_directory_as_launch_dir_is_not_a_shadow(self, tmp_path: Path) -> None:
        """``cd lib/python3.X && python -c`` finds the REAL modules under the launch
        entry; that is the fail-closed edge the classifier must not misread."""
        res = _run(["-c", _PROBE], _stdlib_concurrent_dir().parent, tmp_path)
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == []

    def test_stdlib_package_directory_as_launch_dir_is_not_a_shadow(self, tmp_path: Path) -> None:
        """``cd <stdlib>/concurrent && python -m kiro_crew``: the real
        ``concurrent/__init__.py`` lies INSIDE the launch entry but was not
        provided BY it -- the finder only loads ``<entry>/<name>/__init__.py``.
        A containment test attributed it to the launch directory and refused a
        healthy start; the provider match must be exact."""
        res = _run(["-c", _PROBE], _stdlib_concurrent_dir(), tmp_path)
        assert res.returncode == 0, res.stderr
        assert ast.literal_eval(res.stdout.strip()) == []
        started = _run(["-m", "kiro_crew", "--version"], _stdlib_concurrent_dir(), tmp_path)
        assert started.returncode == 0, started.stderr
        assert started.stdout == f"kirocrew {__version__}\n"

    def test_report_names_module_entry_and_fix(self) -> None:
        shadow = stdlib_shadow.ShadowedModule(
            name="concurrent",
            resolved="/home/u/concurrent/__init__.py",
            path_entry="/home/u",
            entry_kind="launch directory",
        )
        text = stdlib_shadow.format_shadow_report([shadow])
        assert "concurrent -> '/home/u/concurrent/__init__.py'" in text
        assert "'/home/u' (launch directory)" in text
        # The remedy names the detected package directory, not a hardcoded
        # example: a user shadowed by ~/json must not be told to move ~/concurrent.
        assert "mv '/home/u/concurrent' '/home/u/concurrent.bak'" in text
        module_shadow = stdlib_shadow.ShadowedModule(
            name="types",
            resolved="/proj/types.py",
            path_entry="/proj",
            entry_kind="launch directory",
        )
        assert "mv '/proj/types.py' '/proj/types.py.bak'" in stdlib_shadow.format_shadow_report(
            [module_shadow]
        )

    def test_remedy_is_shell_safe_or_absent(self, tmp_path: Path) -> None:
        """The path is attacker-named and the line is meant to be pasted. An
        apostrophe plus `$(...)` must stay inert inside the command (repr-style
        quoting would switch to double quotes and let the substitution run);
        control characters or non-ASCII yield NO command at all. The quoting is
        proven by handing the words to a real POSIX shell with substitution
        enabled, not by re-implementing the shell's grammar in the test."""
        marker = tmp_path / "pwned"
        hostile_dir = f"/tmp/it's $(touch {marker})"
        hostile = stdlib_shadow.ShadowedModule(
            "json", f"{hostile_dir}/json/__init__.py", hostile_dir, "PYTHONPATH"
        )
        cmd = stdlib_shadow.remedy_command(hostile)
        assert cmd is not None and cmd.startswith("mv ")
        sh = shutil.which("bash") or shutil.which("sh")
        if sh is None:
            pytest.skip("no POSIX shell on this host")
        res = subprocess.run(
            [sh, "-c", "set -- " + cmd[len("mv ") :] + '; printf "%s\\n" "$@"'],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=tmp_path,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.splitlines() == [f"{hostile_dir}/json", f"{hostile_dir}/json.bak"]
        assert not marker.exists(), "the substitution ran: the remedy is pasteable-unsafe"
        for bad in ("/tmp/\x1b[31m/json/__init__.py", "/home/\u7528\u6237/json/__init__.py"):
            shadow = stdlib_shadow.ShadowedModule("json", bad, "/x", "PYTHONPATH")
            assert stdlib_shadow.remedy_command(shadow) is None
            text = stdlib_shadow.format_shadow_report([shadow])
            assert "mv " not in text
            text.encode("ascii")
        # Printed before ensure_utf8_console() on the console-script path.
        text.encode("ascii")

    def test_report_escapes_hostile_and_non_ascii_paths(self) -> None:
        """A path is caller-chosen bytes and this prints before the console is
        UTF-8 on Windows: control characters and non-ASCII must both be escaped,
        or the refusal itself becomes a terminal write or a UnicodeEncodeError."""
        shadow = stdlib_shadow.ShadowedModule(
            name="json",
            resolved="/home/\u7528\u6237/json/__init__.py",
            path_entry="/home/\x1b[31mevil",
            entry_kind="PYTHONPATH",
        )
        text = stdlib_shadow.format_shadow_report([shadow])
        assert "\x1b" not in text
        assert "\\x1b" in text
        assert "\\u7528" in text
        text.encode("ascii")

    def test_module_imports_stdlib_only(self) -> None:
        """Same invariant ``_bootstrap`` pins on ``dep_sync``: this module runs from
        the entry points before the package's dependencies are known to exist."""
        roots = _import_roots(stdlib_shadow.__file__)
        third_party = roots - set(sys.stdlib_module_names)
        assert (
            not third_party
        ), f"stdlib_shadow must import stdlib only; found {sorted(third_party)}"

    def test_module_imports_nothing_the_entry_points_have_not_already_loaded(self) -> None:
        """The probe inspects the launch directory; a fresh stdlib import here would
        execute that name FROM that directory before the refusal. So the module may
        import only what a bare interpreter already holds in ``sys.modules`` plus
        ``importlib``, which both entry points import before it (``_bootstrap``
        directly, ``__main__`` through ``platform_compat``). ``__future__`` counts:
        ``from __future__ import ...`` imports that module at run time."""
        roots = _import_roots(stdlib_shadow.__file__)
        res = subprocess.run(
            [sys.executable, "-c", "import sys; print(repr(sorted(sys.modules)))"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert res.returncode == 0, res.stderr
        preloaded = set(ast.literal_eval(res.stdout.strip())) | {"importlib"}
        fresh = roots - preloaded
        assert (
            not fresh
        ), f"stdlib_shadow imports names the entry points have not loaded: {sorted(fresh)}"


class TestEntryRefusal:
    def test_module_entry_refuses_from_shadowing_directory(
        self, shadowing_home: Path, tmp_path: Path
    ) -> None:
        """``python -m kiro_crew`` run from the stray directory names the cause and
        exits 2 instead of printing a version string it would later contradict."""
        res = _run(["-m", "kiro_crew", "--version"], shadowing_home, tmp_path)
        assert res.returncode == stdlib_shadow.SHADOW_EXIT_STATUS, res.stdout + res.stderr
        assert "Refusing to start" in res.stderr
        assert "concurrent -> '" in res.stderr
        assert "launch directory" in res.stderr
        assert __version__ not in res.stdout

    def test_module_entry_under_safe_path_starts(
        self, shadowing_home: Path, tmp_path: Path
    ) -> None:
        """What the launchers' ``-P`` buys: the same directory, a normal start."""
        res = _run(["-P", "-m", "kiro_crew", "--version"], shadowing_home, tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout == f"kirocrew {__version__}\n"

    def test_bootstrap_refuses_before_importing_cli(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from kiro_crew import _bootstrap

        shadow = stdlib_shadow.ShadowedModule("json", "/x/json/__init__.py", "/x", "PYTHONPATH")
        monkeypatch.setattr(stdlib_shadow, "find_shadowed_stdlib", lambda: [shadow])

        def _must_not_import() -> None:
            raise AssertionError("cli imported despite a shadowed stdlib")

        monkeypatch.setattr(_bootstrap, "_import_cli", _must_not_import)
        monkeypatch.setattr(sys, "argv", ["kirocrew", "doctor"])
        with pytest.raises(SystemExit) as exc:
            _bootstrap.main()
        assert exc.value.code == stdlib_shadow.SHADOW_EXIT_STATUS
        err = capsys.readouterr().err
        assert "json -> '/x/json/__init__.py'" in err
        assert "(PYTHONPATH)" in err


class TestDoctorRow:
    def test_shadow_is_an_issue(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        from kiro_crew import cli_doctor

        shadow = stdlib_shadow.ShadowedModule(
            "concurrent", "/home/u/concurrent/__init__.py", "/home/u", "launch directory"
        )
        monkeypatch.setattr(stdlib_shadow, "find_shadowed_stdlib", lambda: [shadow])
        issues: list[str] = []
        cli_doctor._doctor_import_path(issues)
        out = capsys.readouterr().out
        assert "❌ concurrent shadowed by '/home/u/concurrent/__init__.py'" in out
        assert "'/home/u' (launch directory)" in out
        assert issues == ["stdlib shadowed"]

    def test_clean_is_not_an_issue(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        from kiro_crew import cli_doctor

        monkeypatch.setattr(stdlib_shadow, "find_shadowed_stdlib", lambda: [])
        issues: list[str] = []
        cli_doctor._doctor_import_path(issues)
        out = capsys.readouterr().out
        assert "import path: ✅ stdlib intact" in out
        assert issues == []


class TestLaunchersPassSafePath:
    """A revert of one launcher's ``-P`` is a silent regression; pin each spelling."""

    def test_posix_launcher(self) -> None:
        text = _BUILD_DESKTOP.read_text(encoding="utf-8")
        m = re.search(r"cat > \"\$out/bin/kirocrew\" <<'LAUNCH'\n(.*?)\nLAUNCH\n", text, re.DOTALL)
        assert m, "POSIX launcher heredoc not found"
        assert re.search(
            r'^exec "\$DIR/python3\.12" -s -P -m kiro_crew "\$@"$', m.group(1), re.M
        ), m.group(1)

    def test_windows_cmd_shim(self) -> None:
        text = _BUILD_DESKTOP.read_text(encoding="utf-8")
        assert 'python.exe" -s -P -m kiro_crew %%*' in text

    def test_electron_windows_direct_spawn(self) -> None:
        text = _GATEWAY_SUPERVISOR.read_text(encoding="utf-8")
        assert 'spawnArgs = ["-s", "-P", "-m", "kiro_crew", ...spawnArgs];' in text

    def test_ci_windows_replicas_match_the_shipped_shape(self) -> None:
        """`build.yml` writes its own copy of the .cmd shim (declared byte-identical
        in shape to `build_backend_windows`'s) and the installer test spawns the
        gateway the way the Electron supervisor does; a flag present in production
        and absent in the replica means CI boots a launch shape production no
        longer writes."""
        build_yml = _BUILD_YML.read_text(encoding="utf-8")
        assert SHIM_IN_BUILD_YML in build_yml
        installer = _WIN_INSTALLER_TEST.read_text(encoding="utf-8")
        assert '"-s", "-P", "-m", "kiro_crew", "gateway"' in installer
        smoke = _WIN_SMOKE.read_text(encoding="utf-8")
        assert '"-s", "-P", "-m", "kiro_crew", "gateway"' in smoke

    def test_every_self_launch_enters_the_module_with_safe_path(self) -> None:
        """Every place Kiro Crew spawns or execs ITSELF through `-m kiro_crew...`
        passes `-P`: the launchers, the restart re-exec, the pod unit, the
        dev-fleet CLI, the jail re-exec, the setup respawn, the test harness, the
        container supervisor, the MCP gatewayd and the computer-use overlay. A
        new spelling without it is the same class as the field failure, so the
        ratchet is over the whole source tree, not a list -- reading argv across
        line breaks, and matching the dotted submodule spellings and the module
        constants (`_GATEWAYD_MODULE`, `OVERLAY_MODULE`, `_STUB_MODULE`) as well
        as the literal package."""
        pattern = re.compile(r'"-m",\s*(?:"kiro_crew|_GATEWAYD_MODULE|OVERLAY_MODULE|_STUB_MODULE)')
        # The MCP stub argv is a PERSISTED, fingerprinted overlay format read by
        # kiro-cli; adding a flag there re-shapes every written overlay and its
        # cache key, so it is tracked as its own change rather than folded in here.
        deferred = {"kiro_crew/mcp_gateway/rewriter.py"}
        offenders: list[str] = []
        for path in (_SRC / "kiro_crew").rglob("*.py"):
            posix = path.as_posix()
            if "/tests/" in posix or "/container_tests/" in posix or path.name.startswith("test_"):
                continue
            rel = path.relative_to(_SRC).as_posix()
            if rel in deferred:
                continue
            text = path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                # Look back to the start of the same argv call (piper_runtime
                # comments each flag on its own line), else a short window for
                # a bare tuple such as BACKEND_LAUNCHER.
                call = text.rfind("isolated_python_argv(", 0, m.start())
                window = text[call if call != -1 else max(0, m.start() - 120) : m.start()]
                if '"-P",' in window:
                    continue
                if "expected_args" in text[max(0, m.start() - 200) : m.start()]:
                    continue  # mcp_discovery's matcher READS an argv, it spawns nothing
                lineno = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{rel}:{lineno}")
        assert not offenders, "\n".join(offenders)

    def test_home_cwd_respawn_keeps_home_off_sys_path(self) -> None:
        """`_spawn_detached_gateway` is the one in-process `-m kiro_crew` fallback
        that hardcodes `cwd=Path.home()` -- the exact launch shape of the field
        failure -- so it passes `-P` like the launchers do."""
        text = _CLI_SERVER.read_text(encoding="utf-8")
        assert 'isolated_python_argv("-P", "-m", "kiro_crew", "gateway")' in text

    def test_self_containment_probes_use_the_shipped_argv(self) -> None:
        """The build's self-containment check runs the exact launcher argv, so a
        flag the launcher passes that the probe omits is a contract nobody
        verified at build time."""
        text = _BUILD_DESKTOP.read_text(encoding="utf-8")
        assert '"$out/bin/python3.12" -s -P -m kiro_crew --version' in text
        assert '"$out/python.exe" -s -P -m kiro_crew --version' in text
