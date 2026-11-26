"""An unset ``${VAR}`` in a stubbed server's env is logged with that server's name.

The launch-approval hash (``_admit_launch``) and the card's entry guard
(``launch_resolve.matches_entry``) expand the declared env only to fingerprint
it; both must name the server in the unset-placeholder warning, never ``None``.
"""

from __future__ import annotations

import logging
import sys

from mcp_launch_approval_helpers import (  # noqa: F401
    _APPROVED_CMD,
    _approval_module,
    _approvals_for,
    _rewrite,
    _write_agent,
)

# Exercise the real resolver and approval store rather than the suite's launch stub.
ENFORCE_LAUNCH_APPROVAL = True

_LOGGER = "kiro_crew.mcp_gateway.rewriter"
_VAR = "KC_TEST_UNSET_PLACEHOLDER"
_ENV = {"URL": "${%s}" % _VAR}


def _unset_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "is unset" in r.getMessage()]


def test_a_refused_launch_names_the_server_in_the_unset_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(_VAR, raising=False)
    approvals = _approvals_for(_approval_module(), "srv", _APPROVED_CMD, [])
    _write_agent(
        tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": [], "env": dict(_ENV)}}
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert approvals.refused
    lines = _unset_lines(caplog)
    assert lines and all("'srv'" in m for m in lines), lines


def test_the_entry_guard_names_the_server_in_the_unset_warning(monkeypatch, caplog):
    from kiro_crew.mcp_gateway import launch_resolve, rewriter
    from kiro_crew.mcp_gateway.launch_approval import ResolvedLaunch, launch_fingerprint

    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.setattr(rewriter, "_resolve_target_command", lambda *args: sys.executable)
    entry = {"command": sys.executable, "args": [], "env": dict(_ENV)}
    # A matching fingerprint, so the guard goes on to expand the env.
    launch = ResolvedLaunch(launch_fingerprint(sys.executable, [], _ENV), sys.executable, ())
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        launch_resolve.matches_entry(entry, [launch], server="srv")

    lines = _unset_lines(caplog)
    assert lines and all("'srv'" in m for m in lines), lines
