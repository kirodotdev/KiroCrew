"""``kirocrew eval`` reports a session that could not start as one line.

A signed-out kiro-cli raises ``AcpAuthRequired`` (an ``AcpError``), a KAS engine that
cannot be installed raises ``AcpRuntimeError``, and a host that cannot sandbox the
session raises ``SandboxUnavailableError`` or ``SandboxCeilingUnsealable``, from a
scenario session's start or from the ``--judge`` session's. ``cli.main()``'s eval
branch prints that refusal's own text as one stderr line and exits 1, logging the
traceback at DEBUG only. The scenario runner is stubbed, so no model is called and no
report is written.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli
from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeError
from kiro_crew.acp.transport_errors import AcpAuthRequired
from kiro_crew.sandbox import SandboxCeilingUnsealable, SandboxUnavailableError

_SIGNED_OUT = "Not signed in to kiro-cli. Run `kiro-cli login`, then retry."
_KAS_INSTALL = "KAS engine bundle: the download failed (HTTP 503). Run `kirocrew kas install`."
_NO_BACKEND = "No sandbox backend on this host. Run `kirocrew sandbox install-profile`."
_UNSEALABLE = "Cannot seal computer_use.json: a dangling symlink sits there. Remove it."


class _RefusingProvider:
    """A provider whose ``start()`` raises the backend's refusal."""

    def __init__(self, refusal: Exception) -> None:
        self._refusal = refusal

    async def start(self) -> None:
        raise self._refusal

    async def shutdown(self) -> None:
        return None


@pytest.mark.parametrize(
    ("judge", "refusal", "text"),
    [
        (False, AcpAuthRequired(_SIGNED_OUT, backend="kiro"), _SIGNED_OUT),
        (True, AcpRuntimeError(_KAS_INSTALL), _KAS_INSTALL),
        (
            False,
            SandboxUnavailableError(_NO_BACKEND, kind="no_backend", detail="userns denied"),
            _NO_BACKEND,
        ),
        (True, SandboxCeilingUnsealable(_UNSEALABLE), _UNSEALABLE),
    ],
    ids=[
        "scenario-session-signed-out",
        "judge-session-kas-install",
        "scenario-session-no-sandbox-backend",
        "judge-session-unsealable-ceiling",
    ],
)
def test_a_session_start_refusal_is_one_stderr_line_and_exit_1(
    judge: bool,
    refusal: Exception,
    text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "boot_platform", lambda *_a, **_k: None)
    # _setup_cli_logging attaches a file handler to the global logger.
    monkeypatch.setattr(cli, "_setup_cli_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sys, "argv", ["kirocrew", "eval", "--no-jail", *(["--judge"] if judge else [])]
    )
    # No kiro-cli probe: the stored backend pick is not what this test is about.
    monkeypatch.setattr("kiro_crew.agent_sdk.backends.ensure_kiro_cli_kas_verdict", lambda: None)
    monkeypatch.setattr(
        "kiro_crew.cli_commands.build_provider_factory",
        lambda _config: lambda _key, **_kw: _RefusingProvider(refusal),
    )
    runner = MagicMock()
    if judge:
        # The scenarios ran; the real LLMJudge then starts its own session.
        runner.run_scenarios = AsyncMock(
            return_value=[SimpleNamespace(passed=True, sessions=[], summary=dict)]
        )
    else:
        # What EvalRunner._run_session lets out when provider.start() refuses.
        runner.run_scenarios = AsyncMock(side_effect=refusal)
    monkeypatch.setattr("kiro_crew.cli_commands.EvalRunner", lambda **_kw: runner)
    caplog.set_level(logging.DEBUG, logger="kiro_crew.cli")

    # The refusal is caught too, so a refusal that escapes main() fails the assertion
    # below by name instead of erroring the test.
    with pytest.raises((SystemExit, type(refusal))) as exited:
        cli.main()

    assert (exited.type, getattr(exited.value, "code", None)) == (SystemExit, 1)
    assert capsys.readouterr().err.splitlines() == [f"Error: {text}"]
    assert [r.exc_info[1] for r in caplog.records if r.exc_info] == [refusal]
    assert not (tmp_path / "eval_results").exists()
