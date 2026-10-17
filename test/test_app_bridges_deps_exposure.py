"""App bridge tests: how an app's provisioned deps reach its stdio MCP servers.

PYTHONPATH versus the deps_boot shim, the deps stamp gate, and shebang, trampoline and
launcher detection.
"""

from __future__ import annotations

import os
import sys

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401

from kiro_crew import platform_compat


def _isolated_python_args(cfg: dict) -> list[str]:
    args = cfg["args"]
    assert args and args[0] == "-s", cfg
    return args[1:]


class TestAbiMatchedShebangPathScript:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shebang launch semantics are POSIX-only",
    )
    def test_a_path_script_with_gateway_shebang_shims_through_deps_boot(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """An absolute-path launcher whose shebang names an ABI-matched
        interpreter is a python launch: it must reach its provisioned deps
        (via the shim, so .pth files work) instead of dying on import."""
        import sys as _sys

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        launcher = tmp_path / "bin" / "server"
        launcher.parent.mkdir()
        launcher.write_text(f"#!{_sys.executable}\nimport requests\n")
        launcher.chmod(0o755)
        cfg = resolve_stdio_command(
            {"command": str(launcher), "args": ["--flag"]}, app_root=tmp_path
        )
        assert cfg["command"] == _sys.executable, cfg
        assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
        assert _isolated_python_args(cfg)[1] == str(app_deps_dir(tmp_path)), cfg
        assert _isolated_python_args(cfg)[2] == str(launcher), cfg
        assert _isolated_python_args(cfg)[3] == "--flag", cfg

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shebang launch semantics are POSIX-only",
    )
    def test_a_path_script_with_foreign_shebang_stays_direct(self, tmp_path):
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        launcher = tmp_path / "bin" / "server"
        launcher.parent.mkdir()
        launcher.write_text("#!/opt/foreign/python3.11\nimport requests\n")
        launcher.chmod(0o755)
        cfg = resolve_stdio_command({"command": str(launcher)}, app_root=tmp_path)
        assert cfg["command"] == str(launcher), cfg
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg


class TestStaleTreeScriptSelection:
    def test_a_stale_abi_tree_console_script_is_not_selected(self, tmp_path):
        """Command SELECTION is stamp-gated like every deps-exposure arm: a
        stale tree cannot receive the deps PYTHONPATH/shim, so pointing the
        command at its console script guarantees an import crash. The bare
        name falls through instead and the provisioning error surfaces."""
        import sys as _sys

        from kiro_crew.apps.interpreter import app_deps_dir, venv_provided_command

        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        deps_bin = app_deps_dir(tmp_path) / ("Scripts" if _sys.platform == "win32" else "bin")
        deps_bin.mkdir(parents=True)
        script = deps_bin / ("tool.exe" if _sys.platform == "win32" else "tool")
        script.write_bytes(b"#!/usr/bin/python3\nimport dep\n")
        script.chmod(0o755)
        # NO stamp / foreign stamp: the tree is not current.
        assert venv_provided_command(tmp_path, script.name) is None
        # With a current stamp the same script IS selected.
        _plant_deps_stamp(tmp_path, b"requests\n")
        assert venv_provided_command(tmp_path, script.name) == str(script)


