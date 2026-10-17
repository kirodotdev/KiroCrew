"""Wire-contract tests for the MCP gateway's per-server sharing endpoints.

``GET /api/mcp-gateway/servers``, the single and batch
``POST /api/mcp-gateway/servers/stub`` writes, the stub-eligibility rule behind them,
the shareability verdict that refuses a probe taken before an edit, and the
synchronous cleanup-sweep file lock those writes share.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_mcp_coverage_helpers import agents_dir_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _effective_stubs, _payload, _request

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.testing.wait import LoopHop, async_wait_until, await_until, default_timeout

# ── GET /api/mcp-gateway/servers ────────────────────────────────────────


@pytest.fixture
def routed_allowlist(monkeypatch: pytest.MonkeyPatch):
    """Pin ``KiroCrewConfig.load().mcp_gateway.stub_servers``."""
    import kiro_crew.config.loader as loader

    def _set(
        names: list[str], *, enabled: bool = False, forward_declared_env: bool = False
    ) -> None:
        # ``socket_path`` is part of the real ``McpGatewayConfig`` and the
        # handler reads it to locate the observed-hazard ledger. A double that
        # omitted it would make the row builder raise on a field production
        # always has — empty is the honest stand-in for "no broker configured".
        #
        # ``enabled`` + ``forward_declared_env`` are read for the same reason:
        # together they decide whether stubbing a server could produce a SHARED
        # backend at all, which the row reports as ``pooling_blocked_by_env``.
        # Defaults match a fresh install (sharing off, no forwarding), so the
        # existing cases keep describing the state they were written for.
        cfg = SimpleNamespace(
            mcp_gateway=SimpleNamespace(
                stub_servers=list(names),
                socket_path="",
                enabled=enabled,
                forward_declared_env=forward_declared_env,
            )
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    return _set


def _seed_probe(monkeypatch, *names: str) -> None:
    """Make ``probe_metadata`` report a probed server that declares caller identity.

    Without this the verdict stops at "never probed" for every row, and a test of
    the preflight gate would pass whether or not the gate works.
    """
    meta = SimpleNamespace(
        status="ok",
        capabilities={"experimental": {"kirocrew.caller-identity": {}}},
        protocol_version="2024-11-05",
        tool_annotations=[],
        tools=[SimpleNamespace(name="t")],
    )
    monkeypatch.setattr(mcp_mod, "probe_metadata", lambda n: meta if n in names else None)


class TestGatewayServers:
    @pytest.mark.asyncio
    async def test_the_write_resolves_eligibility_against_the_state_it_writes_under(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The sharing state the rule reads is the one the write itself sees.

        A client filtering its own rows can only answer for the moment it read
        them, and sharing is a separate switch another dashboard or the CLI can
        flip. So the client sends candidates and the decision happens here, inside
        the lock hold that performs the write: a server whose evidence supports a
        stub but not co-tenancy is written while sharing is off and skipped once it
        is on, with no guard, expectation or retry in between.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "a.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": {"a-mcp": {"command": "run"}}}),
            encoding="utf-8",
        )
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
        # Patch the LOADER's name: the handler re-imports ``config_path`` from
        # ``kiro_crew.config.loader`` inside the function body, so patching this
        # module's copy is silently ignored.
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        # Supports a stub, does not support co-tenancy -- the case where the two
        # sharing states must disagree.
        from kiro_crew.mcp_gateway.shareability import ShareVerdict, Strength

        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: ShareVerdict(
                name=name,
                strength=Strength.MEASURED,
                recommend_stub=True,
                recommend_share=False,
                reasons=(),
            ),
        )

        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": False, "stub_servers": []}}))
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True, "resolve_eligibility": True})
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["stubbed"] == ["a-mcp"]
        assert body["skipped"] == []
        saved = json.loads(cfg_path.read_text())["mcp_gateway"]
        assert _effective_stubs(saved) == ["a-mcp"]
        # Recorded as a decision over the roster, which the write leaves alone.
        assert saved["stub_servers"] == []
        assert saved["stub_overrides"] == {"a-mcp": True}

        # Same request, same verdict, sharing now ON: the write must decline it and
        # say which name it declined, rather than co-tenanting on the weaker flag.
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True, "resolve_eligibility": True})
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["stubbed"] == []
        assert body["skipped"] == [{"name": "a-mcp", "reason": "evidence_insufficient"}]
        # Nothing qualified means the file is not rewritten at all.
        assert json.loads(cfg_path.read_text())["mcp_gateway"]["stub_servers"] == []

    @pytest.mark.asyncio
    async def test_the_overlay_decides_forward_declared_env_for_the_batch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``config.local.json`` wins, because the rewriter reads the merged value.

        Whether a declared key is forwarded decides whether a pooled spawn happens
        at all, so reading the base value while the rewriter reads the overlay would
        let the batch report a stub whose backend is left direct. The sharing switch
        beside it is already read this way.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "mcp_gateway": {
                        "enabled": True,
                        "stub_servers": [],
                        "forward_declared_env": True,
                    }
                }
            )
        )
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(
            mcp_mod, "_local_overlay_section", lambda: {"forward_declared_env": False}
        )

        seen: dict[str, bool] = {}

        def _record(names, *, sharing_on, forward_declared_env, probe_current=None):  # type: ignore[no-untyped-def]
            seen["forward"] = forward_declared_env
            return list(names), []

        monkeypatch.setattr(mcp_mod, "_stub_eligibility", _record)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True, "resolve_eligibility": True})
        )
        assert resp.status == 200
        assert (
            seen["forward"] is False
        ), "the batch must use the effective value the rewriter sees, not the base"

    @pytest.mark.asyncio
    async def test_unstubbing_never_asks_the_evidence_for_permission(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Removing a stub needs no verdict.

        Eligibility gates ADDING co-tenancy. Applying it to a removal would strand
        an operator with a stub they explicitly asked to drop, on the grounds that
        the evidence for keeping it had weakened.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": ["a-mcp"]}})
        )
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp"], "stub": False, "resolve_eligibility": True})
        )
        assert resp.status == 200
        assert _effective_stubs(json.loads(cfg_path.read_text())["mcp_gateway"]) == []

    @pytest.mark.asyncio
    async def test_a_single_toggle_writes_exactly_the_name_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Absent ``resolve_eligibility`` means "write exactly what I named".

        The per-row switch is a direct instruction about one server the operator is
        looking at, so it needs no verdict lookup; and an older dashboard served
        from a previous build sends no such field. Neither may be filtered.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        # Patch the LOADER's name: the handler re-imports ``config_path`` from
        # ``kiro_crew.config.loader`` inside the function body, so patching this
        # module's copy is silently ignored.
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(
            loader.KiroCrewConfig,
            "load",
            staticmethod(
                lambda: SimpleNamespace(
                    mcp_gateway=SimpleNamespace(
                        enabled=True, stub_servers=[], socket_path="", forward_declared_env=False
                    )
                )
            ),
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(_request({"name": "a-mcp", "stub": True}))
        assert resp.status == 200
        assert _effective_stubs(json.loads(cfg_path.read_text())["mcp_gateway"]) == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_the_stub_write_goes_through_the_locked_config_primitive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The stub write is only sound inside a CROSS-PROCESS lock.

        ``_MCP_GATEWAY_APPLY_LOCK`` and ``_get_config_lock`` are asyncio locks:
        they serialize this gateway's own handlers and say nothing about another
        process. The CLI writes ``mcp_gateway.enabled`` through the same file, so
        reading the sharing state outside the lock would let it change before the
        write -- and stub-only servers land in a pool that is now shared. The
        eligibility rule reads that state INSIDE this hold, which is what makes the
        decision and the write see one config.

        ``update_config_locked`` holds an advisory ``flock`` for the whole
        read-modify-write, and its own docstring names it the required path for new
        config.json mutations. This asserts the batch actually uses it rather than
        re-implementing the read-then-write it replaced; a regression to
        ``write_config_atomically`` would reopen the window silently, since every
        behavioural test still passes with the lock removed.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        seen: list[str] = []
        real = loader.update_config_locked

        def _recording_update(path=None, **kwargs):  # type: ignore[no-untyped-def]
            seen.append("locked")
            return real(path, **kwargs)

        monkeypatch.setattr(loader, "update_config_locked", _recording_update)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True, "resolve_eligibility": False})
        )
        assert resp.status == 200
        # Empty means the handler wrote config.json some other way, which is the
        # regression: a direct ``write_config_atomically`` passes every behavioural
        # test in this file while reopening the cross-process window.
        assert seen == ["locked"]
        assert _effective_stubs(json.loads(cfg_path.read_text())["mcp_gateway"]) == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_a_cancelled_request_still_waits_for_the_write_to_finish(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cancellation must not unwind the locks while the worker is mid-write.

        A worker thread cannot be cancelled. With a bare ``asyncio.to_thread`` the
        ``CancelledError`` propagates immediately, both locks unwind, and the thread
        keeps mutating config.json -- so an agent-CRUD save can interleave with a
        write the caller already gave up on, which is exactly the interleaving those
        locks exist to prevent.

        The property asserted is ORDERING, not merely that the file was written:
        the write lands either way, since nothing stops the thread. What
        distinguishes the two is whether the write had FINISHED by the time
        cancellation was observable.
        """
        import kiro_crew.config.loader as loader

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        entered = threading.Event()
        cancel_sent = threading.Event()
        finished = threading.Event()
        real = loader.update_config_locked
        hop = LoopHop(asyncio.get_running_loop())

        async def _one_loop_turn() -> None:
            return None

        def _slow_update(path=None, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            # Held until the cancellation below has been sent and the loop has run
            # the step that delivers it, so it lands while this write is running.
            assert cancel_sent.wait(timeout=default_timeout()), "the request was never cancelled"
            hop.run(_one_loop_turn(), "a loop turn after the cancel")
            try:
                return real(path, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(loader, "update_config_locked", _slow_update)

        task = asyncio.create_task(
            mcp_mod.api_mcp_gateway_set_stub(_request({"name": "a-mcp", "stub": True}))
        )
        try:
            await async_wait_until(entered.is_set, describe=lambda: f"task.done()={task.done()}")
            task.cancel()
        finally:
            cancel_sent.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled request")
        hop.check()

        assert finished.is_set(), (
            "cancellation was observable before the offloaded write completed, so "
            "both locks were released while the worker was still writing"
        )
        assert _effective_stubs(json.loads(cfg_path.read_text())["mcp_gateway"]) == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_the_stub_write_also_holds_the_lock_agent_crud_writes_under(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The file lock excludes other PROCESSES; it does not exclude this one.

        Agent CRUD saves config through ``cfg.save()`` ->
        ``write_config_atomically``, which takes no advisory file lock and
        serializes only on ``_get_config_lock``. So the two guards cover disjoint
        sets of writers: holding only the file lock leaves an in-process agent
        save free to interleave with this read-modify-write and silently drop one
        of the two changes.

        The offload is what makes this reachable -- ``await asyncio.to_thread``
        yields the event loop mid-write. Asserting the lock is HELD while the
        offloaded write runs is what a behavioural test cannot see: removing
        ``_get_config_lock`` leaves every stub assertion in this file passing.
        """
        import kiro_crew.config.loader as loader
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": []}}))
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)

        held_during_write: list[bool] = []
        real = loader.update_config_locked
        # ``_get_config_lock`` calls ``asyncio.get_running_loop()``, so it can only
        # be resolved here on the event loop -- the recorder below runs on a
        # ``to_thread`` worker. ``Lock.locked()`` needs no loop, so capture the
        # object now and only read its state from the thread.
        lock = _get_config_lock()

        def _checking_update(path=None, **kwargs):  # type: ignore[no-untyped-def]
            held_during_write.append(lock.locked())
            return real(path, **kwargs)

        monkeypatch.setattr(loader, "update_config_locked", _checking_update)

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True, "resolve_eligibility": False})
        )
        assert resp.status == 200
        assert held_during_write == [True], (
            "the config lock agent CRUD writes under must be held across the "
            "offloaded write, not merely taken somewhere in the handler"
        )

    @pytest.mark.asyncio
    async def test_declared_env_blocks_pooling_only_while_sharing_is_on(
        self, agents_dir: Path, routed_allowlist
    ) -> None:
        """The row has to say when stubbing could not produce a SHARED backend.

        The rewriter leaves an env-declaring entry unwrapped rather than spawn a
        pooled backend without a declared key, so a bulk action that stubbed it
        would report work the broker then silently skips. With sharing OFF there
        is no pooled spawn to withhold anything, so the same server is not
        blocked — the flag describes the pooled path, not the server.
        """
        (agents_dir / "a.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "mcpServers": {
                        "declares-env": {"command": "run", "env": {"HOME_DIR": "/x"}},
                        "no-env": {"command": "run"},
                    },
                }
            ),
            encoding="utf-8",
        )

        routed_allowlist([], enabled=True)
        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }
        assert rows["declares-env"]["pooling_blocked_by_env"] is True
        assert rows["no-env"]["pooling_blocked_by_env"] is False

        # Same config, sharing off: nothing is pooled, so nothing is withheld.
        routed_allowlist([], enabled=False)
        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }
        assert rows["declares-env"]["pooling_blocked_by_env"] is False

        # Forwarding on: only the rotating-secret and credential classes are
        # withheld, so an ordinary declared key stops blocking.
        routed_allowlist([], enabled=True, forward_declared_env=True)
        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }
        assert rows["declares-env"]["pooling_blocked_by_env"] is False

    @pytest.mark.asyncio
    async def test_missing_agents_dir_yields_no_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routed_allowlist
    ) -> None:
        import kiro_crew.agent as agent_mod

        routed_allowlist([])
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path / "absent")
        resp = await mcp_mod.api_mcp_gateway_servers(_request())
        assert resp.status == 200
        assert _payload(resp) == {"servers": []}

    @pytest.mark.asyncio
    async def test_dedupes_across_agents_and_computes_effective_stub_state(
        self, agents_dir: Path, routed_allowlist
    ) -> None:
        routed_allowlist(["allowed-mcp"])
        (agents_dir / "a.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "mcpServers": {
                        "allowed-mcp": {"command": "run"},
                        "opted-in": {"command": "run", "poolable": True},
                        "plain": {"command": "run"},
                        "remote": {"url": "https://example.invalid/sse"},
                        "junk": "not-a-mapping",
                    },
                }
            ),
            encoding="utf-8",
        )
        (agents_dir / "b.json").write_text(
            json.dumps({"name": "beta", "mcpServers": {"allowed-mcp": {"command": "run"}}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_servers(_request())
        rows = {r["name"]: r for r in _payload(resp)["servers"]}

        assert "junk" not in rows  # non-mapping entries are ignored
        assert rows["allowed-mcp"]["agents"] == ["alpha", "beta"]  # deduped + sorted
        assert rows["allowed-mcp"]["stub"] is True
        assert rows["allowed-mcp"]["in_allowlist"] is True
        # A spec-level opt-in is NOT sufficient any more, and the row must say so:
        # only ``stub_servers`` produces a stub, so reporting one here would claim
        # a stub the broker never created. ``entry_poolable`` is still surfaced as
        # information about the spec.
        assert rows["opted-in"]["stub"] is False
        assert rows["opted-in"]["in_allowlist"] is False
        assert rows["opted-in"]["entry_poolable"] is True
        # Neither allowlisted nor opted in.
        assert rows["plain"]["stub"] is False
        # HTTP/SSE transports are shared by nature, never pooled.
        assert rows["remote"]["transport"] == "http"
        assert rows["remote"]["stub"] is False
        assert list(rows) == sorted(rows)  # response is name-sorted

    @pytest.mark.asyncio
    async def test_denylisted_server_can_never_be_pooled(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, routed_allowlist
    ) -> None:
        from kiro_crew.mcp_gateway import rewriter

        monkeypatch.setattr(rewriter, "UNPOOLABLE_SERVERS", frozenset({"never-mcp"}))
        routed_allowlist(["never-mcp"])
        (agents_dir / "a.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "mcpServers": {"never-mcp": {"command": "run", "stub": True}},
                }
            ),
            encoding="utf-8",
        )
        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }
        assert rows["never-mcp"]["denylisted"] is True
        assert rows["never-mcp"]["stub"] is False

    @pytest.mark.asyncio
    async def test_two_agents_disagreeing_on_the_command_withhold_the_measurement(
        self, agents_dir: Path, routed_allowlist, monkeypatch
    ) -> None:
        """A merged row must not inherit a measurement of one of its definitions.

        Discovery merges by NAME before probing, so only the definition that wins
        the merge is ever measured. If two agents disagree about the command, the
        row covers something nobody ran — and reporting the measured one's verdict
        would tell the operator it is safe to share a backend that was never
        started.
        """
        routed_allowlist([])
        (agents_dir / "alpha.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": {"two-faced": {"command": "/bin/a"}}}),
            encoding="utf-8",
        )
        (agents_dir / "beta.json").write_text(
            json.dumps({"name": "beta", "mcpServers": {"two-faced": {"command": "/bin/b"}}}),
            encoding="utf-8",
        )
        # A clean, sharing-granting measurement exists under that name. The probe
        # metadata declares caller-identity so that consuming the measurement
        # WOULD grant sharing — without that, both branches look the same and the
        # test would pass whether or not the gate works.
        _seed_probe(monkeypatch, "two-faced")
        monkeypatch.setattr(
            mcp_mod, "_load_shareability_state", lambda: ({}, {"two-faced": (True, False)})
        )

        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }

        rec = rows["two-faced"]["recommendation"]
        assert sorted(rows["two-faced"]["agents"]) == ["alpha", "beta"]
        assert rec["recommendShare"] is False, rec
        assert "preflight_not_run" in [r["code"] for r in rec["reasons"]], rec

    @pytest.mark.asyncio
    async def test_one_definition_in_two_agents_still_gets_its_measurement(
        self, agents_dir: Path, routed_allowlist, monkeypatch
    ) -> None:
        """The ordinary case must keep working: same server, several agents.

        Withholding on agent count instead of distinct launches would silently
        drop the recommendation for most real configurations.
        """
        routed_allowlist([])
        spec = {"mcpServers": {"shared-mcp": {"command": "/bin/a", "args": ["--x"]}}}
        (agents_dir / "alpha.json").write_text(
            json.dumps({"name": "alpha", **spec}), encoding="utf-8"
        )
        (agents_dir / "beta.json").write_text(
            json.dumps({"name": "beta", **spec}), encoding="utf-8"
        )
        _seed_probe(monkeypatch, "shared-mcp")
        monkeypatch.setattr(
            mcp_mod, "_load_shareability_state", lambda: ({}, {"shared-mcp": (True, False)})
        )

        rows = {
            r["name"]: r
            for r in _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        }

        rec = rows["shared-mcp"]["recommendation"]
        assert rec["recommendShare"] is True, rec
        assert "preflight_passed" in [r["code"] for r in rec["reasons"]], rec

    @pytest.mark.asyncio
    async def test_unreadable_and_non_object_agent_files_are_skipped(
        self, agents_dir: Path, routed_allowlist
    ) -> None:
        routed_allowlist([])
        (agents_dir / "broken.json").write_text("{oops", encoding="utf-8")
        (agents_dir / "list.json").write_text("[1, 2]", encoding="utf-8")
        (agents_dir / "nomcp.json").write_text(
            json.dumps({"name": "x", "mcpServers": "wrong-type"}), encoding="utf-8"
        )
        (agents_dir / "ok.json").write_text(
            json.dumps({"mcpServers": {"good": {"command": "run"}}}), encoding="utf-8"
        )
        rows = _payload(await mcp_mod.api_mcp_gateway_servers(_request()))["servers"]
        assert [r["name"] for r in rows] == ["good"]
        # No "name" key in ok.json → the file stem is used as the agent label.
        assert rows[0]["agents"] == ["ok"]


# ── POST /api/mcp-gateway/servers/stub ──────────────────────────────


class TestGatewaySetStub:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(ValueError("boom"), state=SimpleNamespace())
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"stub": True}, "name is required"),
            ({"name": "../evil", "stub": True}, "invalid server name"),
            ({"name": "ok-mcp", "stub": "yes"}, "stub must be a boolean"),
            ({"name": "ok-mcp"}, "stub must be a boolean"),
        ],
    )
    async def test_validation_matrix_is_400(
        self, monkeypatch: pytest.MonkeyPatch, body: dict, expected: str
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(_request(body, state=SimpleNamespace()))
        assert resp.status == 400
        assert expected in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_corrupt_config_json_is_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ nope", encoding="utf-8")
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 500
        assert "corrupt" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_object_section_is_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcp_gateway": []}), encoding="utf-8")
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 500
        assert "not an object" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_persists_allowlist_without_an_apply_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the gateway unwired the allowlist is persisted AND reported pending.

        The write happens before the callback is ever reached, so an unwired
        gateway means the change was recorded -- the same state the callback
        reports. Answering ``applied: false`` alone would drop the client onto its
        fault branch and blame the gateway for a change that is safely saved.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body == {
            "ok": True,
            "name": "ok-mcp",
            "stub": True,
            "applied": False,
            "restart_required": True,
        }
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["ok-mcp"]

    @pytest.mark.asyncio
    async def test_an_unwired_batch_that_wrote_nothing_claims_no_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unwired answer must stay tied to something having been WRITTEN.

        A server-resolved batch where no candidate qualified persists nothing, so
        there is no pending change and nothing for a restart to pick up. Claiming
        ``restart_required`` here would send the operator to restart the gateway
        for a write that never happened.
        """
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        monkeypatch.setattr(
            mcp_mod,
            "_collect_server_rows",
            lambda *a, **k: {},
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(
                {"name": "nope-mcp", "stub": True, "resolve_eligibility": True},
                state=SimpleNamespace(),
            )
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body.get("applied") is False
        assert "restart_required" not in body
        assert body.get("stubbed") == []

    @pytest.mark.asyncio
    async def test_the_first_toggle_keeps_the_migrated_legacy_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy config's effective set survives the first write.

        The runtime resolves the stub set through the deprecated
        ``poolable_servers`` when ``stub_servers`` is absent. Seeding this
        read-modify-write from the raw key instead would start from nothing, so
        enabling one server would persist only that server and silently unstub
        everything the migration was preserving.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"enabled": True, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "y-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["x-mcp", "y-mcp"]
        # The migrated set is frozen into the roster, not merged with the click.
        assert saved["mcp_gateway"]["stub_servers"] == ["x-mcp"]
        assert saved["mcp_gateway"]["stub_overrides"] == {"y-mcp": True}

    @pytest.mark.asyncio
    async def test_a_disabled_legacy_config_is_not_seeded_by_the_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same guard the resolver applies: a gateway that was off ran no
        stub, so its inert allowlist must not be revived by a toggle either."""
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"enabled": False, "poolable_servers": ["x-mcp"]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "y-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["y-mcp"]
        # The inert allowlist stays unrevived: the roster froze empty.
        assert saved["mcp_gateway"]["stub_servers"] == []

    @pytest.mark.asyncio
    async def test_removal_dedupes_and_drops_non_string_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["b-mcp", "a-mcp", "a-mcp", 7]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": False}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["b-mcp"]
        # The write still normalizes the roster's FORM -- the duplicate and the
        # non-string are gone -- while leaving its membership to the roster's owner.
        # A surviving duplicate would make the dashboard's stub_count overcount.
        assert saved["mcp_gateway"]["stub_servers"] == ["a-mcp", "b-mcp"]
        assert saved["mcp_gateway"]["stub_overrides"] == {"a-mcp": False}

    @pytest.mark.asyncio
    async def test_toggling_back_to_agree_with_the_roster_drops_the_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-following the roster, not freezing its current value.

        An override that agrees with the roster is identical in effect today and
        differs tomorrow: stored, it would pin that server against the next roster
        change. So an operator who toggles a server back to what the roster says is
        asking to follow the roster again, and the entry must go.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcp_gateway": {"stub_servers": ["a-mcp"]}}), encoding="utf-8")

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": False}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))["mcp_gateway"]
        assert saved["stub_overrides"] == {"a-mcp": False}
        assert _effective_stubs(saved) == []

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))["mcp_gateway"]
        assert "stub_overrides" not in saved, (
            "the override survived a toggle back to the roster's own answer, so "
            "this server is now pinned against any later roster change"
        )
        assert _effective_stubs(saved) == ["a-mcp"]

    @pytest.mark.asyncio
    async def test_a_deviation_does_not_shadow_a_later_roster_addition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through the handler: the reason the two layers are separate.

        The operator turns one shipped server off; the edition then ships another.
        The new server must arrive, and the one they turned off must stay off.
        """
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["a-mcp", "b-mcp"]}}),
            encoding="utf-8",
        )

        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "b-mcp", "stub": False}, state=SimpleNamespace())
        )
        assert resp.status == 200

        # Checked BEFORE the release is simulated: overwriting the roster below
        # would otherwise hide a handler that had already taken it over, and the
        # claim being made here is precisely that the click did not.
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["mcp_gateway"]["stub_servers"] == [
            "a-mcp",
            "b-mcp",
        ], "the click rewrote the roster, so the edition no longer owns it"
        assert saved["mcp_gateway"]["stub_overrides"] == {"b-mcp": False}

        # The edition's next release grows the roster. Nothing else is touched.
        saved["mcp_gateway"]["stub_servers"] = ["a-mcp", "b-mcp", "c-mcp"]
        path.write_text(json.dumps(saved), encoding="utf-8")

        assert _effective_stubs(saved["mcp_gateway"]) == ["a-mcp", "c-mcp"], (
            "the roster addition did not arrive, or the operator's opt-out was "
            "overwritten -- the click took ownership of the roster"
        )

    @pytest.mark.asyncio
    async def test_apply_result_is_merged_into_the_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        apply_cb = AsyncMock(return_value={"applied": True, "sessions_relinked": 2})
        state = SimpleNamespace(_mcp_gateway_apply_stub=apply_cb)
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=state)
        )
        assert resp.status == 200
        assert _payload(resp)["sessions_relinked"] == 2
        apply_cb.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_apply_failure_is_500_and_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config.loader import config_path

        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        state = SimpleNamespace(
            _mcp_gateway_apply_stub=AsyncMock(side_effect=RuntimeError("relink"))
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=state)
        )
        assert resp.status == 500
        # Raw exception text stays server-side; client body is generic + coded.
        body = _payload(resp)
        assert "relink" not in body["error"]
        assert body["error"] == "apply failed"
        assert body["code"] == "mcp_apply_failed"
        # The config write happens BEFORE apply, so it survives the failure.
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["ok-mcp"]
        outcomes = [c.kwargs.get("outcome") for c in sel.log_api_access.call_args_list]
        assert "error" in outcomes


