"""An unresolvable MCP command must be reported once, not every probe cycle.

The contract this pins: a command that does not resolve is a STABLE fact, so it
warns the first time and drops to DEBUG afterwards; a command that resolves
later (or a different command under the same server name) is news again; and
clearing the ledger restores first-warn behaviour. Transient failure classes
(timeouts, handshake errors) are deliberately NOT routed through the ledger and
keep warning on every occurrence.
"""

import asyncio
import logging
from unittest import mock

import pytest

from kiro_crew import mcp_discovery


@pytest.fixture(autouse=True)
def _clean_ledger():
    """Each test starts and ends with an empty ledger (it is module state)."""
    mcp_discovery.reset_unresolvable_warnings()
    yield
    mcp_discovery.reset_unresolvable_warnings()


def _levels_for(caplog, needle):
    return [r.levelno for r in caplog.records if needle in r.getMessage()]


def test_first_unresolvable_command_warns(caplog):
    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        mcp_discovery._warn_unresolvable_once("slack-mcp", "slack-mcp")
    assert _levels_for(caplog, "slack-mcp") == [logging.WARNING]


def test_repeat_of_the_same_command_drops_to_debug(caplog):
    # The reported symptom: a config carried from another machine re-emitted the
    # same handful of warnings on every discovery cycle, burying real ones.
    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        for _ in range(5):
            mcp_discovery._warn_unresolvable_once("slack-mcp", "slack-mcp")
    levels = _levels_for(caplog, "slack-mcp")
    assert levels[0] == logging.WARNING
    assert set(levels[1:]) == {logging.DEBUG}
    assert levels.count(logging.WARNING) == 1


def test_a_different_command_under_the_same_name_warns_again(caplog):
    # Keyed on (name, command): editing the config to a new binary that is ALSO
    # missing is a new fact and must not be swallowed by the old entry.
    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        mcp_discovery._warn_unresolvable_once("pw", "/usr/bin/playwright-mcp")
        mcp_discovery._warn_unresolvable_once("pw", "/opt/bin/playwright-mcp")
    assert _levels_for(caplog, "/usr/bin/playwright-mcp") == [logging.WARNING]
    assert _levels_for(caplog, "/opt/bin/playwright-mcp") == [logging.WARNING]


def test_distinct_servers_each_warn_once(caplog):
    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        mcp_discovery._warn_unresolvable_once("a", "cmd-a")
        mcp_discovery._warn_unresolvable_once("b", "cmd-b")
        mcp_discovery._warn_unresolvable_once("a", "cmd-a")
    assert _levels_for(caplog, "cmd-a") == [logging.WARNING, logging.DEBUG]
    assert _levels_for(caplog, "cmd-b") == [logging.WARNING]


def test_reset_restores_first_warn_behaviour(caplog):
    # Callers reset after a config change so a regression is reported afresh
    # rather than silently swallowed for the life of the process.
    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        mcp_discovery._warn_unresolvable_once("x", "cmd-x")
        mcp_discovery.reset_unresolvable_warnings()
        mcp_discovery._warn_unresolvable_once("x", "cmd-x")
    assert _levels_for(caplog, "cmd-x") == [logging.WARNING, logging.WARNING]


@pytest.mark.asyncio
async def test_malformed_command_does_not_abort_the_whole_probe_pass(caplog):
    """A malformed config entry must fail alone, not take down bulk probing.

    `command` is read straight from config JSON (`spec.get("command", "")`)
    with no validation, so it can be a dict or list. Those are unhashable, and
    the ledger prune runs OUTSIDE the per-server `gather` — so without a guard
    one bad entry raises `TypeError` out of `probe_all()` and `/api/mcp/probe`
    returns 500 with nothing probed at all.
    """
    bad = mcp_discovery.McpServerInfo(name="malformed", command={"not": "a string"})  # type: ignore[arg-type]
    good = mcp_discovery.McpServerInfo(
        name="ghost", command="kirocrew-no-such-binary-2f8a1c", args=[]
    )

    with mock.patch.object(mcp_discovery, "list_servers", return_value=[bad, good]):
        with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
            results = await mcp_discovery.probe_all()

    # Every server still gets a verdict — the malformed one included.
    assert {r.name for r in results} == {"malformed", "ghost"}
    by_name = {r.name: r for r in results}
    assert by_name["ghost"].status == "error"
    assert "command not found" in (by_name["ghost"].error or "")
    # The malformed entry fails visibly in isolation rather than silently.
    assert by_name["malformed"].status == "error"
    assert by_name["malformed"].error, "a malformed entry must still report an error"


