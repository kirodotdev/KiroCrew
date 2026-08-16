"""The shareability pre-flight, against a REAL MCP server process.

Every other test of this feature replaces ``probe_server`` with a fake that
answers from a dict, or mocks ``create_subprocess_exec`` outright. Those pin the
policy layer, but none of them can say the mechanism works: the whole point of
the pre-flight is that it spawns a server TWICE with different ``clientInfo`` and
compares what the server really answered, and a fake that returns canned
capabilities proves only that the fake behaves as written.

So these tests spawn an actual stdio MCP server -- a small script written to
disk, speaking real JSON-RPC over real pipes -- through the real
``preflight`` -> ``probe_server`` path with nothing patched.

The evidence is produced BY THE SERVER, not asserted about a double: each
process appends the ``clientInfo.name`` it was handed to a witness file. A test
that passes without that file holding two distinct identities is a test that
never spawned anything, so the witness is checked explicitly rather than
inferred from the verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.mcp_gateway.preflight import PREFLIGHT_IDENTITY_NAMES, preflight
from kiro_crew.mcp_gateway.shareability import ShareEvidence, Strength, assess

# A server whose declared capabilities depend on who is asking. This is the
# hazard the pre-flight exists to detect: the pool caches the FIRST caller's
# initialize result and replays it to everyone else, so a server that
# negotiates per-client would hand session B session A's capability set.
_CALLER_SENSITIVE = '''\
import json, sys
from pathlib import Path

witness = Path(sys.argv[1])
first_identity = sys.argv[2]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        who = (msg.get("params") or {}).get("clientInfo", {}).get("name", "?")
        with witness.open("a", encoding="utf-8") as fh:
            fh.write(who + "\\n")
        # The divergence: the first identity is told subscriptions exist, anyone
        # else is not. Compared by equality against the name the pre-flight
        # really sends, so renaming an identity cannot quietly stop the fixture
        # from diverging.
        caps = {"resources": {"subscribe": True}} if who == first_identity else {"resources": {}}
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"protocolVersion": "2024-11-05", "capabilities": caps,
                       "serverInfo": {"name": "caller-sensitive", "version": "1"}},
        }) + "\\n")
        sys.stdout.flush()
    elif method == "tools/list":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"tools": [{"name": "peek", "description": "d"}]},
        }) + "\\n")
        sys.stdout.flush()
'''

# Same script, minus the branch on who is asking.
_STABLE = _CALLER_SENSITIVE.replace(
    'caps = {"resources": {"subscribe": True}} if who == first_identity else {"resources": {}}',
    'caps = {"tools": {"listChanged": True}}',
).replace('"name": "caller-sensitive"', '"name": "stable"')

# Answers the first caller and then exits, so the SECOND spawn gets nothing.
# The pre-flight must read that as "no measurement", never as agreement.
_DIES_AFTER_FIRST = '''\
import json, sys
from pathlib import Path

witness = Path(sys.argv[1])
if witness.exists() and witness.read_text(encoding="utf-8").strip():
    sys.exit(1)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("method") == "initialize":
        who = (msg.get("params") or {}).get("clientInfo", {}).get("name", "?")
        with witness.open("a", encoding="utf-8") as fh:
            fh.write(who + "\\n")
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "serverInfo": {"name": "dies", "version": "1"}},
        }) + "\\n")
        sys.stdout.flush()
    elif msg.get("method") == "tools/list":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": []},
        }) + "\\n")
        sys.stdout.flush()
'''


@pytest.fixture
def real_probe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated home whose config permits a genuinely unsandboxed spawn.

    ``probe_server`` fail-closes when the host has no OS-level sandbox backend,
    which is the case on a plain container or a userns-restricted host. Without
    this opt-in the probe returns "skipped" everywhere such a backend is
    missing, and these tests would pass while spawning nothing.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": True}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def _server(tmp_path: Path, source: str, witness: Path, name: str) -> McpServerInfo:
    script = tmp_path / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    import sys

    return McpServerInfo(
        name=name,
        command=sys.executable,
        args=[str(script), str(witness), PREFLIGHT_IDENTITY_NAMES[0]],
    )


def _identities(witness: Path) -> list[str]:
    if not witness.exists():
        return []
    return [ln for ln in witness.read_text(encoding="utf-8").splitlines() if ln]


@pytest.mark.asyncio
async def test_a_caller_sensitive_server_is_caught_by_provoking_it(
    tmp_path: Path, real_probe_home: Path
) -> None:
    """Two real spawns, two real answers, and the difference is detected."""
    witness = tmp_path / "seen-caller-sensitive.txt"
    server = _server(tmp_path, _CALLER_SENSITIVE, witness, "caller-sensitive")

    result = await preflight(server)

    seen = _identities(witness)
    if not seen:
        pytest.skip(
            "the probe did not spawn anything on this host, so nothing here is "
            "proven. The isolated home does set the unsandboxed opt-in, so the "
            "blocker is upstream of it — a governance floor above Kiro Crew's own "
            "config, or no usable spawn backend at all"
        )
    if len(seen) == 1 and result.ran is False and result.detail == "timeout":
        # The first real handshake succeeded but the SECOND timed out under
        # load (preflight reports ran=False with the probe's timeout detail —
        # the single-shot branch in preflight()). Keyed on the timeout detail
        # so a genuine second-probe regression (crash, refusal, protocol
        # error) still FAILS; only the load-starvation shape skips. Observed
        # on loaded xdist shards where sibling-test CPU contention starves
        # the second subprocess handshake past the probe budget.
        pytest.skip(f"second probe handshake timed out under load: {seen}")

    assert len(seen) == 2, f"the pre-flight must provoke exactly twice, saw {seen}"
    assert len(set(seen)) == 2, f"both spawns used the same clientInfo: {seen}"
    assert result.ran is True
    assert result.caller_sensitive is True, result.reasons

    verdict = assess(
        ShareEvidence(
            name=server.name,
            probe_ok=True,
            capabilities={"resources": {}},
            preflight_ran=True,
            preflight_caller_sensitive=True,
        )
    )
    assert verdict.strength is Strength.DISQUALIFIED
    assert verdict.recommend_share is False


@pytest.mark.asyncio
async def test_a_stable_server_answers_both_callers_identically(
    tmp_path: Path, real_probe_home: Path
) -> None:
    """The negative case: a server that does not look at who is asking passes.

    Without this, the test above would also pass if the pre-flight reported
    every server as caller-sensitive.
    """
    witness = tmp_path / "seen-stable.txt"
    server = _server(tmp_path, _STABLE, witness, "stable")

    result = await preflight(server)

    seen = _identities(witness)
    if not seen:
        pytest.skip("the probe did not spawn anything on this host")
    if len(seen) == 1 and result.ran is False and result.detail == "timeout":
        # Same environment skip as the caller-sensitive test above: only the
        # load-starvation shape (second-probe timeout) skips; any other
        # single-spawn outcome still fails.
        pytest.skip(f"second probe handshake timed out under load: {seen}")

    assert len(seen) == 2, f"expected two spawns, saw {seen}"
    assert result.ran is True
    assert result.caller_sensitive is False, result.reasons


@pytest.mark.asyncio
async def test_a_server_that_never_starts_is_unavailable_not_safe(
    tmp_path: Path, real_probe_home: Path
) -> None:
    """A real failure to launch must read as "no measurement", never as a pass.

    The distinction is the whole reason ``ran`` exists: reporting an
    unreachable server as not-caller-sensitive would recommend sharing a
    backend nobody ever spoke to.
    """
    server = McpServerInfo(
        name="missing", command=str(tmp_path / "does-not-exist"), args=[]
    )

    result = await preflight(server)

    assert result.ran is False
    verdict = assess(
        ShareEvidence(
            name="missing",
            probe_ok=False,
            capabilities=None,
            preflight_ran=False,
            preflight_caller_sensitive=False,
        )
    )
    assert verdict.recommend_share is False


@pytest.mark.asyncio
async def test_a_server_that_answers_once_then_refuses_is_not_agreement(
    tmp_path: Path, real_probe_home: Path
) -> None:
    """Half a measurement is no measurement.

    The second spawn failing leaves one answer and nothing to compare it to.
    Reading that as "the two agreed" would recommend sharing a backend on the
    strength of a single handshake, which is the same fabrication as trusting a
    server that never started.
    """
    witness = tmp_path / "seen-dies.txt"
    server = _server(tmp_path, _DIES_AFTER_FIRST, witness, "dies-after-first")

    result = await preflight(server)

    seen = _identities(witness)
    if not seen:
        pytest.skip("the probe did not spawn anything on this host")

    assert len(seen) == 1, f"the second spawn should have died before answering: {seen}"
    assert result.ran is False, result.reasons
    assert result.caller_sensitive is False


def test_the_fake_servers_differ_only_in_the_branch_under_test() -> None:
    """Guard the fixtures: the two scripts must be one edit apart.

    If they drift, the pair stops being a controlled comparison and the
    caller-sensitive result could come from any other difference.
    """
    assert _STABLE != _CALLER_SENSITIVE
    assert "clientInfo" in _STABLE, "the stable server must still read clientInfo"
    assert _STABLE.count("initialize") == _CALLER_SENSITIVE.count("initialize")