# ── the batch ("toggle all") form of the same endpoint ──────────────────


class TestGatewaySetStubBatch:
    """``{"names": [...]}`` writes the allowlist once for the whole set."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"names": "a-mcp", "stub": True}, "names must be a list of strings"),
            (
                {"names": ["a-mcp", 7], "stub": True},
                "names must be a list of strings",
            ),
            ({"names": [], "stub": True}, "names is required"),
            ({"names": ["  "], "stub": True}, "names is required"),
            ({"names": ["../evil"], "stub": True}, "invalid server name"),
            ({"names": ["a-mcp"]}, "stub must be a boolean"),
        ],
    )
    async def test_validation_matrix_is_400(
        self, monkeypatch: pytest.MonkeyPatch, body: dict, expected: str
    ) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(_request(body, state=SimpleNamespace()))
        assert resp.status == 400
        assert expected in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_oversized_batch_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        names = [f"srv{i}-mcp" for i in range(mcp_mod._MAX_STUB_BATCH + 1)]
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": names, "stub": True}, state=SimpleNamespace())
        )
        assert resp.status == 400
        assert "at most" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        resp = await mcp_mod.api_mcp_gateway_set_stub(_request(["a-mcp"], state=SimpleNamespace()))
        assert resp.status == 400
        assert "object" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_adds_every_name_in_one_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["kept-mcp"]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(
                {"names": ["b-mcp", "a-mcp", "a-mcp"], "stub": True},
                state=SimpleNamespace(),
            )
        )
        assert resp.status == 400
        assert _payload(resp)["code"] == "batch_stub_requires_individual"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["kept-mcp"]

    @pytest.mark.asyncio
    async def test_removes_every_name_and_leaves_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import config_path

        monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcp_gateway": {"stub_servers": ["a-mcp", "b-mcp", "kept-mcp", 7]}}),
            encoding="utf-8",
        )
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request(
                # A name that is not in the allowlist must not fail the batch.
                {"names": ["a-mcp", "b-mcp", "absent-mcp"], "stub": False},
                state=SimpleNamespace(),
            )
        )
        assert resp.status == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert _effective_stubs(saved["mcp_gateway"]) == ["kept-mcp"]

    @pytest.mark.asyncio
    async def test_re_applies_the_pool_once_for_the_whole_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N names must not mean N re-links — that is the point of the form."""
        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        apply_cb = AsyncMock(return_value={"applied": True, "sessions_relinked": 3})
        state = SimpleNamespace(_mcp_gateway_apply_stub=apply_cb)
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"names": ["a-mcp", "b-mcp"], "stub": True}, state=state)
        )
        assert resp.status == 400
        assert _payload(resp)["code"] == "batch_stub_requires_individual"
        apply_cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_name_form_still_answers_with_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`names` absent keeps the pre-existing single-server contract."""
        sel = MagicMock()
        monkeypatch.setattr(mcp_mod, "sel", lambda: sel)
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "ok-mcp", "stub": True}, state=SimpleNamespace())
        )
        body = _payload(resp)
        assert body == {
            "ok": True,
            "name": "ok-mcp",
            "stub": True,
            "applied": False,
            "restart_required": True,
        }
        audited = [c.kwargs.get("resources") for c in sel.log_api_access.call_args_list]
        assert any("name=ok-mcp" in (r or "") for r in audited)


# ── the synchronous cleanup-sweep file lock ─────────────────────────────


class TestSyncFileLock:
    def test_creates_the_lock_sidecar_and_releases_it(self, sandbox: SimpleNamespace) -> None:
        """The sweep's lock must be re-entrant across sequential acquires."""
        lock_path = mcp_mod._MCP_LOCK_PATH
        assert not lock_path.exists()
        with mcp_mod._get_mcp_lock_sync():
            assert lock_path.exists()
        # Released: a second acquire in the same process must not block.
        with mcp_mod._get_mcp_lock_sync():
            assert lock_path.exists()

    @pytest.mark.asyncio
    async def test_async_lock_shares_the_same_sidecar(self, sandbox: SimpleNamespace) -> None:
        async with mcp_mod._get_mcp_lock():
            assert mcp_mod._MCP_LOCK_PATH.exists()
        with mcp_mod._get_mcp_lock_sync():
            pass


