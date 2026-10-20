"""``kiro_crew.cli.main`` hands every command to its handler.

The dispatch chain stays in ``cli.main``, where the lazy-import ratchet and the boot-order
pins read it, while the arguments come from the ``cli_parser_*`` registrars. These tests run
the real ``main`` -- the parser, the prologue and the chain -- for every command but
``gateway`` (``test_cli_main_entry.py``'s lock-refusal and ``--seed`` tests drive that branch
in-process), with the boot steps and the command's handler replaced by recorders, and pin
which handler each command reaches and with what.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import cli
from kiro_crew.platform import PlatformCompositionError

Call = tuple[tuple[Any, ...], dict[str, Any]]


class _Config:
    """What ``main`` loads before booting the platform; the boot step is a recorder too."""

    @staticmethod
    def load() -> object:
        return object()


@pytest.fixture
def booted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Replace every boot step ``main`` runs before dispatch; return the steps it took."""
    steps: list[str] = []
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    for marker in ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL"):
        monkeypatch.delenv(marker, raising=False)
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        monkeypatch.setenv(var, "1")  # an mcp-* command sets them; restored either way
    monkeypatch.setattr(cli, "KiroCrewConfig", _Config)
    monkeypatch.setattr(cli, "boot_platform", lambda cfg: steps.append("boot_platform"))
    monkeypatch.setattr(cli, "ensure_data_home", lambda: steps.append("ensure_data_home"))
    monkeypatch.setattr(cli, "_setup_cli_logging", lambda command, verbose: steps.append("logging"))
    monkeypatch.setattr(
        cli, "_jail_reexec_gate", lambda command, no_jail: steps.append(f"jail:{command}")
    )
    lazy = {
        "kiro_crew.platform.tree_liveness": "hold_running_tree_lock",
        "kiro_crew.kiro_prerequisite": "register_process_start_override_attestation",
        "kiro_crew.memory_stores": "repair_legacy_member_stores",
    }
    for module_name, attr in lazy.items():
        monkeypatch.setattr(
            importlib.import_module(module_name), attr, lambda attr=attr: steps.append(attr)
        )
    return steps


