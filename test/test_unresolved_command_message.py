"""``command not found`` must say WHERE it looked.

Both the dashboard probe and the agent config rebuild reported an unresolvable
MCP command as a bare ``command not found: <name>``, and the searched
directories existed only at DEBUG. A reader could not tell "this binary is not
installed" from "it is installed somewhere the search path does not cover" --
two different problems with two different fixes -- without reading the source.

Path fixtures are built through :func:`_abs` and ``os.pathsep`` rather than
written as POSIX literals: on Windows ``os.pathsep`` is ``;`` and ``normpath``
returns backslashes, so a hardcoded ``"/usr/bin:/bin"`` is ONE entry there and
would silently assert something different.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import env as env_mod
from kiro_crew.env import describe_search_path


def _abs(*parts: str) -> str:
    root = "C:\\" if os.name == "nt" else "/"
    return os.path.normpath(os.path.join(root, *parts))


USR_BIN = _abs("usr", "bin")
BIN = _abs("bin")
BASE_PATH = os.pathsep.join([USR_BIN, BIN])


# ── describe_search_path ──────────────────────────────────────────────────────


def test_names_the_directories_and_the_count():
    out = describe_search_path(BASE_PATH)
    assert USR_BIN in out and BIN in out
    assert "2" in out  # states how many were searched


def test_truncates_but_states_the_total():
    limit = env_mod._SEARCH_PATH_REPORT_LIMIT
    total = limit + 20
    many = os.pathsep.join(_abs(f"d{i}") for i in range(total))
    out = describe_search_path(many)
    assert f"searched {total} directories" in out
    assert f"+{total - limit} more" in out
    assert _abs(f"d{total - 1}") not in out


def test_handles_an_empty_path():
    assert "empty PATH" in describe_search_path("")


def test_ignores_empty_entries():
    """A trailing or doubled separator must not be counted as a directory."""
    padded = os.pathsep.join(["", USR_BIN, "", BIN, ""])
    assert "searched 2 directories" in describe_search_path(padded)


# ── the dashboard-facing string ───────────────────────────────────────────────


def test_probe_error_states_how_many_directories_were_searched():
    from kiro_crew.mcp_discovery import _unresolved_error

    msg = _unresolved_error("vendor-mcp", BASE_PATH)
    assert "command not found: vendor-mcp" in msg  # prefix preserved for callers
    assert "2 directories" in msg


def test_probe_error_without_a_search_path_is_unchanged():
    """No search happened, so claim nothing about directories."""
    from kiro_crew.mcp_discovery import _unresolved_error

    assert _unresolved_error("vendor-mcp") == "command not found: vendor-mcp"


# ── a Node.js program names what to install ───────────────────────────────────


@pytest.mark.parametrize("command", ["npx", "node", "npm", "npx.cmd", "NPM.EXE"])
@pytest.mark.parametrize("search_path", [BASE_PATH, ""])
def test_a_missing_node_program_says_to_install_node(command, search_path):
    """``npx`` is not something a reader knows to install; Node.js is."""
    from kiro_crew.constants import MIN_NODE_VERSION, format_node_version
    from kiro_crew.mcp_discovery import _unresolved_error

    msg = _unresolved_error(command, search_path)
    assert msg.startswith(f"command not found: {command}")
    assert "is part of Node.js" in msg
    assert f"Install Node.js {format_node_version(MIN_NODE_VERSION)} or later" in msg
    assert msg.endswith("then restart the gateway.")


@pytest.mark.parametrize("command", ["uvx", "nodemon", "npx-helper", _abs("opt", "node", "npx")])
def test_any_other_missing_command_keeps_the_plain_text(command):
    """Only the bare Node.js program names get the hint; a path names its own location."""
    from kiro_crew.mcp_discovery import _unresolved_error

    assert _unresolved_error(command, BASE_PATH) == (
        f"command not found: {command} — not in any of the 2 directories searched "
        "(see the gateway log for the list)"
    )


# ── the operator-facing log line ──────────────────────────────────────────────


def test_probe_warning_lists_the_searched_directories(caplog):
    from kiro_crew import mcp_discovery

    mcp_discovery._unresolvable_warned.clear()
    with caplog.at_level("WARNING"):
        mcp_discovery._warn_unresolvable_once("srv", "ghost", BASE_PATH)
    assert USR_BIN in caplog.text
    assert "ghost" in caplog.text


def test_probe_warning_repeats_are_still_demoted(caplog):
    """The once-only ledger must survive the added argument."""
    from kiro_crew import mcp_discovery

    mcp_discovery._unresolvable_warned.clear()
    with caplog.at_level("WARNING"):
        mcp_discovery._warn_unresolvable_once("srv", "ghost", BASE_PATH)
        assert "command not found" in caplog.text  # first sighting warns
        caplog.clear()
        mcp_discovery._warn_unresolvable_once("srv", "ghost", BASE_PATH)
        assert "command not found" not in caplog.text  # repeat demoted to DEBUG
