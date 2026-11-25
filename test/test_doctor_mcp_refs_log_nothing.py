"""``kirocrew doctor``'s ``mcp tool refs`` rows log nothing while they project.

Each mirror logs a server it withholds as a starting session would. The doctor
projects the default spec on every selectable backend, and each authored agent on
the backend in use, with no session behind either, so those WARNING records would
open a stock report with lines about backends nobody selected and append them to
``gateway.log``. These tests pin that both rows run their projection silenced,
still print their row, and leave logging as they found it.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.doctor_checks import mcp

_MIRROR_LOGGER = "kiro_crew.providers.mirrors.codex"


class _NarratingMirror:
    """A mirror that withholds every server and says so, as the real ones do."""

    def session_params(self, agent: str, **_kw: object) -> dict:
        logging.getLogger(_MIRROR_LOGGER).warning(
            "codex session MCP: withholding server %r", f"{agent}-server"
        )
        return {"mcpServers": []}


def _write(name: str) -> None:
    directory = kiro_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    spec = {"name": name, "mcpServers": {}, "tools": ["fs_read", "@kc-absent"]}
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _default_spec_row() -> None:
    _write(mcp._MAIN_AGENT_NAME)
    mcp._doctor_unresolved_mcp_refs()


def _authored_agent_row() -> None:
    _write("reprobot")
    mcp._doctor_authored_agent_mcp_refs(SimpleNamespace(agent=SimpleNamespace(acp_backend="codex")))


@pytest.mark.parametrize("row", [_default_spec_row, _authored_agent_row])
def test_the_projection_logs_no_warning_and_the_row_still_prints(
    row, monkeypatch, caplog, capsys
) -> None:
    from kiro_crew import acp_backends, providers

    monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: ["codex"])
    # The PACKAGE module: the driver imports ``mirror_for`` from ``providers.mirrors``.
    monkeypatch.setattr(providers.mirrors, "mirror_for", lambda _backend: _NarratingMirror())

    with caplog.at_level(logging.WARNING):
        row()
        # The silence ends with the projection: a warning after it is recorded.
        logging.getLogger(_MIRROR_LOGGER).warning("after the doctor row")

    assert [r.getMessage() for r in caplog.records] == ["after the doctor row"]
    rows = [line for line in capsys.readouterr().out.splitlines() if "mcp tool refs:" in line]
    assert rows and all("'@kc-absent'" in line for line in rows)