def _recorder(calls: list[Call], result: Any = None) -> Callable[..., Any]:
    def record(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return result

    return record


def _async_recorder(calls: list[Call]) -> Callable[..., Any]:
    async def record(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    return record


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", *argv])
    cli.main()


def _command_of(call: Call) -> str:
    (args,), _ = call
    assert isinstance(args, argparse.Namespace)
    return str(args.command)


# Commands whose branch passes the parsed namespace to one handler of a sibling module.
_NAMESPACE_HANDLERS = [
    (("cron", "list"), "kiro_crew.cli_commands", "_cron"),
    (("spawn", "list"), "kiro_crew.cli_commands", "_spawn"),
    (("learn", "list"), "kiro_crew.cli_commands", "_learn"),
    (("artifact", "list"), "kiro_crew.cli_commands", "_artifact"),
    (("memory", "list"), "kiro_crew.cli_commands", "_memory_cmd"),
    (("security", "audit"), "kiro_crew.cli_commands", "_security"),
    (("tailnet", "status"), "kiro_crew.cli_commands", "_tailnet"),
    (("telemetry", "status"), "kiro_crew.cli_commands", "_telemetry"),
    (("policy", "show"), "kiro_crew.cli_commands", "_policy"),
    (("secrets", "import"), "kiro_crew.cli_commands", "_handle_secrets"),
    (("pod", "ls"), "kiro_crew.cli_commands", "_pod"),
    (("agent", "list"), "kiro_crew.cli_commands", "_handle_agent"),
    (("workspace", "list"), "kiro_crew.cli_commands", "_handle_workspace"),
    (("app", "list"), "kiro_crew.cli_commands", "_handle_app"),
    (("logs",), "kiro_crew.cli_server", "_logs_cmd"),
    (("token",), "kiro_crew.cli_server", "_token"),
    (("status",), "kiro_crew.cli_server", "_status"),
]


@pytest.mark.parametrize(
    ("argv", "module_name", "attr"),
    _NAMESPACE_HANDLERS,
    ids=[argv[0] for argv, _m, _a in _NAMESPACE_HANDLERS],
)
def test_a_command_hands_its_namespace_to_its_handler(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    module_name: str,
    attr: str,
) -> None:
    calls: list[Call] = []
    monkeypatch.setattr(importlib.import_module(module_name), attr, _recorder(calls))
    _run(monkeypatch, *argv)
    assert [_command_of(call) for call in calls] == [argv[0]]
    # The prologue of a plain command, in its order.
    assert booted == [
        "hold_running_tree_lock",
        "ensure_data_home",
        "logging",
        "boot_platform",
        "repair_legacy_member_stores",
    ]


# Commands whose handler the facade binds, and which take the parsed namespace.
_FACADE_HANDLERS = [
    (("knowledge", "stats"), "_knowledge"),
    (("consolidate",), "_consolidate_cmd"),
    (("config", "get"), "_config_cmd"),
]


@pytest.mark.parametrize(
    ("argv", "attr"), _FACADE_HANDLERS, ids=[argv[0] for argv, _a in _FACADE_HANDLERS]
)
def test_a_facade_bound_handler_gets_the_namespace(
    booted: list[str], monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...], attr: str
) -> None:
    calls: list[Call] = []
    monkeypatch.setattr(cli, attr, _recorder(calls))
    _run(monkeypatch, *argv)
    assert [_command_of(call) for call in calls] == [argv[0]]


# Commands whose handler returns an exit status that ``main`` raises when it is non-zero.
_STATUS_HANDLERS = [
    (("perf", "sample"), None, "perf_cmd", set()),
    (("bench", "list"), None, "bench_cmd", set()),
    (("desktop", "metrics"), None, "desktop_cmd", set()),
    (("snapshot",), "kiro_crew.snapshot", "snapshot_main", {"parsed"}),
    (("restore",), "kiro_crew.snapshot", "restore_main", {"parsed"}),
]


@pytest.mark.parametrize(
    ("argv", "module_name", "attr", "keywords"),
    _STATUS_HANDLERS,
    ids=[argv[0] for argv, _m, _a, _k in _STATUS_HANDLERS],
)
def test_a_non_zero_status_from_the_handler_is_the_exit_status(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    module_name: str | None,
    attr: str,
    keywords: set[str],
) -> None:
    calls: list[Call] = []
    target = cli if module_name is None else importlib.import_module(module_name)
    monkeypatch.setattr(target, attr, _recorder(calls, result=3))
    with pytest.raises(SystemExit) as exited:
        _run(monkeypatch, *argv)
    assert exited.value.code == 3
    assert len(calls) == 1
    positional, named = calls[0]
    assert set(named) == keywords
    parsed = named.get("parsed") or positional[0]
    assert parsed.command == argv[0]


# Commands whose branch exits with whatever status the handler returns, zero included.
_EXIT_HANDLERS = [
    (("service", "status"), "kiro_crew.cli_server", "_service_cmd"),
    (("sandbox", "status"), "kiro_crew.cli_server", "_sandbox_cmd"),
    (("kas", "status"), "kiro_crew.cli_kas", "kas_cmd"),
    (("cloud", "list"), None, "handle_cloud"),
]


@pytest.mark.parametrize(
    ("argv", "module_name", "attr"),
    _EXIT_HANDLERS,
    ids=[argv[0] for argv, _m, _a in _EXIT_HANDLERS],
)
def test_the_handler_status_is_the_exit_status(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    module_name: str | None,
    attr: str,
) -> None:
    calls: list[Call] = []
    target = cli if module_name is None else importlib.import_module(module_name)
    monkeypatch.setattr(target, attr, _recorder(calls, result=0))
    with pytest.raises(SystemExit) as exited:
        _run(monkeypatch, *argv)
    assert exited.value.code == 0
    assert [_command_of(call) for call in calls] == [argv[0]]


@pytest.mark.parametrize(
    ("argv", "module_name", "attr"),
    [
        (("run", "TASK.md"), "kiro_crew.cli_server", "_run_task"),
        (("eval",), "kiro_crew.cli_commands", "_run_eval"),
    ],
    ids=["run", "eval"],
)
def test_a_coroutine_handler_runs_to_completion(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    module_name: str,
    attr: str,
) -> None:
    calls: list[Call] = []
    monkeypatch.setattr(importlib.import_module(module_name), attr, _async_recorder(calls))
    _run(monkeypatch, *argv)
    assert [_command_of(call) for call in calls] == [argv[0]]
    # Both are jailed commands: the attestation and the jail gate run before dispatch.
    assert "register_process_start_override_attestation" in booted
    assert f"jail:{argv[0]}" in booted


def test_chat_reaches_the_chat_runner_with_its_flags(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Call] = []
    monkeypatch.setattr(cli, "_run_chat", _recorder(calls))
    _run(monkeypatch, "chat", "-m", "hello", "--model", "m1", "--agent", "a1")
    assert calls == [(("hello", "m1"), {"agent": "a1"})]
    assert "jail:chat" in booted


def test_doctor_runs_even_when_the_platform_cannot_compose(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = PlatformCompositionError("companion missing")

    def refuse(cfg: object) -> None:
        raise failure

    calls: list[Call] = []
    monkeypatch.setattr(cli, "boot_platform", refuse)
    monkeypatch.setattr(cli, "_doctor", _recorder(calls))
    _run(monkeypatch, "doctor", "--bundle")
    assert calls == [((), {"platform_boot_error": failure, "bundle": True})]
    assert "repair_legacy_member_stores" not in booted


def test_an_unexpected_boot_error_does_not_stop_a_standalone_command(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(cfg: object) -> None:
        raise RuntimeError("corrupt config")

    calls: list[Call] = []
    monkeypatch.setattr(cli, "boot_platform", broken)
    monkeypatch.setattr(cli, "_doctor", _recorder(calls))
    _run(monkeypatch, "doctor")
    assert calls == [((), {"platform_boot_error": None, "bundle": False})]


def test_setup_and_manifest_get_their_flags_as_keywords(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_calls: list[Call] = []
    manifest_calls: list[Call] = []
    monkeypatch.setattr(cli, "_setup", _recorder(setup_calls))
    monkeypatch.setattr(cli, "_manifest", _recorder(manifest_calls))
    _run(monkeypatch, "setup", "--agent-only", "--clean")
    _run(monkeypatch, "manifest", "--alias", "me", "--url")
    assert setup_calls == [
        (
            (),
            {
                "agent_only": True,
                "electron_only": False,
                "clean": True,
                "slack": False,
                "whatsapp": False,
            },
        )
    ]
    assert manifest_calls == [((), {"alias": "me", "output": None, "url": True})]


def test_ledger_sweep_passes_its_purge_options(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Call] = []
    sweep = importlib.import_module("kiro_crew.ledger_sweep")
    monkeypatch.setattr(sweep, "run_command", _recorder(calls))
    _run(monkeypatch, "ledger-sweep", "--purge", "--older-than-days", "7")
    assert calls == [((), {"purge": True, "older_than_days": 7.0, "purge_unreadable": False})]


def test_computer_forwards_its_remaining_arguments(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Call] = []
    computer_cli = importlib.import_module("kiro_crew.computer_use.cli")
    monkeypatch.setattr(computer_cli, "run_computer", _recorder(calls))
    _run(monkeypatch, "computer", "doctor", "--json")
    assert calls == [((["doctor", "--json"],), {})]


@pytest.mark.parametrize(
    ("argv", "attr", "expected"),
    [
        (("update",), "_update", ((), {"force": False})),
        (("update", "--force"), "_update", ((), {"force": True})),
        (("update", "approve"), "_update_approve", ((), {})),
        (("file-delivery", "approve"), "_file_delivery_approve", ((), {})),
        (("stop", "--port", "7001"), "_stop", ((7001,), {"expect_pid": None})),
        (("restart", "--port", "7002"), "_restart", ((7002,), {})),
        (("gateway-pid",), "_gateway_pid", ((), {})),
    ],
    ids=["update", "update-force", "update-approve", "file-delivery", "stop", "restart", "pid"],
)
def test_a_gateway_control_command_reaches_its_server_handler(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    attr: str,
    expected: Call,
) -> None:
    calls: list[Call] = []
    monkeypatch.setattr(importlib.import_module("kiro_crew.cli_server"), attr, _recorder(calls))
    _run(monkeypatch, *argv)
    assert calls == [expected]


def test_logout_revokes_on_the_resolved_client_port(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = importlib.import_module("kiro_crew.cli_server")
    calls: list[Call] = []
    monkeypatch.setattr(server, "resolve_client_port", lambda port: 9000 + (port or 0))
    monkeypatch.setattr(server, "_logout", _recorder(calls))
    _run(monkeypatch, "logout", "--port", "5")
    assert calls == [((9005,), {})]


_MCP_SERVERS = [
    ("mcp-cron", "kiro_crew.mcp_cron", "run_mcp_server"),
    ("mcp-core", "kiro_crew.mcp_core", "run_mcp_core_server"),
    ("mcp-computer", "kiro_crew.mcp_computer", "run_mcp_server"),
    ("mcp-dashboard", "kiro_crew.mcp_dashboard", "run_mcp_server"),
    ("mcp-work", "kiro_crew.mcp_work", "run_mcp_server"),
    ("mcp-crew-log", "kiro_crew.mcp_crew_log", "run_mcp_server"),
    ("mcp-debug", "kiro_crew.mcp_debug", "run_mcp_server"),
    ("mcp-panel", "kiro_crew.mcp_panel", "run_mcp_server"),
    ("mcp-guide", "kiro_crew.mcp_guide", "run_mcp_server"),
    ("mcp-mate", "kiro_crew.mcp_mate", "run_mcp_server"),
]


@pytest.mark.parametrize(
    ("command", "module_name", "entry"), _MCP_SERVERS, ids=[c for c, _m, _e in _MCP_SERVERS]
)
def test_an_internal_mcp_command_starts_its_server(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    module_name: str,
    entry: str,
) -> None:
    """The server module is stood in for in ``sys.modules``, as ``main`` loads it lazily."""
    started: list[str] = []
    stand_in = types.ModuleType(module_name)
    setattr(stand_in, entry, lambda: started.append(command))
    monkeypatch.setitem(sys.modules, module_name, stand_in)
    _run(monkeypatch, command)
    assert started == [command]
    # A child of a gateway: it takes the tree hold every non-gateway command takes, and
    # skips the member-store repair its gateway already ran.
    assert booted == ["hold_running_tree_lock", "ensure_data_home", "logging", "boot_platform"]


def test_a_builtin_app_mcp_command_runs_that_apps_server(
    booted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = cli._BUILTIN_NAMES[0]
    calls: list[Call] = []
    monkeypatch.setattr(cli, "_builtin_mcp_server_available", lambda candidate: True)
    commands = importlib.import_module("kiro_crew.cli_commands")
    monkeypatch.setattr(commands, "_run_app_mcp_server", _recorder(calls))
    _run(monkeypatch, f"mcp-{name}")
    assert calls == [((name,), {})]


def test_no_command_prints_the_banner_and_the_help(
    booted: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(monkeypatch)
    out = capsys.readouterr().out
    assert out.startswith(cli.BANNER)
    assert "usage: kirocrew" in out
    assert "boot_platform" not in booted


@pytest.mark.parametrize(
    ("raw", "complaint"),
    [("abc", "is not a valid integer"), ("70000", "is outside the valid port range 1-65535")],
    ids=["not-an-integer", "out-of-range"],
)
def test_an_unusable_kirocrew_port_stops_every_command_before_it_starts(
    booted: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str,
    complaint: str,
) -> None:
    monkeypatch.setenv("KIROCREW_PORT", raw)
    with pytest.raises(SystemExit) as exited:
        _run(monkeypatch, "status")
    assert exited.value.code == 1
    assert f"KIROCREW_PORT={raw!r} {complaint}" in capsys.readouterr().err
    assert booted == []