@pytest.mark.asyncio
async def test_a_resolving_command_clears_the_ledger_even_if_the_handshake_fails():
    """The ledger tracks RESOLVABILITY, not handshake health.

    Without this, a server whose binary was missing, then got installed but
    failed its handshake, would keep a stale key — so when the binary is
    removed again the user sees only DEBUG, never the WARNING.
    """
    server = mcp_discovery.McpServerInfo(name="flaky", command="flaky-bin", args=[])
    mcp_discovery._warn_unresolvable_once("flaky", "flaky-bin")
    assert ("flaky", "flaky-bin") in mcp_discovery._unresolvable_warned

    # Resolves fine, then dies during the handshake — the timeout path.
    with mock.patch.object(mcp_discovery.shutil, "which", return_value="/usr/bin/flaky-bin"):
        with mock.patch.object(
            mcp_discovery, "sandboxed_spawn_argv", side_effect=asyncio.TimeoutError()
        ):
            result = await mcp_discovery.probe_server(server)

    assert result.status == "error"
    assert ("flaky", "flaky-bin") not in mcp_discovery._unresolvable_warned, (
        "a command that resolved must leave no stale key, even when the probe then failed"
    )


def test_ledger_is_bounded_by_config_size_not_cycle_count():
    # Repeated cycles over the same servers must not grow the ledger — that is
    # what makes it safe to keep for the life of the process.
    for _ in range(50):
        for name in ("s1", "s2", "s3"):
            mcp_discovery._warn_unresolvable_once(name, f"cmd-{name}")
    assert len(mcp_discovery._unresolvable_warned) == 3


def test_a_command_that_resolves_clears_its_entry():
    # missing -> installed -> missing must warn again on the second outage,
    # not stay silent for the life of the process.
    mcp_discovery._warn_unresolvable_once("srv", "cmd")
    assert ("srv", "cmd") in mcp_discovery._unresolvable_warned
    mcp_discovery._clear_unresolvable("srv", "cmd")
    assert ("srv", "cmd") not in mcp_discovery._unresolvable_warned


def test_prune_drops_keys_the_config_no_longer_names():
    # Editing a command to another missing binary must not retain the old
    # string, otherwise the ledger grows with config churn.
    mcp_discovery._warn_unresolvable_once("srv", "/old/bin")
    mcp_discovery._warn_unresolvable_once("keep", "/keep/bin")
    mcp_discovery._prune_unresolvable({("srv", "/new/bin"), ("keep", "/keep/bin")})
    assert mcp_discovery._unresolvable_warned == {("keep", "/keep/bin")}


def test_clearing_an_absent_key_is_a_no_op():
    mcp_discovery._clear_unresolvable("never", "seen")  # must not raise
    assert mcp_discovery._unresolvable_warned == set()


@pytest.mark.asyncio
async def test_probe_server_warns_once_across_cycles(caplog):
    """End-to-end through probe_server: the real dedup path, not a source grep.

    Uses a command that genuinely does not resolve, so this exercises the
    `shutil.which` pre-check and its early return.
    """
    server = mcp_discovery.McpServerInfo(
        name="ghost", command="kirocrew-no-such-binary-2f8a1c", args=[]
    )
    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        for _ in range(3):
            result = await mcp_discovery.probe_server(server)
    # The machine-readable failure state is unchanged by the dedup — this is
    # what doctor and the dashboard actually read.
    assert result.status == "error"
    assert "command not found" in (result.error or "")
    levels = _levels_for(caplog, "kirocrew-no-such-binary-2f8a1c")
    assert levels.count(logging.WARNING) == 1, f"expected exactly one WARNING, got {levels}"


@pytest.mark.asyncio
async def test_timeouts_still_warn_on_every_cycle(caplog):
    """The scope line, asserted behaviourally: transient failures are NOT deduped.

    A server that exists but hangs must warn every cycle — a newly-hanging
    server is news, unlike a permanently absent binary. The timeout is injected
    at the spawn boundary, which lands in the same `except asyncio.TimeoutError`
    handler that a real readline timeout reaches.
    """
    server = mcp_discovery.McpServerInfo(name="hanger", command="sleep", args=["999"])

    with caplog.at_level(logging.DEBUG, logger=mcp_discovery.logger.name):
        with mock.patch.object(mcp_discovery.shutil, "which", return_value="/bin/sleep"):
            with mock.patch.object(
                mcp_discovery,
                "sandboxed_spawn_argv",
                side_effect=asyncio.TimeoutError(),
            ):
                for _ in range(3):
                    result = await mcp_discovery.probe_server(server)

    assert result.status == "error"
    assert result.error == "timeout"
    timeout_warnings = [
        r for r in caplog.records if "timeout" in r.getMessage() and r.levelno == logging.WARNING
    ]
    assert len(timeout_warnings) == 3, f"timeouts must not be deduped, got {len(timeout_warnings)}"
    assert mcp_discovery._unresolvable_warned == set(), "timeouts must not enter the ledger"
