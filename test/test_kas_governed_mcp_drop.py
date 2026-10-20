"""A governed MCP profile that drops Crew's managed servers must not loop the session.

On an enterprise MCP profile the KAS host filters ``kirocrew-core`` and
``kirocrew-cron`` out before it starts them, and then never names them in any
``_kiro/mcp/status`` snapshot. A readiness timeout there kills the runtime, the
retry meets the same filter, and the chat stays on "Thinking..." forever. These
tests pin both paths: with registry mode declared the session starts after a short
settle and names the servers as not started, every other guard intact; without
it the wait and timeout stand, and a host that never named the servers adds a
pointer to ``kirocrew doctor``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.acp import session_handle_mcp
from kiro_crew.acp.mcp_session_report import KasMcpReadiness, McpSessionReport
from kiro_crew.acp.runtime_session_load import _declared_governed
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_mcp import GOVERNED_DROP_REASON
from kiro_crew.acp.session_handle_runtime_contract import (
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
)
from kiro_crew.acp.types import (
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    METHOD_KAS_TOOLS_CHANGED,
    JsonRpcMessage,
)

_SID = "sess-governed"
_REQUIRED = ("kirocrew-core", "kirocrew-cron")
_CLIENT = {"kiro": {"resource": {"source": {"origin": "client"}}}}
_GLOBAL = {"kiro": {"resource": {"source": {"origin": "global"}}}}


def _status(*servers: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": list(servers)}
    )


def _entry(name: str, status: str, meta: dict | None = _CLIENT) -> dict:
    entry: dict = {"name": name, "status": status}
    if status == "connected":
        entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
    if meta is not None:
        entry["_meta"] = meta
    return entry


def _handle(*frames: JsonRpcMessage) -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    return handle


class TestNeverReported:
    def test_nothing_named_is_never_reported(self):
        ready = KasMcpReadiness(_SID, _REQUIRED)
        # The governed host's snapshot: other servers, none of Crew's.
        ready.record(_status(_entry("admin-catalog-server", "connected")))
        assert ready.never_reported == _REQUIRED

    def test_a_connecting_server_is_not_absent(self):
        ready = KasMcpReadiness(_SID, _REQUIRED)
        ready.record(_status(_entry("kirocrew-core", "connecting")))
        assert ready.never_reported == ()

    def test_a_foreign_same_named_server_is_not_absent(self):
        # The origin filter skips it, so its state stays "unreported" -- and it
        # must still never pass for absent, or a global server carrying another
        # identity would mount under Crew's name.
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(
            _status(_entry("kirocrew-core", "connected", _GLOBAL), _entry("x", "connected"))
        )
        assert ready.pending == "kirocrew-core: unreported"
        assert ready.never_reported == ()

    def test_a_tag_alone_marks_the_server_seen(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(
            JsonRpcMessage(
                method=METHOD_KAS_TOOLS_CHANGED,
                params={"sessionId": _SID, "tags": [{"source": "mcp", "tag": "@kirocrew-core/x"}]},
            )
        )
        assert ready.never_reported == ()

    def test_seen_survives_a_later_snapshot_that_drops_the_server(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_entry("kirocrew-core", "connecting")))
        ready.record(_status())
        assert ready.never_reported == ()

    def test_one_seen_pending_server_keeps_the_barrier_for_all(self):
        ready = KasMcpReadiness(_SID, _REQUIRED)
        ready.record(_status(_entry("kirocrew-cron", "connecting")))
        assert ready.never_reported == ()

    def test_another_sessions_frame_does_not_count(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(
            JsonRpcMessage(
                method=METHOD_KAS_MCP_STATUS,
                params={"sessionId": "other", "servers": [_entry("kirocrew-core", "connecting")]},
            )
        )
        assert ready.never_reported == ("kirocrew-core",)


class TestReadinessTimeoutMessage:
    """Registry mode off: the wait and its timeout stand; silence adds a doctor pointer."""

    @pytest.mark.asyncio
    async def test_servers_the_host_never_named_point_at_the_doctor(self):
        handle = _handle(_status(_entry("admin-catalog-server", "connected")))
        with pytest.raises(AcpRequestTimeout, match="kirocrew doctor") as raised:
            await handle.wait_mcp_ready(_REQUIRED, 0.2)
        # The setting withholds every agent-spec server, so silence never advises it.
        assert "mcp_registry_mode" not in str(raised.value)
        assert handle._mcp_report._failed == []

    @pytest.mark.asyncio
    async def test_a_server_still_connecting_times_out_without_the_pointer(self):
        handle = _handle(_status(_entry("kirocrew-core", "connecting")))
        with pytest.raises(AcpRequestTimeout, match="kirocrew-core: connecting") as raised:
            await handle.wait_mcp_ready(("kirocrew-core",), 0.2)
        assert "kirocrew doctor" not in str(raised.value)

    @pytest.mark.asyncio
    async def test_a_foreign_same_named_server_times_out_without_the_pointer(self):
        handle = _handle(_status(_entry("kirocrew-core", "connected", _GLOBAL)))
        with pytest.raises(AcpRequestTimeout, match="kirocrew-core: unreported") as raised:
            await handle.wait_mcp_ready(("kirocrew-core",), 0.2)
        assert "kirocrew doctor" not in str(raised.value)


class TestGovernedStart:
    """Registry mode on: a short settle, then start without servers the host never named.

    Every other readiness guard still applies on this path.
    """

    @pytest.mark.asyncio
    async def test_never_named_servers_start_the_session_without_them(self):
        handle = _handle(_status(_entry("admin-catalog-server", "connected")))
        await handle.wait_mcp_ready(_REQUIRED, 0.2, governed=True)
        report = handle._mcp_report
        assert report._failed == list(_REQUIRED)
        assert all(report._failures[n] == GOVERNED_DROP_REASON for n in _REQUIRED)

    @pytest.mark.asyncio
    async def test_a_ready_server_is_kept_when_only_the_other_is_absent(self):
        handle = _handle(_status(_entry("kirocrew-cron", "connected")))
        await handle.wait_mcp_ready(_REQUIRED, 0.2, governed=True)
        assert handle._mcp_report._failed == ["kirocrew-core"]
        assert "kirocrew-cron" in handle._mcp_report._ready

    @pytest.mark.asyncio
    async def test_a_foreign_same_named_server_still_times_out(self):
        handle = _handle(_status(_entry("kirocrew-core", "connected", _GLOBAL)))
        with pytest.raises(AcpRequestTimeout, match="kirocrew-core: unreported"):
            await handle.wait_mcp_ready(("kirocrew-core",), 0.2, governed=True)
        assert handle._mcp_report._failed == []

    @pytest.mark.asyncio
    async def test_a_server_still_connecting_still_times_out(self):
        handle = _handle(_status(_entry("kirocrew-core", "connecting")))
        with pytest.raises(AcpRequestTimeout, match="kirocrew-core: connecting"):
            await handle.wait_mcp_ready(("kirocrew-core",), 0.2, governed=True)

    @pytest.mark.asyncio
    async def test_never_named_at_the_settle_starts_before_the_deadline(self, monkeypatch):
        # A 30s budget the test's own --timeout would kill: only the settle ends it.
        monkeypatch.setattr(session_handle_mcp, "GOVERNED_SETTLE_SECS", 0.1)
        handle = _handle()
        await handle.wait_mcp_ready(("kirocrew-core",), 30.0, governed=True)
        assert handle._mcp_report._failed == ["kirocrew-core"]

    @pytest.mark.asyncio
    async def test_a_named_server_keeps_the_full_budget_past_the_settle(self, monkeypatch):
        # A cold server the host named before the settle is not cut short.
        monkeypatch.setattr(session_handle_mcp, "GOVERNED_SETTLE_SECS", 0.1)
        handle = _handle(_status(_entry("kirocrew-core", "connecting")))
        loop = asyncio.get_running_loop()
        loop.call_later(
            0.3, handle._queue.put_nowait, _status(_entry("kirocrew-core", "connected"))
        )
        await handle.wait_mcp_ready(("kirocrew-core",), 30.0, governed=True)
        assert handle._mcp_report._failed == []
        assert "kirocrew-core" in handle._mcp_report._ready

    @pytest.mark.asyncio
    async def test_a_failed_server_still_fails(self):
        handle = _handle(_status(_entry("kirocrew-core", "failed")))
        with pytest.raises(AcpRuntimeError, match="kirocrew-core: failed"):
            await handle.wait_mcp_ready(("kirocrew-core",), 0.2, governed=True)

    @pytest.mark.asyncio
    async def test_runtime_death_still_raises(self):
        handle = _handle(None)
        with pytest.raises(AcpRuntimeDead):
            await handle.wait_mcp_ready(_REQUIRED, 0.2, governed=True)

    @pytest.mark.asyncio
    async def test_a_later_frame_still_moves_a_dropped_server_to_ready(self):
        handle = _handle()
        await handle.wait_mcp_ready(("kirocrew-core",), 0.2, governed=True)
        handle._mcp_report.record_event(EVENT_MCP_SERVER_INITIALIZED, "kirocrew-core")
        assert handle._mcp_report._failed == []
        assert "kirocrew-core" in handle._mcp_report._ready


class TestDeclaredGoverned:
    """Readiness reads an unreadable declaration as NOT governed.

    The spec-server ceiling reads it as governed, the safe way to withhold. Here
    governed loosens readiness, so the safe reading is the opposite one.
    """

    @staticmethod
    def _load(monkeypatch, *, mode=True, base_bad=False, overlay_bad=False, boom=False):
        from kiro_crew.config.loader import KiroCrewConfig

        def load(*_a, **_k):
            if boom:
                raise RuntimeError("config plane down")
            return SimpleNamespace(
                agent=SimpleNamespace(mcp_registry_mode=mode),
                _base_unreadable=base_bad,
                _overlay_unreadable=overlay_bad,
            )

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(load))

    def test_a_failed_load_is_not_governed(self, monkeypatch):
        self._load(monkeypatch, boom=True)
        assert _declared_governed() is False

    @pytest.mark.parametrize("bad", ["base_bad", "overlay_bad"])
    def test_a_half_read_config_is_not_governed(self, monkeypatch, bad):
        self._load(monkeypatch, **{bad: True})
        assert _declared_governed() is False

    @pytest.mark.parametrize("mode", [True, False])
    def test_a_readable_declaration_is_passed_through(self, monkeypatch, mode):
        self._load(monkeypatch, mode=mode)
        assert _declared_governed() is mode
