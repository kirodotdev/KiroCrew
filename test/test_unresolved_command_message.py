"""``command not found`` must say WHERE it looked (part of #3030).

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


# ── a directory-qualified command searched no directories at all ──────────────
#
# ``shutil.which`` looks a command carrying a directory component up DIRECTLY
# and never consults ``path``: its first branch is
# ``if os.path.dirname(cmd): ... return None``. So an absolute (or ``./``
# relative) command that fails to resolve was checked at exactly ONE location.
# Reporting the spec's PATH for it inverts the distinction these messages exist
# to draw -- "this exact path does not exist" is rendered as "not found in any
# of the N directories searched", pointing the reader at a search that never
# happened.


ABSENT_ABS = _abs("opt", "vendor", "bin", "ghost")


def test_shutil_which_really_ignores_path_for_a_qualified_command():
    """The premise, pinned. Everything below rests on it."""
    import shutil

    assert os.path.dirname(ABSENT_ABS)
    assert shutil.which(ABSENT_ABS, path=BASE_PATH) is None


def test_probe_error_for_an_absolute_command_claims_no_directories():
    from kiro_crew.mcp_discovery import _unresolved_error

    assert _unresolved_error(ABSENT_ABS, "") == "command not found: %s" % ABSENT_ABS
    assert "directories" not in _unresolved_error(ABSENT_ABS, "")


# ── the dashboard probe ───────────────────────────────────────────────────────


def _probe(monkeypatch, command: str):
    """Drive the real ``probe_server`` and capture what it reported.

    Asserted at the reporting boundary rather than on an internal local: the
    defect is what the reader is told, and ``probe_server`` reaches the same two
    helpers from two different exits.
    """
    import asyncio

    from kiro_crew import mcp_discovery

    seen: list[tuple[str, str]] = []

    def _record(name, cmd, search_path=""):
        seen.append((cmd, search_path))

    monkeypatch.setattr(mcp_discovery, "_warn_unresolvable_once", _record)
    monkeypatch.setenv("PATH", BASE_PATH)
    server = mcp_discovery.McpServerInfo(name="vendor", command=command)
    result = asyncio.run(mcp_discovery.probe_server(server))
    return result, seen


def test_probe_reports_no_search_path_for_a_directory_qualified_command(monkeypatch):
    result, seen = _probe(monkeypatch, ABSENT_ABS)

    assert result.status == "error"
    assert seen, "the probe never reported the unresolvable command"
    command, search_path = seen[-1]
    assert command == ABSENT_ABS
    assert search_path == "", (
        "the probe told the reader it searched %r for a command shutil.which "
        "only ever checked at one location" % (search_path,)
    )
    assert "directories" not in (
        result.error or ""
    ), "the dashboard string still claims a directory search: %r" % (result.error,)


def test_probe_still_reports_the_search_path_for_a_bare_command(monkeypatch):
    """Preservation: #4954's behaviour is untouched where a search DID happen."""
    result, seen = _probe(monkeypatch, "ghost-bare-command")

    assert result.status == "error"
    assert seen
    _command, search_path = seen[-1]
    assert search_path, "a bare command must still name what it searched"
    assert "directories" in (result.error or "")


# ── the agent config rebuild ──────────────────────────────────────────────────


def _rebuild_with_command(tmp_path, monkeypatch, command: str, caplog):
    """Run the real ``rebuild_agent_config`` over one unresolvable server."""
    import json

    from kiro_crew.agent import rebuild_agent_config

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
    (agent_dir / "prompt.md").write_text("prompt")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings_dir = tmp_path / ".kiro" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"vendor": {"command": command, "env": {"PATH": BASE_PATH}}}})
    )

    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")

    with caplog.at_level("WARNING"):
        rebuild_agent_config()
    return caplog.text


def test_rebuild_drop_warning_names_no_directories_for_an_absolute_command(
    tmp_path, monkeypatch, caplog
):
    text = _rebuild_with_command(tmp_path, monkeypatch, ABSENT_ABS, caplog)

    assert "command not found" in text, "the server was not reported as dropped"
    assert USR_BIN not in text, (
        "the drop warning named a directory that shutil.which never consulted "
        "for a command carrying a directory component"
    )
    assert "directories searched" not in text
    # And not the wrong cause either: the PATH was fine, it was never consulted.
    assert (
        "empty PATH" not in text
    ), "the warning blamed an empty PATH for a command that was never " "PATH-searched: %r" % (
        text,
    )


def test_rebuild_drop_warning_still_names_directories_for_a_bare_command(
    tmp_path, monkeypatch, caplog
):
    """Preservation: the whole point of #4954 survives for bare commands."""
    text = _rebuild_with_command(tmp_path, monkeypatch, "ghost-bare-command", caplog)

    assert "command not found" in text
    assert USR_BIN in text, "a bare command must still name the directories searched"