class TestShebangReaderGates:
    def test_a_sensitive_command_path_is_never_opened(self, tmp_path, monkeypatch):
        """The shebang sniff runs on a manifest-author-controlled absolute
        path: a protected location must be refused by the sensitive-path
        gate BEFORE any read, not read-and-discarded."""
        import kiro_crew.apps.bridges as brmod

        opened: list[str] = []
        real_open = open

        def tracking_open(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        monkeypatch.setattr(brmod, "is_sensitive_path", lambda p: True)
        monkeypatch.setattr("builtins.open", tracking_open)
        assert brmod._python_shebang_interpreter("/etc/whatever") is None
        assert brmod._has_python_shebang("/etc/whatever") is False
        assert brmod._zip_has_main("/etc/whatever") is False
        assert opened == [], "gated readers must refuse before opening"

    @pytest.mark.skipif(sys.platform == "win32", reason="shebang semantics are POSIX-only")
    def test_an_argument_bearing_shebang_is_never_a_rewrite_candidate(self, tmp_path):
        """A ``#!<python> -I`` script asks for an isolation posture the
        rewrite cannot carry faithfully (the kernel passes shebang
        arguments as one token): the command stays on the direct launch,
        where the kernel honors every flag exactly as written."""
        import sys as _sys

        from kiro_crew.apps.bridges import (
            _python_shebang_interpreter,
            resolve_stdio_command,
        )
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        launcher = tmp_path / "bin" / "server"
        launcher.parent.mkdir()
        launcher.write_text(f"#!{_sys.executable} -I\nimport sys\n")
        launcher.chmod(0o755)
        assert _python_shebang_interpreter(str(launcher)) is None
        cfg = resolve_stdio_command({"command": str(launcher)}, app_root=tmp_path)
        assert cfg["command"] == str(launcher), cfg
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg


class TestSkipFirstLineFlagUnshimmable:
    def test_dash_x_falls_back_to_the_pythonpath_transport(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """-x skips the launched script's FIRST LINE; shimming would make
        deps_boot that script and the interpreter dies with SyntaxError on
        its severed docstring. Unshimmable: direct launch + PYTHONPATH."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        (tmp_path / "server.py").write_text("#!custom\nimport sys\n")
        cfg = resolve_stdio_command(
            {"command": "python3", "args": ["-x", "server.py"]}, app_root=tmp_path
        )
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg
        assert cfg["args"] == ["-s", "-x", "server.py"]
        # the transport still serves the deps
        assert str(app_deps_dir(tmp_path)) in (cfg.get("env") or {}).get("PYTHONPATH", ""), cfg


def _plant_deps_stamp(app_root, req_bytes: bytes) -> None:
    """Write the CURRENT interpreter's stamp for req_bytes: activation now
    requires it (a stampless or stale-ABI tree must not inject), so any
    fixture that expects the deps dir to reach the server plants it."""
    import kiro_crew.apps.backend as _bk
    from kiro_crew.apps.interpreter import app_deps_dir as _add

    d = _add(app_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / _bk._DEPS_STAMP_NAME).write_text(_bk._deps_digest(req_bytes))


class TestStdioDepsDirExposure:
    """The provisioned deps dir (pip --target) must reach stdio MCP servers the
    same way it reaches the backend spawn: via PYTHONPATH. A --target install
    carries no interpreter, so the env is the only bridge - without it a
    python-launcher server or a deps-provided console script dies on import."""

    def test_the_deps_dir_is_prepended_to_a_stdio_server_pythonpath(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        import sys as _sys

        # an ABI-matched PATH python (here: the gateway interpreter by path)
        # is a python launch: it routes through the shim so .pth hooks are
        # processed, the command itself is never rewritten, and the deps
        # entry is stripped from PYTHONPATH (shim XOR PYTHONPATH) while the
        # manifest's own entry passes through
        cfg = resolve_stdio_command(
            {
                "command": _sys.executable,
                "args": ["server.py"],
                "env": {"PYTHONPATH": "/manifest/own"},
            },
            app_root=tmp_path,
        )
        assert cfg["command"] == _sys.executable, cfg
        # PATH spelling, not -m: an ABI-matched path python can be the
        # app's own venv interpreter, which cannot import kiro_crew
        assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
        assert _isolated_python_args(cfg)[1] == str(app_deps_dir(tmp_path)), cfg
        assert _isolated_python_args(cfg)[2] == "server.py", cfg
        parts = cfg["env"]["PYTHONPATH"].split(os.pathsep)
        assert str(app_deps_dir(tmp_path)) not in parts, cfg
        assert "/manifest/own" in parts, cfg

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shebang launch semantics are POSIX-only; Windows uses the launcher pair",
    )
    def test_a_deps_console_script_routes_through_the_shim(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """A deps-dir console script is a pip-generated Python script; run
        direct, its editable/.pth-dependent imports die (PYTHONPATH never
        processes .pth). It launches through deps_boot on the gateway
        interpreter - the same one its shebang names - and the deps
        PYTHONPATH entry is stripped (shim XOR PYTHONPATH)."""
        import sys as _sys

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_bin = app_deps_dir(tmp_path) / "bin"
        deps_bin.mkdir(parents=True)
        script = deps_bin / "mytool"
        script.write_bytes(b"#!" + _sys.executable.encode() + b"\nimport mypkg\n")
        script.chmod(0o755)
        (tmp_path / "requirements.txt").write_bytes(b"-e ./lib\n")
        _plant_deps_stamp(tmp_path, b"-e ./lib\n")
        cfg = resolve_stdio_command({"command": "mytool", "args": ["--serve"]}, app_root=tmp_path)
        assert cfg["command"] == _sys.executable, cfg
        assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
        assert _isolated_python_args(cfg)[1:] == [
            str(app_deps_dir(tmp_path)),
            str(script),
            "--serve",
        ], cfg
        pp = (cfg.get("env") or {}).get("PYTHONPATH", "")
        assert str(app_deps_dir(tmp_path)) not in pp.split(os.pathsep), cfg

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shebang launch semantics are POSIX-only; Windows uses the launcher pair",
    )
    def test_a_pip_shell_trampoline_script_is_recognized_as_python(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """pip emits a /bin/sh trampoline when the interpreter path is long
        or space-bearing; the script is still python and must route through
        the shim, or its .pth-dependent imports silently die."""
        import sys as _sys

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_bin = app_deps_dir(tmp_path) / "bin"
        deps_bin.mkdir(parents=True)
        script = deps_bin / "tramptool"
        q3 = b"\x27\x27\x27"  # three single quotes
        script.write_bytes(
            b"#!/bin/sh\n"
            + q3
            + b"exec\x27 "
            + _sys.executable.encode()
            + b' "$0" "$@"\n'
            + b"\x27 "
            + q3
            + b"\nimport mypkg\n"
        )
        script.chmod(0o755)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        cfg = resolve_stdio_command({"command": "tramptool"}, app_root=tmp_path)
        assert cfg["command"] == _sys.executable, cfg
        assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
        assert _isolated_python_args(cfg)[1] == str(app_deps_dir(tmp_path)), cfg

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shebang launch semantics are POSIX-only; Windows uses the launcher pair",
    )
    def test_a_shell_wrapper_mentioning_python_is_not_a_trampoline(self, tmp_path):
        """Only pip's exact polyglot structure (second line: triple-quoted
        exec re-launch) reads as a trampoline. An ordinary dependency shell
        wrapper that merely RUNS a python command must stay a shell script
        - runpy would parse it as python source and crash the server."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_bin = app_deps_dir(tmp_path) / "bin"
        deps_bin.mkdir(parents=True)
        script = deps_bin / "wraptool"
        script.write_bytes(b"#!/bin/sh\n" b"set -e\n" b'exec python3 -m something --flag "$@"\n')
        script.chmod(0o755)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        cfg = resolve_stdio_command({"command": "wraptool"}, app_root=tmp_path)
        assert cfg["command"] == str(script), cfg
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg

    def test_a_windows_launcher_pair_shims_via_the_companion_script(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """pip's classic Windows launcher is a native .exe (no shebang) with
        a `<name>-script.py` companion holding the python entry - the
        companion shims through deps_boot. An embedded-script .exe with no
        companion stays a direct launch."""
        import sys as _sys

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_bin = app_deps_dir(tmp_path) / ("Scripts" if _sys.platform == "win32" else "bin")
        deps_bin.mkdir(parents=True)
        exe = deps_bin / "wintool.exe"
        exe.write_bytes(b"MZnative")
        exe.chmod(0o755)
        companion = deps_bin / "wintool-script.py"
        companion.write_bytes(b"import mypkg\n")
        (tmp_path / "requirements.txt").write_bytes(b"-e ./lib\n")
        _plant_deps_stamp(tmp_path, b"-e ./lib\n")
        cfg = resolve_stdio_command({"command": "wintool.exe"}, app_root=tmp_path)
        if cfg["command"] == _sys.executable:
            # resolver found the exe (Windows probe layout): companion shims
            assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
            assert _isolated_python_args(cfg)[1] == str(app_deps_dir(tmp_path)), cfg
            assert _isolated_python_args(cfg)[2] == str(companion), cfg
        else:
            # POSIX probe does not resolve .exe names from deps bin - the
            # command passes through untouched (nothing to shim here)
            assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg

    def test_a_zip_bearing_exe_without_main_stub_stays_direct(self, tmp_path):
        """zipfile.is_zipfile answers True for any exe with an appended
        archive (self-extractors, installers). Only a __main__.py stub makes
        it a launcher deps_boot can dispatch - anything else must launch
        directly instead of crashing in the archive read."""
        import io
        import sys as _sys
        import zipfile as _zipfile

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_bin = app_deps_dir(tmp_path) / ("Scripts" if _sys.platform == "win32" else "bin")
        deps_bin.mkdir(parents=True)
        buf = io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("payload.dat", "not a launcher")
        exe = deps_bin / ("selfextract.exe" if _sys.platform == "win32" else "selfextract")
        exe.write_bytes(b"MZnative" + buf.getvalue())
        exe.chmod(0o755)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        cfg = resolve_stdio_command({"command": exe.name}, app_root=tmp_path)
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shebang launch semantics are POSIX-only; Windows uses the launcher pair",
    )
    def test_a_non_python_deps_script_stays_direct(self, tmp_path):
        """A package can ship arbitrary bin artifacts; runpy on a shell
        script would break a working launch. Only a python-shebang
        script is shimmed - everything else executes directly and keeps the
        PYTHONPATH transport."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        deps_bin = app_deps_dir(tmp_path) / "bin"
        deps_bin.mkdir(parents=True)
        script = deps_bin / "shtool"
        script.write_bytes(b"#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        cfg = resolve_stdio_command({"command": "shtool"}, app_root=tmp_path)
        assert cfg["command"] == str(script), cfg
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg
        pp = cfg["env"]["PYTHONPATH"].split(os.pathsep)
        assert pp[0] == str(app_deps_dir(tmp_path)), cfg

    def test_a_backendless_app_provisions_deps_at_registration(self, tmp_path, monkeypatch):
        """An app can ship only stdio MCP servers: with no backend start,
        nothing else ever runs pip, and the shim/PYTHONPATH transports would
        reference a forever-empty deps tree. Registration provisions - and
        only for backend-less apps: with an entry point the backend spawn
        owns provisioning (module-style builtins must never reach it)."""
        from types import SimpleNamespace as NS

        import kiro_crew.apps.backend as bkmod
        import kiro_crew.apps.bridges as brmod

        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        monkeypatch.setattr(brmod, "app_dir", lambda name: tmp_path)
        calls: list = []
        monkeypatch.setattr(
            bkmod, "provision_app_deps", lambda name, root: calls.append((name, root)) or ""
        )
        stdio_manifest = NS(
            mcpServers={"srv": {"command": "python3", "args": ["s.py"]}},
            backend=NS(entryPoint=""),
        )
        brmod._maybe_provision_backendless_deps("app", stdio_manifest)
        assert calls == [("app", tmp_path)]

        # A DANGLING requirements.txt must reach the provisioner (whose
        # failure epilogue surfaces it) - is_file() would skip it silently.
        import sys as _sys

        if _sys.platform != "win32":
            calls.clear()
            (tmp_path / "requirements.txt").unlink()
            os.symlink(str(tmp_path / "missing.txt"), str(tmp_path / "requirements.txt"))
            brmod._maybe_provision_backendless_deps("app", stdio_manifest)
            assert calls == [("app", tmp_path)], "dangling link must reach the provisioner"
            (tmp_path / "requirements.txt").unlink()
            # True ABSENCE skips: no lock files for apps without deps.
            calls.clear()
            brmod._maybe_provision_backendless_deps("app", stdio_manifest)
            assert calls == []
            (tmp_path / "requirements.txt").write_bytes(b"requests\n")

        calls.clear()
        # a FILE entry point provisions too: a healthy fixed-port backend is
        # ADOPTED, never spawned, so spawn-path provisioning never ran
        brmod._maybe_provision_backendless_deps(
            "app", NS(mcpServers={"srv": {"command": "x"}}, backend=NS(entryPoint="server.py"))
        )
        assert calls == [("app", tmp_path)]

        calls.clear()
        # a MODULE-style entry point is trusted package code: never
        # provision app-dir requirements for it (backend trust gate)
        brmod._maybe_provision_backendless_deps(
            "app",
            NS(
                mcpServers={"srv": {"command": "x"}},
                backend=NS(entryPoint="kiro_crew.apps.builtins.demo"),
            ),
        )
        # url-only servers never import from the deps dir
        brmod._maybe_provision_backendless_deps(
            "app",
            NS(mcpServers={"srv": {"url": "http://127.0.0.1:9/mcp"}}, backend=NS(entryPoint="")),
        )
        # a shipped BUILTIN is trusted package code: an agent-planted
        # requirements.txt in its writable app dir must never have pip
        # execute build hooks under the gateway (provenance gate)
        monkeypatch.setattr(brmod, "shipped_builtin_app_root", lambda name: tmp_path)
        brmod._maybe_provision_backendless_deps("app", stdio_manifest)
        assert calls == []

    def test_no_deps_dir_leaves_the_manifest_env_untouched(self, tmp_path):
        from kiro_crew.apps.bridges import resolve_stdio_command

        cfg = resolve_stdio_command(
            {"command": "python3", "args": ["server.py"]}, app_root=tmp_path
        )
        assert "env" not in cfg, cfg

    def test_a_python_launcher_with_deps_routes_through_the_shim(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """PYTHONPATH never processes .pth files, so a python-launcher stdio
        server with provisioned deps launches via deps_boot (addsitedir).
        An interpreter OPTION first-token is left unshimmed (it would be
        misread as the target) and falls back to the PYTHONPATH transport."""
        import sys as _sys

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        cfg = resolve_stdio_command(
            {"command": "python3", "args": ["server.py"]}, app_root=tmp_path
        )
        assert cfg["command"] == _sys.executable
        assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
        assert _isolated_python_args(cfg)[1] == str(app_deps_dir(tmp_path)), cfg
        assert _isolated_python_args(cfg)[2] == "server.py", cfg
        # interpreter options are WALKED: -u stays consumed by the
        # interpreter, the shim triple lands at the target token
        cfg2 = resolve_stdio_command(
            {"command": "python3", "args": ["-u", "server.py"]}, app_root=tmp_path
        )
        assert _isolated_python_args(cfg2)[0] == "-u", cfg2
        assert _isolated_python_args(cfg2)[1].endswith("deps_boot.py"), cfg2
        assert _isolated_python_args(cfg2)[2] == str(app_deps_dir(tmp_path)), cfg2
        assert _isolated_python_args(cfg2)[3] == "server.py", cfg2
        # -c launches shim too (deps_boot has a -c arm): raw PYTHONPATH
        # would skip .pth hooks and an editable import would die
        cfg3 = resolve_stdio_command(
            {"command": "python3", "args": ["-c", "import server"]}, app_root=tmp_path
        )
        assert _isolated_python_args(cfg3)[0].endswith("deps_boot.py"), cfg3
        assert _isolated_python_args(cfg3)[1:] == [
            str(app_deps_dir(tmp_path)),
            "-c",
            "import server",
        ], cfg3
        pp3 = (cfg3.get("env") or {}).get("PYTHONPATH", "")
        assert str(app_deps_dir(tmp_path)) not in pp3.split(os.pathsep), cfg3
        # `python -- server.py` is `python server.py`: the separator is
        # normalized away and the script shims; an operand that needs the
        # dash-guard stays unshimmable
        cfg_dd = resolve_stdio_command(
            {"command": "python3", "args": ["--", "server.py"]}, app_root=tmp_path
        )
        assert _isolated_python_args(cfg_dd)[0].endswith("deps_boot.py"), cfg_dd
        assert _isolated_python_args(cfg_dd)[1:] == [
            str(app_deps_dir(tmp_path)),
            "server.py",
        ], cfg_dd
        cfg_dd2 = resolve_stdio_command(
            {"command": "python3", "args": ["--", "-weird.py"]}, app_root=tmp_path
        )
        assert "deps_boot" not in " ".join(cfg_dd2["args"]), cfg_dd2
        # attached -cCODE is normalized like -mMODULE
        cfg3b = resolve_stdio_command(
            {"command": "python3", "args": ["-cimport server"]}, app_root=tmp_path
        )
        assert _isolated_python_args(cfg3b)[2:] == ["-c", "import server"], cfg3b
        # -S skips site initialization, so the shim itself could never
        # import (kiro_crew lives in site-packages) - unshimmable, keep the
        # PYTHONPATH transport; same for -E/-I and combined spellings
        # an attached -W VALUE is not a flag cluster: the uppercase letters
        # inside ignore::ImportWarning must not read as -S/-E/-I
        cfg6 = resolve_stdio_command(
            {"command": "python3", "args": ["-Wignore::ImportWarning", "server.py"]},
            app_root=tmp_path,
        )
        assert _isolated_python_args(cfg6)[1].endswith("deps_boot.py"), cfg6
        assert _isolated_python_args(cfg6)[2] == str(app_deps_dir(tmp_path)), cfg6
        # -S/-E/-I make BOTH the -m spelling and PYTHONPATH inert, so those
        # forms shim via the stdlib-only deps_boot launched BY ABSOLUTE PATH
        for flags in (["-S"], ["-E"], ["-I"], ["-s"], ["-uS"], ["-us"]):
            cfg5 = resolve_stdio_command(
                {"command": "python3", "args": [*flags, "server.py"]}, app_root=tmp_path
            )
            prefix = flags if flags in (["-I"], ["-s"], ["-us"]) else ["-s", *flags]
            assert cfg5["args"][: len(prefix)] == prefix, cfg5
            assert cfg5["args"][len(prefix)].endswith("deps_boot.py"), cfg5
            assert cfg5["args"][len(prefix) + 1] == str(app_deps_dir(tmp_path)), cfg5
            assert cfg5["args"][len(prefix) + 2] == "server.py", cfg5
            pp5 = (cfg5.get("env") or {}).get("PYTHONPATH", "")
            assert str(app_deps_dir(tmp_path)) not in pp5.split(os.pathsep), cfg5
        # attached -mMODULE is CPython-equivalent to the separate form and is
        # normalized so it still gets .pth processing through the shim
        cfg4 = resolve_stdio_command(
            {"command": "python3", "args": ["-mserver"]}, app_root=tmp_path
        )
        assert _isolated_python_args(cfg4)[0].endswith("deps_boot.py"), cfg4
        assert _isolated_python_args(cfg4)[1:] == [
            str(app_deps_dir(tmp_path)),
            "-m",
            "server",
        ], cfg4

    def test_a_stale_deps_tree_without_requirements_is_ignored(self, tmp_path):
        """data/ survives updates, so an update that REMOVES requirements.txt
        leaves the provisioned tree behind - a stale tree must neither
        inject removed dependency code nor route through the shim."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)  # tree present, no requirements.txt
        cfg = resolve_stdio_command(
            {"command": "python3", "args": ["server.py"]}, app_root=tmp_path
        )
        assert "env" not in cfg, cfg
        assert "deps_boot" not in " ".join(cfg.get("args") or []), cfg

    def test_expected_provisioning_emits_the_deps_path_before_the_dir_exists(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        """First enable registers the MCP config BEFORE the backend spawn
        provisions the deps dir, and no reconciliation pass is guaranteed to
        rewrite it. A requirements.txt makes provisioning expected, so the
        deps path is emitted up front - a missing PYTHONPATH entry is inert
        to Python, while omitting it strands the first registration without
        its dependencies."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        cfg = resolve_stdio_command(
            {"command": "python3", "args": ["server.py"]}, app_root=tmp_path
        )
        # the shim triple carries the deps path; addsitedir on the
        # not-yet-existing dir is inert until provisioning fills it
        assert _isolated_python_args(cfg)[0].endswith("deps_boot.py"), cfg
        assert _isolated_python_args(cfg)[1] == str(app_deps_dir(tmp_path)), cfg

    def test_a_symlinked_venv_python_is_abi_matched(self, tmp_path, monkeypatch):
        """A venv python is normally a SYMLINK to the base interpreter;
        the location check must validate the unresolved parent (.venv/bin)
        and leave the interpreter question to the _venv_is_usable probe -
        dereferencing first made standard venvs silently lose their deps."""
        import os as _os
        import sys as _sys

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support")
        import kiro_crew.apps.interpreter as imod

        venv_bin = tmp_path / ".venv" / ("Scripts" if _sys.platform == "win32" else "bin")
        venv_bin.mkdir(parents=True)
        link = venv_bin / "python3"
        try:
            _os.symlink(_sys.executable, link)
        except OSError:
            pytest.skip("symlink not permitted")
        monkeypatch.setattr(imod, "_venv_is_usable", lambda root: True)
        assert imod.path_command_is_abi_matched(tmp_path, str(link))
        # a console script BESIDE the interpreter must not read as one:
        # interpreter operands injected into it would kill the server
        worker = venv_bin / "python-worker"
        worker.write_text("#!/bin/sh\n")
        assert not imod.path_command_is_abi_matched(tmp_path, str(worker))

    def test_a_foreign_path_interpreter_gets_no_deps_pythonpath(self, tmp_path):
        """The deps tree is built by the GATEWAY's pip, ABI-bound to the
        gateway's interpreter. A path-pinned command that is NOT positively
        matched (gateway executable or the version-probed app venv python)
        must not receive the deps PYTHONPATH - 3.12-built wheels injected
        into a foreign 3.11 would kill the server at import. Its env stays
        exactly the pre-deps status quo."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        foreign = tmp_path / "bin" / "python3.11"
        foreign.parent.mkdir()
        foreign.write_text("#!/bin/sh\n")
        foreign.chmod(0o755)
        cfg = resolve_stdio_command(
            {
                "command": str(foreign),
                "args": ["server.py"],
                "env": {"PYTHONPATH": "/manifest/own"},
            },
            app_root=tmp_path,
        )
        assert cfg["command"] == str(foreign), cfg
        assert cfg["env"]["PYTHONPATH"] == "/manifest/own", cfg

    def test_a_path_based_gateway_module_server_gets_no_deps_env(self, tmp_path):
        """The gateway-module exclusion survives the hoist: even a path-based
        command whose args launch a kiro_crew module must not see the app
        deps dir on PYTHONPATH."""
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        cfg = resolve_stdio_command(
            {
                "command": os.path.join("bin", "python3"),
                "args": ["-m", "kiro_crew.apps.builtins.x"],
            },
            app_root=tmp_path,
        )
        assert "env" not in cfg, cfg

    def test_a_gateway_module_server_never_gets_the_deps_pythonpath(self, tmp_path):
        """An app that pip-pins its own kiro_crew copy must not shadow the
        gateway's code: a `-m kiro_crew...` server runs the gateway's OWN
        module on the gateway's interpreter, so the app deps dir stays out of
        its PYTHONPATH entirely."""
        import sys as _sys

        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        app_deps_dir(tmp_path).mkdir(parents=True)
        cfg = resolve_stdio_command(
            {"command": "python3", "args": ["-m", "kiro_crew.apps.builtins.x"]},
            app_root=tmp_path,
        )
        assert cfg["command"] == _sys.executable
        assert "env" not in cfg, cfg

    def test_a_deps_dir_console_script_is_rewritten_and_gets_the_env(self, tmp_path):
        from kiro_crew.apps.bridges import resolve_stdio_command
        from kiro_crew.apps.interpreter import app_deps_dir

        scripts = "Scripts" if platform_compat.IS_WINDOWS else "bin"
        name = "my-mcp-server.exe" if platform_compat.IS_WINDOWS else "my-mcp-server"
        script = app_deps_dir(tmp_path) / scripts / name
        script.parent.mkdir(parents=True)
        (tmp_path / "requirements.txt").write_bytes(b"requests\n")
        _plant_deps_stamp(tmp_path, b"requests\n")
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        cfg = resolve_stdio_command({"command": "my-mcp-server"}, app_root=tmp_path)
        assert cfg["command"] == str(script), cfg
        # The script's shebang is the INSTALLING interpreter (sys.executable),
        # which sees the script's own package only through this env.
        parts = cfg["env"]["PYTHONPATH"].split(os.pathsep)
        assert parts[0] == str(app_deps_dir(tmp_path)), cfg
