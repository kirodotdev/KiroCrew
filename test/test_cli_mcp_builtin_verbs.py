"""Tests for the gated ``mcp-<builtin>`` CLI verbs (#5901).

``_BUILTIN_NAMES`` is shared between HTTP route registration (every builtin)
and MCP-verb registration (only builtins that ship an ``mcp_server`` module).
The risk this file pins: a verb registered for a module-less builtin dies with
a raw ``ModuleNotFoundError`` traceback when invoked, and every NEW builtin
re-adds such a verb silently.
"""

import sys
import types

import pytest

from kiro_crew import cli
from kiro_crew.apps.builtins import BUILTIN_NAMES


def _run_cli(monkeypatch, tmp_path, argv):
    """Drive ``kirocrew <argv>`` through the real ``main()`` in isolation."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "boot_platform", lambda *_a, **_k: None)
    # _setup_cli_logging attaches a file handler under tmp_path to the global
    # logger; the retained open file blocks Windows cleanup and accumulates
    # handlers across tests.
    monkeypatch.setattr(cli, "_setup_cli_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["kirocrew", *argv])
    cli.main()


def _moduleless_builtins() -> list[str]:
    """Builtin names that do not resolve an ``mcp_server`` module."""
    return [name for name in BUILTIN_NAMES if not cli._builtin_mcp_server_available(name)]


class TestPredicate:
    def test_mochi_resolves(self):
        """mochi ships ``mcp_server.py`` — the one builtin whose verb must stay."""
        assert cli._builtin_mcp_server_available("mochi") is True

    def test_moduleless_builtins_exist_and_do_not_resolve(self):
        """The defect class is live: some builtins have no ``mcp_server`` module."""
        missing = _moduleless_builtins()
        assert missing, "every builtin resolved an mcp_server module — gate is vacuous"
        assert "mochi" not in missing

    def test_missing_parent_package_returns_false_instead_of_raising(self):
        """A name with no package under ``apps/builtins`` resolves to False."""
        assert cli._builtin_mcp_server_available("no_such_builtin_xyz") is False

    def test_probing_does_not_import_builtin_packages(self):
        """Resolution must not EXECUTE app packages: each builtin's
        ``__init__`` pulls in its backend routes, and the probe must stay a
        pure filesystem walk even on the invocations that do run it."""
        before = set(sys.modules)
        for name in BUILTIN_NAMES:
            cli._builtin_mcp_server_available(name)
        assert set(sys.modules) == before

    def test_non_mcp_invocations_do_not_probe(self, monkeypatch, tmp_path, capsys):
        """A command that names no ``mcp-*`` verb must not pay the probe at
        all — parser build on the gateway boot path stays free of filesystem
        work (registration precision is unobservable there: the verbs are
        hidden from help and dispatch cannot reach them)."""

        def _fail(_name):  # pragma: no cover - failure path
            raise AssertionError("probe ran on a non-mcp invocation")

        monkeypatch.setattr(cli, "_builtin_mcp_server_available", _fail)
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(monkeypatch, tmp_path, ["--help"])
        assert excinfo.value.code == 0
        assert "usage" in capsys.readouterr().out


class TestParserRegistration:
    def test_mochi_verb_is_registered(self, monkeypatch, tmp_path, capsys):
        """``kirocrew mcp-mochi --help`` parses: the verb exists."""
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(monkeypatch, tmp_path, ["mcp-mochi", "--help"])
        assert excinfo.value.code == 0
        assert "mcp-mochi" in capsys.readouterr().out

    def test_moduleless_builtins_are_not_registered(self, monkeypatch, tmp_path, capsys):
        """A module-less builtin's verb is rejected by argparse, not registered."""
        name = _moduleless_builtins()[0]
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(monkeypatch, tmp_path, [f"mcp-{name}"])
        assert excinfo.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_builtin_gaining_a_server_module_gains_the_verb(self, monkeypatch, tmp_path, capsys):
        """Registration is driven by the predicate: a builtin that starts
        resolving an ``mcp_server`` module is registered with no list edit."""
        name = _moduleless_builtins()[0]
        real = cli._builtin_mcp_server_available
        monkeypatch.setattr(
            cli,
            "_builtin_mcp_server_available",
            lambda bname: True if bname == name else real(bname),
        )
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(monkeypatch, tmp_path, [f"mcp-{name}", "--help"])
        assert excinfo.value.code == 0
        assert f"mcp-{name}" in capsys.readouterr().out


class TestDispatch:
    def test_verb_delegates_to_the_shared_app_mcp_helper(self, monkeypatch, tmp_path):
        """Dispatch is a delegation to ``_run_app_mcp_server`` — the ONE
        spelling of "import the builtin's mcp_server and run it or refuse
        cleanly", shared with the ``kirocrew app mcp <name>`` manifest path."""
        from kiro_crew import cli_commands

        calls: list[str] = []
        monkeypatch.setattr(cli_commands, "_run_app_mcp_server", calls.append)
        _run_cli(monkeypatch, tmp_path, ["mcp-mochi"])
        assert calls == ["mochi"]

    def test_moduleless_dispatch_exits_with_clean_message(self, monkeypatch, tmp_path, capsys):
        """End-to-end pin of the #5901 contract: if a module-less verb ever
        reaches dispatch (predicate stubbed True so the parser registers it),
        the shared helper refuses with a one-line stderr message and exit 1 —
        never a raw ModuleNotFoundError traceback.

        Note: the real import attempt executes the builtin's parent package
        (its ``__init__``), permanently populating ``sys.modules`` for this
        worker — harmless for the probe test, which snapshots inside itself."""
        name = _moduleless_builtins()[0]
        monkeypatch.setattr(cli, "_builtin_mcp_server_available", lambda _bname: True)
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(monkeypatch, tmp_path, [f"mcp-{name}"])
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "has no MCP server" in err
        assert "Traceback" not in err

    def test_resolvable_builtin_runs_its_mcp_server(self, monkeypatch, tmp_path):
        """A registered verb reaches ``run_mcp_server()`` on the real module
        resolution path (import stubbed at the module seam)."""
        from kiro_crew import cli_commands

        ran = {"hit": False}
        stub = types.SimpleNamespace(run_mcp_server=lambda: ran.__setitem__("hit", True))
        real_import = cli_commands.importlib.import_module

        def stubbing_import(module, *a, **k):
            if module == "kiro_crew.apps.builtins.mochi.mcp_server":
                return stub
            return real_import(module, *a, **k)

        monkeypatch.setattr(
            cli_commands,
            "importlib",
            types.SimpleNamespace(
                **{**vars(cli_commands.importlib), "import_module": stubbing_import}
            ),
        )
        _run_cli(monkeypatch, tmp_path, ["mcp-mochi"])
        assert ran["hit"] is True
