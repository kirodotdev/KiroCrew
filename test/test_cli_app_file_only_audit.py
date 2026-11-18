"""The security-log rows a file-only ``kirocrew app enable``/``disable`` writes.

With no gateway answering, the CLI toggles the app itself and writes the
``app_enable``/``app_disable`` row the dashboard route writes for the same action.
Handlers are called directly; ``sel`` and the manager calls are patched through the
``kiro_crew.cli_commands`` facade.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _ns

from kiro_crew import cli_commands as cc


def _result(ok: bool, *, error: str = "nope", error_code: str = "") -> Any:
    return SimpleNamespace(ok=ok, message="done", error=error, error_code=error_code, name="demo")


def _registration(**kw: Any) -> Any:
    base: dict[str, list[str]] = {"agents": [], "skills": [], "crons": [], "errors": []}
    base.update(kw)
    return SimpleNamespace(**base)


def _failed_row(operation: str, error: str) -> dict[str, str]:
    return {
        "caller": "cli",
        "source": "cli",
        "operation": operation,
        "outcome": "failed",
        "resources": "app=demo",
        "error": error,
    }


@pytest.fixture(autouse=True)
def _no_gateway() -> Any:
    with (
        patch("kiro_crew.cli_commands._run_app_action_through_gateway", return_value=False),
        patch("kiro_crew.cli_commands._set_folded_app_switch", return_value=False),
        patch("kiro_crew.cli_commands._register_unrecorded_builtin"),
        patch("kiro_crew.cli_commands._warn_hooks_need_restart"),
        patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
    ):
        yield


def test_disable_then_enable_write_completed_rows_naming_the_grant() -> None:
    with (
        patch("kiro_crew.cli_commands._register_app_crons_to_scheduler"),
        patch("kiro_crew.cli_commands.deregister_app", return_value=_registration()),
        patch("kiro_crew.cli_commands.register_app", return_value=_registration()),
        patch("kiro_crew.cli_commands.disable_app", return_value=_result(True)),
        patch("kiro_crew.cli_commands.enable_app", return_value=_result(True)),
        patch("kiro_crew.cli_commands.sel") as sel,
    ):
        cc._handle_app(_ns(app_action="disable", name="demo"))
        cc._handle_app(_ns(app_action="enable", name="demo"))
    rows = [c.kwargs for c in sel.return_value.log_api_access.call_args_list]
    assert rows == [
        {
            "caller": "cli",
            "source": "cli",
            "operation": "app_disable",
            "outcome": "completed",
            "resources": "app=demo host_exec=revoked",
            "error": "",
        },
        {
            "caller": "cli",
            "source": "cli",
            "operation": "app_enable",
            "outcome": "completed",
            "resources": "app=demo host_exec=granted",
            "error": "",
        },
    ]


@pytest.mark.parametrize(
    ("action", "manager", "error_code"),
    [
        ("enable", "enable_app", ""),
        ("enable", "enable_app", "session_approval_consent_required"),
        ("disable", "disable_app", ""),
    ],
)
def test_refusal_writes_a_failed_row(action: str, manager: str, error_code: str) -> None:
    with (
        patch("kiro_crew.cli_commands.deregister_app", return_value=_registration()),
        patch(
            f"kiro_crew.cli_commands.{manager}",
            return_value=_result(False, error_code=error_code),
        ),
        patch("kiro_crew.cli_commands.get_app_manifest", return_value=None),
        patch("kiro_crew.dashboard.urls.resolve_dashboard_host", return_value="127.0.0.1"),
        patch("kiro_crew.port_resolution.resolve_client_port", return_value=6123),
        patch("kiro_crew.cli_commands.sel") as sel,
        pytest.raises(SystemExit) as exc,
    ):
        cc._handle_app(_ns(app_action=action, name="demo"))
    assert exc.value.code == 1
    sel.return_value.log_api_access.assert_called_once_with(**_failed_row(f"app_{action}", "nope"))


def test_disable_whose_teardown_fails_writes_a_failed_row() -> None:
    with (
        patch(
            "kiro_crew.cli_commands.deregister_app",
            return_value=_registration(errors=["agent busy"]),
        ),
        patch("kiro_crew.cli_commands.disable_app", return_value=_result(True)),
        patch("kiro_crew.cli_commands.sel") as sel,
        pytest.raises(SystemExit),
    ):
        cc._handle_app(_ns(app_action="disable", name="demo"))
    sel.return_value.log_api_access.assert_called_once_with(
        **_failed_row("app_disable", "some of its resources could not be removed")
    )


def test_enable_row_follows_registration_and_records_its_failure() -> None:
    order: list[str] = []
    with (
        patch(
            "kiro_crew.cli_commands._register_app_crons_to_scheduler",
            side_effect=lambda n: order.append("crons"),
        ),
        patch("kiro_crew.cli_commands.register_app", return_value=_registration()),
        patch("kiro_crew.cli_commands.enable_app", return_value=_result(True)),
        patch("kiro_crew.cli_commands.sel") as sel,
    ):
        sel.return_value.log_api_access.side_effect = lambda **kw: order.append(kw["outcome"])
        cc._handle_app(_ns(app_action="enable", name="demo"))
    assert order == ["crons", "completed"]
    with (
        patch("kiro_crew.cli_commands.register_app", side_effect=RuntimeError("store busy")),
        patch("kiro_crew.cli_commands.enable_app", return_value=_result(True)),
        patch("kiro_crew.cli_commands.sel") as sel,
        pytest.raises(RuntimeError, match="store busy"),
    ):
        cc._handle_app(_ns(app_action="enable", name="demo"))
    sel.return_value.log_api_access.assert_called_once_with(
        **_failed_row("app_enable", "store busy")
    )
