"""A start's memory price counts the MCP servers its session will start."""

from __future__ import annotations

import json

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.subagent as subagent
from kiro_crew import subagent_mcp_roster as roster


def _write_spec(tmp_path, servers: dict) -> None:
    (tmp_path / "kirocrew.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_counts_only_the_stdio_servers_a_session_launches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(agent_mod, "gateway_routed_names", lambda: frozenset({"pooled"}))
    _write_spec(
        tmp_path,
        {
            "one": {"command": "one-mcp"},
            "two": {"command": "two-mcp", "args": ["--x"]},
            "muted": {"command": "muted-mcp", "disabled": True},
            "remote": {"url": "https://example.invalid/mcp"},
            "pooled": {"command": "pooled-mcp"},
            "broken": "not an object",
        },
    )
    assert roster._unpooled_stdio_server_count() == 2


def test_an_unreadable_spec_prices_nothing_extra(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path / "absent")
    assert roster._unpooled_stdio_server_count() == 0


def test_the_default_roster_stays_under_the_unlearned_price() -> None:
    # Four of Kiro Crew's own servers: a default install is priced as before.
    assert roster.roster_gb_for(4) < subagent._UNLEARNED_DEDICATED_START_GB
    # 26 user servers plus four of Kiro Crew's own measured 4.3 GiB per session.
    assert roster.roster_gb_for(30) == pytest.approx(4.25)


def test_a_heavy_roster_raises_the_unlearned_and_the_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(subagent, "session_mcp_roster_gb", lambda: 4.25)
    assert subagent._dedicated_start_price_gb(0.5, {}, "") == pytest.approx(4.25)
    # A learned figure past the old 2 GiB ceiling is trusted up to the roster.
    assert subagent._dedicated_start_price_gb(0.5, {"kirocrew": 5.0}, "") == pytest.approx(4.25)
    # A learned figure below the roster is a measurement, and it wins.
    assert subagent._dedicated_start_price_gb(0.5, {"kirocrew": 1.2}, "") == pytest.approx(1.2)


def test_a_light_roster_prices_exactly_as_before(monkeypatch) -> None:
    monkeypatch.setattr(subagent, "session_mcp_roster_gb", lambda: roster.roster_gb_for(4))
    assert subagent._dedicated_start_price_gb(0.5, {}, "") == (
        subagent._UNLEARNED_DEDICATED_START_GB
    )
    assert subagent._dedicated_start_price_gb(0.5, {"kirocrew": 3.0}, "") == (
        subagent._SETTLED_PROJECTION_CEILING_GB
    )


def test_off_the_loop_the_roster_is_read_inline(monkeypatch) -> None:
    monkeypatch.setattr(roster, "_cache", None)
    monkeypatch.setattr(roster, "_unpooled_stdio_server_count", lambda: 30)
    assert roster.session_mcp_roster_gb() == pytest.approx(4.25)


def test_on_the_loop_the_roster_is_read_in_a_worker(monkeypatch) -> None:
    """The read walks the agents directory, so the event loop never waits on it."""
    import asyncio
    import threading

    monkeypatch.setattr(roster, "_cache", None)
    monkeypatch.setattr(roster, "_refresh_future", None)
    readers: list[int] = []
    release = threading.Event()

    def _count() -> int:
        readers.append(threading.get_ident())
        # Held until the loop side has captured the pending refresh, which the
        # worker clears when it finishes.
        assert release.wait(timeout=30), "the test never released the worker"
        return 30

    monkeypatch.setattr(roster, "_unpooled_stdio_server_count", _count)

    async def _drive() -> tuple[float, float, int]:
        first = roster.session_mcp_roster_gb()
        pending = roster._refresh_future
        release.set()
        assert pending is not None
        await asyncio.wait_for(pending, timeout=30)
        return first, roster.session_mcp_roster_gb(), threading.get_ident()

    first, second, loop_thread = asyncio.run(_drive())
    assert first == 0.0  # nothing read yet, so nothing extra is priced
    assert second == pytest.approx(4.25)
    assert readers and loop_thread not in readers