# ── the set-stub eligibility rule ───────────────────────────────────────


class TestStubEligibility:
    """The eligibility rule, which lives on the server rather than in the browser.

    A client can only answer for the moment it read the rows: sharing is a separate
    switch another dashboard or the CLI can flip, and verdicts move as measurements
    land. Resolving it inside the write's own lock hold is what makes the decision
    and the write see one state, so these are the tests of the rule itself.
    """

    @staticmethod
    def _spec(agents_dir: Path, servers: dict[str, dict]) -> None:
        (agents_dir / "a.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": servers}), encoding="utf-8"
        )

    @staticmethod
    def _verdict(monkeypatch: pytest.MonkeyPatch, *, stub: bool, share: bool) -> None:
        """Pin the verdict so these test the RULE, not the engine behind it."""
        from kiro_crew.mcp_gateway.shareability import ShareVerdict, Strength

        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: ShareVerdict(
                name=name,
                strength=Strength.NO_OBJECTION,
                recommend_stub=stub,
                recommend_share=share,
                reasons=(),
            ),
        )

    def test_the_sharing_state_picks_which_flag_the_rule_reads(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same server, same verdict, two answers.

        With sharing OFF a stub keeps the backend 1:1 with the session, so the
        weakest useful verdict is enough. With sharing ON the identical write hands
        that server co-tenants, which is what ``recommend_share`` means.
        """
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=False)

        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]
        assert skipped == []

        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "evidence_insufficient"}]

    def test_a_server_claiming_isolation_is_allowed_to_co_tenant(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, _ = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]

    def test_declared_env_a_shared_backend_would_withhold_skips_the_server(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rewriter leaves that entry unwrapped, so stubbing produces no pool.

        Counting it would report work the broker never does. With sharing OFF there
        is no pooled spawn to withhold anything, so the same server qualifies.
        """
        self._spec(agents_dir, {"a-mcp": {"command": "run", "env": {"HOME_DIR": "/x"}}})
        self._verdict(monkeypatch, stub=True, share=True)

        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "pooling_blocked_by_env"}]

        eligible, _ = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]

    def test_a_server_with_no_stdio_pipe_is_never_eligible(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._spec(agents_dir, {"http-mcp": {"url": "https://example.test"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, skipped = mcp_mod._stub_eligibility(
            ["http-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "http-mcp", "reason": "cannot_stub"}]

    def test_a_denylisted_server_is_never_eligible(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``UNPOOLABLE_SERVERS`` is empty today, so the branch is patched in.

        Testing it against the live set would pass by vacuum: an empty denylist
        makes the assertion unfalsifiable, and the day a name is added is exactly
        when this needs to already work.
        """
        import kiro_crew.mcp_gateway.rewriter as rewriter_mod

        monkeypatch.setattr(rewriter_mod, "UNPOOLABLE_SERVERS", frozenset({"walled-mcp"}))
        self._spec(agents_dir, {"walled-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, skipped = mcp_mod._stub_eligibility(
            ["walled-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "walled-mcp", "reason": "cannot_stub"}]

    def test_a_measurement_of_a_replaced_command_does_not_count(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cache rows are keyed by NAME, so a replaced command keeps the old row.

        That is tolerable for a rendered row, which only shows a stale verdict
        until the next probe. It is not tolerable for an automatic write: stubbing
        on it pools a program nobody measured, and with sharing on that hands one
        session's state to another. The eligibility read therefore goes through
        ``VerdictCache.get`` with the CURRENT identity, where a mismatch reads as
        no measurement.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for
        from kiro_crew.mcp_gateway.verdict_cache import CachedPreflight, load_cache

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)

        # Measure the server as it stands, storing a clean row under its identity.
        self._spec(agents_dir, {"a-mcp": {"command": "orig-server"}})
        measured = SimpleNamespace(command="orig-server", args=[], env={})
        cache = load_cache(runtime)
        cache.put(
            "a-mcp",
            identity_for(measured),
            CachedPreflight(ran=True, caller_sensitive=False, reasons=(), evaluated_at=0.0),
        )
        cache.flush()

        # First leg: with the command unchanged the measurement is honoured, so
        # the assertion below is about identity rather than about an empty cache.
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, _ = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == ["a-mcp"]

        # Same name, different program. The stored row still exists and
        # ``get_by_name`` would return it; the identity-checked read must not.
        self._spec(agents_dir, {"a-mcp": {"command": "replaced-server"}})
        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: SimpleNamespace(
                recommend_stub=kw["preflight"] is not None,
                recommend_share=kw["preflight"] is not None,
            ),
        )
        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "evidence_insufficient"}]

    def test_a_recorded_hazard_survives_an_identity_change(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Hazards are read name-wide, and that asymmetry is deliberate.

        A preflight can promote a server, so it must belong to the program being
        written about. A hazard only ever demotes, so filtering it by the current
        identity would DISCARD a recorded objection -- the permissive direction --
        every time a command changed. The cautious read is the wider one.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})

        seen: dict[str, tuple[str, ...]] = {}

        def _capture(name, **kw):  # type: ignore[no-untyped-def]
            seen[name] = kw["observed_hazards"]
            return SimpleNamespace(recommend_stub=False, recommend_share=False)

        monkeypatch.setattr(mcp_mod, "_assess_server", _capture)
        monkeypatch.setattr(
            mcp_mod.hazards,
            "load_ledger",
            lambda _rt: SimpleNamespace(as_dict=lambda: {"a-mcp": ("caller_state_leak",)}),
        )

        mcp_mod._stub_eligibility(["a-mcp"], sharing_on=True, forward_declared_env=False)
        assert seen["a-mcp"] == ("caller_state_leak",)

    def test_two_agents_declaring_one_name_differently_have_no_usable_measurement(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Definitions that differ only in env are different programs to the pool.

        ``PoolKey`` includes the effective env, so these two declarations never
        share a backend with each other -- but the write is per NAME and covers
        both. A measurement of one is therefore not evidence for the other, and
        accepting it would pool a program nobody probed.

        The command and args are byte-identical here on purpose: that is the case a
        command+args launch hash cannot see, so this fails if eligibility consults
        that proxy instead of the full identity.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for
        from kiro_crew.mcp_gateway.verdict_cache import CachedPreflight, load_cache

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(mcp_mod, "records_dir", lambda _socket: runtime)

        (agents_dir / "a.json").write_text(
            json.dumps(
                {"name": "alpha", "mcpServers": {"a-mcp": {"command": "run", "env": {"K": "1"}}}}
            ),
            encoding="utf-8",
        )
        (agents_dir / "b.json").write_text(
            json.dumps(
                {"name": "beta", "mcpServers": {"a-mcp": {"command": "run", "env": {"K": "2"}}}}
            ),
            encoding="utf-8",
        )

        # A clean measurement exists for the FIRST declaration.
        cache = load_cache(runtime)
        cache.put(
            "a-mcp",
            identity_for(SimpleNamespace(command="run", args=[], env={"K": "1"})),
            CachedPreflight(ran=True, caller_sensitive=False, reasons=(), evaluated_at=0.0),
        )
        cache.flush()

        monkeypatch.setattr(
            mcp_mod,
            "_assess_server",
            lambda name, **kw: SimpleNamespace(
                recommend_stub=kw["preflight"] is not None,
                recommend_share=kw["preflight"] is not None,
            ),
        )
        # ``forward_declared_env`` on, so a plainly-named declared key is forwarded
        # rather than withheld. Without it the env-withheld branch answers first
        # and this would pass without exercising identity at all.
        eligible, skipped = mcp_mod._stub_eligibility(
            ["a-mcp"], sharing_on=True, forward_declared_env=True
        )
        assert eligible == []
        assert skipped == [{"name": "a-mcp", "reason": "evidence_insufficient"}]

    def test_a_name_no_agent_declares_is_reported_rather_than_written(
        self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale client can name a server that has since been removed.

        Writing it would put a name in ``stub_servers`` that nothing can ever spawn,
        so it is skipped and named instead.
        """
        self._spec(agents_dir, {"a-mcp": {"command": "run"}})
        self._verdict(monkeypatch, stub=True, share=True)
        eligible, skipped = mcp_mod._stub_eligibility(
            ["ghost-mcp"], sharing_on=False, forward_declared_env=False
        )
        assert eligible == []
        assert skipped == [{"name": "ghost-mcp", "reason": "unknown"}]


# ── a shareability verdict refuses a probe from before an edit ──────────


class TestShareabilityRefusesAProbeFromBeforeAnEdit:
    """``probe_metadata`` is keyed by name, so the verdict readers gate it.

    Each test pins one call site. Reverting that site's ``probe_current``
    argument leaves the verdict grounded in the previous target's handshake,
    and only a test that goes through the site can see it.
    """

    _ROW = {
        "agents": {"alpha"},
        "transport": "stdio",
        "entry_poolable": False,
        "env_names": set(),
        "launch_ids": {"x"},
    }

    def test_a_refused_probe_reads_as_never_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_probe(monkeypatch, "a-mcp")
        args = dict(is_stdio=True, env_names=(), observed_hazards=(), preflight=None)
        measured = mcp_mod._assess_server("a-mcp", **args).to_dict()
        refused = mcp_mod._assess_server("a-mcp", probe_current=False, **args).to_dict()
        monkeypatch.setattr(mcp_mod, "probe_metadata", lambda n: None)
        never = mcp_mod._assess_server("a-mcp", **args).to_dict()
        # Guard the guard: the seeded probe has to change the verdict at all.
        assert measured != never
        assert refused == never

    @pytest.mark.asyncio
    async def test_the_rows_endpoint_passes_the_edit_check_through(
        self, monkeypatch: pytest.MonkeyPatch, routed_allowlist
    ) -> None:
        routed_allowlist([])
        monkeypatch.setattr(mcp_mod, "_collect_server_rows", lambda: {"a-mcp": dict(self._ROW)})
        monkeypatch.setattr(mcp_mod, "_load_shareability_state", lambda: ({}, {}))
        monkeypatch.setattr(mcp_mod, "_probe_currency", lambda: {"a-mcp": False})
        seen: dict[str, Any] = {}
        real = mcp_mod._assess_server

        def spy(name: str, **kw: Any):
            seen[name] = kw.get("probe_current")
            return real(name, **kw)

        monkeypatch.setattr(mcp_mod, "_assess_server", spy)

        await mcp_mod.api_mcp_gateway_servers(_request())

        assert seen == {"a-mcp": False}

    @pytest.mark.asyncio
    async def test_the_stub_write_passes_the_edit_check_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import kiro_crew.agent as agent_mod
        import kiro_crew.config.loader as loader
        from kiro_crew.mcp_gateway.shareability import ShareVerdict, Strength

        cfg_path = tmp_path / "config.json"
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "a.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": {"a-mcp": {"command": "run"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mcp_mod, "_probe_currency", lambda: {"a-mcp": False})
        seen: dict[str, Any] = {}

        def spy(name: str, **kw: Any) -> ShareVerdict:
            seen[name] = kw.get("probe_current")
            return ShareVerdict(
                name=name,
                strength=Strength.MEASURED,
                recommend_stub=True,
                recommend_share=False,
                reasons=(),
            )

        monkeypatch.setattr(mcp_mod, "_assess_server", spy)
        cfg_path.write_text(json.dumps({"mcp_gateway": {"enabled": False, "stub_servers": []}}))

        # One server per request with its displayed launch: a stub approval is a
        # compare-and-set on that launch, and ``_request`` fills in the
        # launch of a command named after the server, which the agent below runs.
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": "a-mcp", "stub": True, "resolve_eligibility": True})
        )

        assert resp.status == 200, resp.text
        assert seen == {"a-mcp": False}

    def test_probe_currency_compares_against_the_current_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        disc._probe_cache.clear()
        try:
            disc._cache_probe(McpServerInfo(name="edited", command="old-bin", status="ok"))
            disc._cache_probe(McpServerInfo(name="same", command="bin", status="ok"))
            servers = [
                McpServerInfo(name="edited", command="new-bin"),
                McpServerInfo(name="same", command="bin"),
            ]
            monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))

            assert mcp_mod._probe_currency() == {"edited": False, "same": True}
        finally:
            disc._probe_cache.clear()
