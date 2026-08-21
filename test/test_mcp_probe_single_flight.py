"""The MCP startup probe must fan out ONCE per boot.

Two independent boot paths reach ``_bg_mcp_probe``: ``dashboard/server.py`` fires
it as a background task once the port is bound, and ``slack/gateway.py`` awaits it
before warming sessions. Nothing joined them, so boot spawned and handshaked
every enabled MCP server twice.

``_mcp_probe_in_progress`` could not close this: it was written but never read at
entry, and a bool cannot be awaited, so the second caller had nothing to wait on.
These tests pin the joinable task handle that does.
"""

import asyncio

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod


class _FakeServer:
    """Minimal stand-in for a probed server row."""

    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name, "status": "ok"}


@pytest.fixture
def probe_env(monkeypatch, tmp_path):
    """Isolate every module global the probe reads or writes."""
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
    monkeypatch.setattr(mcp_mod, "_mcp_probe_task", None)
    # No global mcp.json — the probe tolerates its absence, and pointing at a
    # missing path keeps the test off the developer's real config.
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent" / "mcp.json")
    return tmp_path


def _install_probe_all(monkeypatch, *, delay: float, calls: list[int]):
    """Patch probe_all with a slow counter so overlap is observable."""

    async def _probe_all():
        calls.append(1)
        await asyncio.sleep(delay)
        return [_FakeServer("alpha"), _FakeServer("beta")]

    # The probe imports probe_all in-function (documented circular-import
    # workaround), so the patch must land on the source module.
    monkeypatch.setattr("kiro_crew.mcp_discovery.probe_all", _probe_all)


@pytest.mark.asyncio
async def test_two_concurrent_callers_produce_one_fan_out(monkeypatch, probe_env):
    """The exact boot shape: server.py's task and gateway's await, overlapping."""
    calls: list[int] = []
    _install_probe_all(monkeypatch, delay=0.05, calls=calls)

    await asyncio.gather(mcp_mod._bg_mcp_probe(), mcp_mod._bg_mcp_probe())

    assert len(calls) == 1, f"probe fanned out {len(calls)}× for one boot"
    # Both callers must still observe a populated cache — joining is only
    # correct if the second caller gets the same guarantee it had when it ran
    # its own probe.
    assert [row["name"] for row in mcp_mod._mcp_probe_cache] == ["alpha", "beta"]
    assert mcp_mod._mcp_probe_ts > 0


@pytest.mark.asyncio
async def test_second_caller_waits_rather_than_returning_early(monkeypatch, probe_env):
    """A joining caller must not return before the cache is populated."""
    calls: list[int] = []
    _install_probe_all(monkeypatch, delay=0.05, calls=calls)

    first = asyncio.ensure_future(mcp_mod._bg_mcp_probe())
    await asyncio.sleep(0)  # let the first caller register the in-flight task

    # Joining caller: the cache is still empty at this point.
    assert mcp_mod._mcp_probe_cache == []
    await mcp_mod._bg_mcp_probe()
    assert mcp_mod._mcp_probe_cache != [], "joined caller returned before the probe landed"

    await first
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_probe_runs_again_after_the_previous_one_completed(monkeypatch, probe_env):
    """Single-flight must not latch — a later re-probe still works.

    The request handlers (`api_mcp_servers`, `api_mcp_probe_cached`) schedule a
    re-probe when the cache goes stale; a guard that never released would leave
    the dashboard showing "Outdated" forever.
    """
    calls: list[int] = []
    _install_probe_all(monkeypatch, delay=0, calls=calls)

    await mcp_mod._bg_mcp_probe()
    await mcp_mod._bg_mcp_probe()

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_caller_timing_out_does_not_cancel_the_probe(monkeypatch, probe_env):
    """gateway wraps the probe in wait_for; a timeout is the caller giving up.

    It must abandon its own wait, not kill the fan-out mid-handshake — the boot
    path's "continuing without full probe" message already implies the probe is
    still coming, and cancelling it means the cache stays empty until the first
    request pays for a fresh probe.
    """
    calls: list[int] = []
    _install_probe_all(monkeypatch, delay=0.1, calls=calls)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mcp_mod._bg_mcp_probe(), timeout=0.01)

    inflight = mcp_mod._mcp_probe_task
    assert inflight is not None
    assert not inflight.cancelled(), "the caller's timeout cancelled the shared probe"

    await inflight
    assert [row["name"] for row in mcp_mod._mcp_probe_cache] == ["alpha", "beta"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_joining_caller_timing_out_does_not_cancel_the_probe(monkeypatch, probe_env):
    """The JOIN path needs the same shield as the owner path.

    Distinct from the owner case above: here the probe belongs to someone else,
    so an unshielded join lets a second caller's timeout cancel a fan-out it does
    not own — the worse of the two, because the owner is still waiting on it.
    """
    calls: list[int] = []
    _install_probe_all(monkeypatch, delay=0.1, calls=calls)

    owner = asyncio.ensure_future(mcp_mod._bg_mcp_probe())
    await asyncio.sleep(0)  # owner registers the in-flight task

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mcp_mod._bg_mcp_probe(), timeout=0.01)

    inflight = mcp_mod._mcp_probe_task
    assert inflight is not None
    assert not inflight.cancelled(), "a joining caller's timeout cancelled the shared probe"

    await owner
    assert [row["name"] for row in mcp_mod._mcp_probe_cache] == ["alpha", "beta"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_in_progress_flag_is_cleared_when_the_probe_finishes(monkeypatch, probe_env):
    """The flag the request handlers read must still settle to False.

    They consult it to avoid STACKING a re-probe; leaving it stuck True would
    suppress every future refresh.
    """
    calls: list[int] = []
    _install_probe_all(monkeypatch, delay=0, calls=calls)

    await mcp_mod._bg_mcp_probe()

    assert mcp_mod._mcp_probe_in_progress is False


@pytest.mark.asyncio
async def test_a_failing_probe_does_not_wedge_later_probes(monkeypatch, probe_env):
    """A raising probe_all must leave the guard releasable."""
    calls: list[int] = []

    async def _boom():
        calls.append(1)
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("kiro_crew.mcp_discovery.probe_all", _boom)

    # _run_mcp_probe swallows and logs — a boot path must not die on this.
    await mcp_mod._bg_mcp_probe()
    assert mcp_mod._mcp_probe_in_progress is False

    _install_probe_all(monkeypatch, delay=0, calls=calls)
    await mcp_mod._bg_mcp_probe()
    assert len(calls) == 2
    assert mcp_mod._mcp_probe_cache != []
