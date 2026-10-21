"""Unit coverage for gatewayd's backend acquire and the respawn that rebinds a stub.

For ``_acquire_backend`` it pins the clean rejection of an unresolvable server, a spawn that
reports ``was_spawned`` and starts the stdout pump, a pool reuse that reports no spawn,
declared env winning over the inherited value, a ``secret://`` temp dir that is never
classified or logged raw and reaches the spawn resolved, and a declared temp dir inside the
sandbox-sealed runtime parent: classified off the event loop, it leaves no temp key in the
spawn env and is quoted escaped in the warning. For ``_respawn_backend_for_stub`` it pins
the give-up exits (no captured ``initialize``, an acquire rejection, a prime failure that
releases its reservation), the cancelled old writer task and its flushed inbox, the
transparent rebind and its lifecycle log line, a recorded hazard bringing the replacement
back connection-private, the subscription replay gated on an unchanged owner, the
declaring agent carried into the acquire and the attach, and the replacement's tool-set
check: drift, an unanswerable probe and a retargeted owner are refused, an unchanged set or
a stub with no served listing is adopted, both outcomes are audited, and the validated
surface carries into the next respawn. The other daemon helpers are covered in
``test_mcp_gatewayd_coverage.py``, and the shared doubles, the ``_await_kwargs`` and
``_drain_task`` helpers and the autouse registry reset live in
``mcp_gatewayd_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_gatewayd_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _await_kwargs,
    _clean_gateway_globals,
    _drain_task,
    _fake_backend,
    _FakeWriter,
    _pool_key,
)

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend import Backend, BackendGone
from kiro_crew.mcp_gateway.pool import BackendPool, BackendUnavailable

pytestmark = pytest.mark.xdist_group("mcp_gateway")


# --- backend acquire / respawn ----------------------------------------------


class TestAcquireBackend:
    @pytest.mark.asyncio
    async def test_unresolvable_server_is_a_clean_rejection(self):
        pool = BackendPool(max_backends=2)
        with pytest.raises(gw._TargetUnknown) as excinfo:
            await gw._acquire_backend(pool, _pool_key(server="ghost-mcp"), lambda k: None)
        assert "ghost-mcp" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_spawn_reports_was_spawned_and_starts_the_stdout_pump(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        key = _pool_key(server="spawn-mcp")
        backend = _fake_backend(key)
        monkeypatch.setattr(gw, "spawn_backend", AsyncMock(return_value=backend))
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})

        got, was_spawned = await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", ["--stdio"], {"A": "1"}, "/tmp/cov")
        )

        assert got is backend
        assert was_spawned is True
        assert backend._stdout_task is not None
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_declared_env_is_merged_over_the_inherited_value(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        key = _pool_key(server="declared-mcp")
        backend = _fake_backend(key)
        spawn = AsyncMock(return_value=backend)
        monkeypatch.setattr(gw, "spawn_backend", spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {"A": "declared"})

        await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", [], {"A": "inherited"}, "/tmp/cov")
        )

        assert _await_kwargs(spawn)["env"]["A"] == "declared"
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_secret_temp_skips_raw_precheck_and_reaches_spawn_backend(
        self, monkeypatch, caplog
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod

        pool = BackendPool(max_backends=2)
        key = _pool_key(server="secret-temp-mcp")
        backend = _fake_backend(key)
        captured: dict[str, Any] = {}

        async def _spawn_backend(**kwargs: Any) -> Backend:
            captured.update(kwargs)
            captured["env"] = dict(kwargs["env"])
            return backend

        monkeypatch.setattr(gw, "spawn_backend", AsyncMock(side_effect=_spawn_backend))
        raw_reference = "secret://../../../../run/x\nFORGED"
        monkeypatch.setattr(
            gw,
            "_declared_env_to_forward",
            lambda _key: {"TMPDIR": raw_reference},
        )
        classified: list[str] = []

        def _sealed(path: str) -> str:
            classified.append(path)
            return "sealed"

        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", _sealed)

        def _resolve(env: dict[str, str], _config: Path):
            if "TMPDIR" not in env:
                return dict(env), set()
            return {**env, "TMPDIR": "/resolved/secret"}, {"TMPDIR"}

        monkeypatch.setattr(gw, "resolve_secret_uris", _resolve)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.gatewayd"):
            await gw._acquire_backend(
                pool,
                key,
                lambda _key: ("demo-bin", [], {}, "/tmp/cov"),
            )

        assert classified == []
        assert raw_reference not in caplog.text
        assert captured["env"]["TMPDIR"] == "/resolved/secret"
        assert captured["declared_temp_keys"] == ("TMPDIR",)
        assert captured["secret_env_keys"] == ("TMPDIR",)
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_sealed_declared_temp_is_removed_before_spawn(self, monkeypatch, caplog) -> None:
        from kiro_crew import sandbox as sandbox_mod

        pool = BackendPool(max_backends=2)
        key = _pool_key(server="sealed-temp-mcp")
        backend = _fake_backend(key)
        spawn = AsyncMock(return_value=backend)
        monkeypatch.setattr(gw, "spawn_backend", spawn)
        declared = "/sealed/runtime/tmp\nFORGED"
        monkeypatch.setattr(
            gw,
            "_declared_env_to_forward",
            lambda _key: {"TMPDIR": declared, "A": "declared"},
        )
        loop_thread = threading.get_ident()
        classifier_threads: list[int] = []

        def _sealed(_path: str) -> str:
            classifier_threads.append(threading.get_ident())
            return "sealed"

        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", _sealed)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.gatewayd"):
            await gw._acquire_backend(
                pool,
                key,
                lambda _key: (
                    "demo-bin",
                    [],
                    {"TMPDIR": "/ambient/tmp", "A": "inherited"},
                    "/tmp/cov",
                ),
            )

        kwargs = _await_kwargs(spawn)
        assert declared not in kwargs["env"].values()
        assert not [key for key in kwargs["env"] if key.upper() in ("TMPDIR", "TMP", "TEMP")]
        assert kwargs["declared_temp_keys"] == ()
        assert classifier_threads and all(thread != loop_thread for thread in classifier_threads)
        warning = next(
            record.getMessage()
            for record in caplog.records
            if "ignoring spec-declared" in record.getMessage()
        )
        assert f"TMPDIR={declared!r}" in warning
        assert "\nFORGED" not in warning
        assert "inside the sandbox-sealed runtime parent" in warning
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_pool_reuse_reports_was_spawned_false(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        key = _pool_key(server="reuse-mcp")
        backend = _fake_backend(key)
        monkeypatch.setattr(gw, "spawn_backend", AsyncMock(return_value=backend))
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})

        first, spawned_first = await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", [], {}, "/tmp/cov")
        )
        second, spawned_second = await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", [], {}, "/tmp/cov")
        )

        assert (spawned_first, spawned_second) == (True, False)
        assert first is second
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)


class TestRespawnBackendForStub:
    @pytest.mark.asyncio
    async def test_no_captured_initialize_gives_up(self):
        pool = BackendPool(max_backends=2)
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(),
            lambda k: None,
            "stub-r1",
            cast(Any, _FakeWriter()),
            None,
            old,
            None,
            None,
        )

        assert out is None
        old.detach_stub.assert_awaited_once_with("stub-r1")

    @pytest.mark.asyncio
    async def test_old_writer_task_is_cancelled_and_inbox_flushed(self):
        """Late replies for the stub's OTHER in-flight ids must still reach it,
        or kiro-cli hangs waiting on those ids forever."""
        pool = BackendPool(max_backends=2)
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        old_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await old_inbox.put(b'{"id":9,"error":{}}\n')
        writer = _FakeWriter()
        # flake-ok: pre-existing parked writer task that the respawn cancels, moved verbatim; tracked for a de-flake pass
        old_task = asyncio.create_task(asyncio.Event().wait())
        # flake-ok: pre-existing one-iteration yield that starts the parked writer task, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(),
            lambda k: None,
            "stub-r2",
            cast(Any, writer),
            None,
            old,
            old_inbox,
            cast(Any, old_task),
        )

        assert out is None
        assert old_task.done()
        assert writer.writes == [b'{"id":9,"error":{}}\n']

    @pytest.mark.asyncio
    async def test_acquire_rejection_gives_up_without_churning_spawns(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        monkeypatch.setattr(
            gw, "_acquire_backend", AsyncMock(side_effect=BackendUnavailable("breaker open"))
        )

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(),
            lambda k: None,
            "stub-r3",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is None

    @pytest.mark.asyncio
    async def test_prime_failure_gives_up_and_releases_the_reservation(self, monkeypatch):
        key = _pool_key(server="prime-fail-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        fresh = _fake_backend(key, pid=5150)
        fresh.prime_initialize = AsyncMock(  # type: ignore[method-assign]
            side_effect=BackendGone("died during prime")
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r4",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is None
        pool.unreserve.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_successful_respawn_rebinds_the_stub_transparently(self, monkeypatch, caplog):
        key = _pool_key(server="respawn-ok-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        fresh = _fake_backend(key, pid=6161)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        new_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        fresh.attach_stub = AsyncMock(return_value=new_inbox)  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        with caplog.at_level("INFO"):
            out = await gw._respawn_backend_for_stub(
                pool,
                key,
                lambda k: None,
                "stub-r5",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
            )

        assert out is not None
        got_backend, got_inbox, got_task = out
        assert got_backend is fresh
        assert got_inbox is new_inbox
        pool.unreserve.assert_called_once_with(key)
        # The rebind line names the FRESH backend's generation and attempt.
        [rebound] = [r.getMessage() for r in caplog.records if "rebound to fresh" in r.getMessage()]
        # It also names the dead backend's generation, so a chain joins across the respawn.
        assert rebound.endswith(
            fresh.lifecycle_fields("reconnect_succeeded", "success")
            + f" previous_backend_generation_id={old.backend_generation_id}"
        )
        assert fresh.backend_generation_id != old.backend_generation_id
        await _drain_task(got_task)

    @pytest.mark.asyncio
    async def test_a_recorded_hazard_makes_the_respawn_come_back_private(
        self, monkeypatch, tmp_path
    ):
        """The retreat's hole if the respawn inherited the shared binding blindly.

        The recycle that follows an unroutable server request comes straight back
        here, so re-pooling would hand the SAME stubs a shared backend for the
        server just observed misbehaving, and no new register happens to
        re-decide it. Also pins that a now-private respawn releases NO
        reservation: only ``pool.get_or_create`` reserves, so releasing on the
        old backend's binding instead would decrement a digest this respawn never
        reserved and drop a concurrent pooled connection's eviction protection.
        """
        from kiro_crew.mcp_gateway import hazards

        key = _pool_key(server="respawn-hazard-mcp")
        ledger = hazards.HazardLedger(tmp_path / hazards.HAZARDS_FILENAME)
        ledger.record(
            "respawn-hazard-mcp",
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
            hazards.launch_identity(
                key.command_args_hash, key.effective_env_hash, key.binary_version
            ),
        )
        monkeypatch.setattr(hazards, "_sink", ledger)

        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        assert not old.exclusive_token, "old backend must be POOLED for this case"
        fresh = _fake_backend(key, pid=6262)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        new_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        fresh.attach_stub = AsyncMock(return_value=new_inbox)  # type: ignore[method-assign]
        acquire = AsyncMock(return_value=(fresh, True))
        monkeypatch.setattr(gw, "_acquire_backend", acquire)

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-hz1",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        assert acquire.await_args.kwargs["exclusive_stub_uuid"] == "stub-hz1"
        pool.unreserve.assert_not_called()
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_replay_skipped_when_owner_rekeyed_mid_respawn(self, monkeypatch):
        """A ``claim`` frame can retarget this connection's identity during
        the respawn's acquire/prime awaits. The captured URIs belong to the
        OLD principal — replaying them would resubscribe the old owner's
        resources onto the rekeyed stub, the exact leak
        ``evict_stub_subscriptions`` exists to prevent. The replay gate must
        recheck the live owner and skip when it changed."""
        key = _pool_key(server="respawn-rekey-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        old.resource_subscription_uris = MagicMock(  # type: ignore[method-assign]
            return_value=["file:///old-owner.txt"]
        )
        fresh = _fake_backend(key, pid=7171)
        fresh.attach_stub = AsyncMock(return_value=asyncio.Queue())  # type: ignore[method-assign]
        fresh.replay_resource_subscriptions = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        old_caller = CallerContext(session_key="dashboard:old")
        conn = gw._StubConn("stub-r6", [], "pool", old_caller)
        # The claim lands while prime_initialize is awaited: the connection
        # identity names a NEW principal before the replay gate runs.
        fresh.prime_initialize = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda *_a, **_k: setattr(
                conn, "caller", CallerContext(session_key="dashboard:new")
            )
        )

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r6",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
            caller=old_caller,
            conn=conn,
        )

        assert out is not None  # the respawn itself still succeeds
        fresh.replay_resource_subscriptions.assert_not_awaited()
        _backend, _inbox, task = out
        await _drain_task(task)

    @pytest.mark.asyncio
    async def test_replay_proceeds_when_owner_unchanged(self, monkeypatch):
        """Control for the rekey gate: an unchanged owner still gets its
        subscriptions replayed onto the fresh backend."""
        key = _pool_key(server="respawn-stable-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        old.resource_subscription_uris = MagicMock(  # type: ignore[method-assign]
            return_value=["file:///same-owner.txt"]
        )
        fresh = _fake_backend(key, pid=7272)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        fresh.attach_stub = AsyncMock(return_value=asyncio.Queue())  # type: ignore[method-assign]
        fresh.replay_resource_subscriptions = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        same_caller = CallerContext(session_key="dashboard:same")
        conn = gw._StubConn("stub-r7", [], "pool", same_caller)

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r7",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
            caller=same_caller,
            conn=conn,
        )

        assert out is not None
        fresh.replay_resource_subscriptions.assert_awaited_once_with(
            "stub-r7", ["file:///same-owner.txt"], caller=same_caller
        )
        _backend, _inbox, task = out
        await _drain_task(task)

    # --- validating the replacement's tool set ------------------------------

    @staticmethod
    def _surface_pair(*, served, published, stub="stub-r8"):
        """An old backend that already served *served* TO ``stub``, and a fresh
        one that publishes *published* — both as projected tool surfaces."""
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        if served is not None:
            old._served_tool_surfaces[stub] = served
        fresh = _fake_backend(_pool_key(server="respawn-surface-mcp"), pid=8383)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        fresh.attach_stub = AsyncMock(return_value=asyncio.Queue())  # type: ignore[method-assign]
        fresh.probe_tool_surface = AsyncMock(return_value=published)  # type: ignore[method-assign]
        return old, fresh

    @pytest.mark.asyncio
    async def test_a_replacement_whose_tool_set_moved_is_not_adopted(self, monkeypatch):
        """The gap this closes: priming the captured handshake proves the fresh
        process talks MCP, so without this check a server upgraded in place is
        adopted under a session still holding the DEAD process's schema."""
        key = _pool_key(server="respawn-surface-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'},
            published={"readFile": '{"type":"object"}'},
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))
        audits: list[tuple] = []
        monkeypatch.setattr(gw, "_audit_replacement_validated", lambda *a: audits.append(a))

        with pytest.raises(gw._ReplacementRefused) as excinfo:
            await gw._respawn_backend_for_stub(
                pool,
                key,
                lambda k: None,
                "stub-r8",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=CallerContext(session_key="dashboard:1"),
            )

        # The SESSION is told what changed, not just "backend gone".
        assert "tool set changed" in str(excinfo.value)
        assert "read_file" in str(excinfo.value)

        # Refused BEFORE adoption: the stub is never bound to the replacement.
        fresh.attach_stub.assert_not_awaited()
        # And the give-up still releases the reservation it took, or the digest
        # is skipped by evict_idle forever.
        pool.unreserve.assert_called_once_with(key)
        assert audits and audits[0][0] == "dashboard:1"
        assert audits[0][2] == "denied"
        assert "gone=read_file" in audits[0][3]

    @pytest.mark.asyncio
    async def test_an_unchanged_tool_set_is_adopted(self, monkeypatch):
        """Control: the check must not cost the transparent recovery when the
        replacement publishes what the session was already told."""
        key = _pool_key(server="respawn-surface-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r9")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r9",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        # ``agent`` carries the dead backend's per-stub label onto the
        # replacement, so a render spooled after the respawn still names its
        # producing agent. Empty here: this double declares none.
        fresh.attach_stub.assert_awaited_once_with("stub-r9", agent="")
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_the_respawn_carries_the_agent_read_before_the_detach(self, monkeypatch):
        """The replacement inherits the dead backend's per-stub agent.

        ORDERING is the whole test, which is why ``detach_stub`` is left REAL
        here while the sibling tests mock it: detach prunes ``_stub_agents``
        with the inbox, so reading the agent after it would hand the replacement
        ``""`` and every render spooled through the respawned backend would name
        no producing agent and have its callbacks refused. The read therefore
        sits beside the ``replay_uris``/``old_surface`` captures, before the
        detach.
        """
        key = _pool_key(server="respawn-surface-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r21")
        # Real detach, and a real attach to populate what it prunes.
        del old.detach_stub
        await old.attach_stub("stub-r21", agent="gpu-dev")
        assert old.agent_for_stub("stub-r21") == "gpu-dev", "premise: the agent is recorded"
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r21",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        fresh.attach_stub.assert_awaited_once_with("stub-r21", agent="gpu-dev")
        # And the detach really did run, so the pass above is about ordering
        # rather than about a detach that never happened.
        assert old.agent_for_stub("stub-r21") == ""
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_the_respawn_acquire_carries_the_agent_too(self, monkeypatch):
        """The SPAWN needs the agent, not just the attach that follows it.

        The declared-env sidecar of a connection-private backend is named for
        the agent that declared it, and the spawn is where that env is read
        (``_declared_env_for_private_backend``). A respawn that named the stub
        but not its agent would look the sidecar up under ``""``, find none, and
        bring the replacement up with no declared env -- so every server that
        needs one dies at prime and the transparent recovery fails. The attach
        happens after the spawn, so carrying it only there is too late.
        """
        key = _pool_key(server="respawn-surface-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r22")
        del old.detach_stub
        await old.attach_stub("stub-r22", agent="gpu-dev")
        acquire = AsyncMock(return_value=(fresh, True))
        monkeypatch.setattr(gw, "_acquire_backend", acquire)

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r22",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        assert acquire.await_args is not None
        assert acquire.await_args.kwargs.get("declaring_agent") == "gpu-dev", (
            "the respawn spawned without its declaring agent, so a private "
            "backend's replacement would come up with no declared env"
        )
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_a_replacement_that_cannot_be_asked_is_not_adopted(self, monkeypatch):
        """A probe that establishes nothing is not agreement. The old backend
        answered a listing projectably, so a replacement that will not is the
        change — adopting on an unanswered probe would be the silent path again."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'}, published=None, stub="stub-r10"
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r10",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
            )

        fresh.attach_stub.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_owner_rekeyed_mid_respawn_is_not_adopted(self, monkeypatch):
        """A claim can retarget this connection during the probe. Both sides of
        the comparison belong to the CAPTURED caller, so across a rekey it
        describes a principal that does not own the stub — and re-probing would
        race the same way."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        # The tool set AGREES; only the owner moved.
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r14")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))
        audits: list[tuple] = []
        monkeypatch.setattr(gw, "_audit_replacement_validated", lambda *a: audits.append(a))

        captured = CallerContext(session_key="dashboard:old-owner")
        conn = gw._StubConn(
            "stub-r14", [], "pool", CallerContext(session_key="dashboard:new-owner")
        )

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r14",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=captured,
                conn=conn,
            )

        fresh.attach_stub.assert_not_awaited()
        assert audits and audits[0][2] == "denied"
        assert "retargeted mid-respawn" in audits[0][3]

    @pytest.mark.asyncio
    async def test_a_rekey_landing_after_validation_still_refuses(self, monkeypatch):
        """attach_stub and the subscription replay both await, so a claim can
        land AFTER the check beside the comparison. The re-check before the
        return is the one that closes that window; the earlier one only saves
        the attach."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r18")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        owner = CallerContext(session_key="dashboard:old-owner")
        conn = gw._StubConn("stub-r18", [], "pool", owner)

        async def _attach_then_rekey(stub_uuid, *, agent=""):
            # The claim lands during the adoption await, past the early check.
            conn.caller = CallerContext(session_key="dashboard:new-owner")
            return asyncio.Queue()

        fresh.attach_stub = AsyncMock(side_effect=_attach_then_rekey)  # type: ignore[method-assign]
        fresh.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r18",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=owner,
                conn=conn,
            )

        # The stub it had just attached is released, or the refcount holds a stub
        # that is about to be told the adoption failed.
        fresh.attach_stub.assert_awaited_once_with("stub-r18", agent="")
        fresh.detach_stub.assert_awaited_once_with("stub-r18")

    @pytest.mark.asyncio
    async def test_an_unchanged_owner_still_adopts(self, monkeypatch):
        """Control for the rekey gate: the same owner is not a rekey, and a
        connection that cannot answer the question is not one either."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r15")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        owner = CallerContext(session_key="dashboard:same")

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r15",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
            caller=owner,
            conn=gw._StubConn("stub-r15", [], "pool", owner),
        )

        assert out is not None
        fresh.attach_stub.assert_awaited_once_with("stub-r15", agent="")
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_no_listing_ever_served_skips_the_probe_entirely(self, monkeypatch):
        """With no claim on record there is nothing a replacement can
        contradict, so the recovery this path already performs must not become a
        failure — and the extra round-trip must not be paid either."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(served=None, published=None, stub="stub-r11")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r11",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        fresh.probe_tool_surface.assert_not_awaited()
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_an_adopted_replacement_is_audited_too(self, monkeypatch):
        """Both outcomes are access decisions about which process may answer a
        live session. Recording only refusals would leave the swap this guard
        exists to make visible as a rotating log line and nothing more — and it
        must be recorded in the no-anchor case especially, which is exactly where
        nothing checked it."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        audits: list[tuple] = []
        monkeypatch.setattr(gw, "_audit_replacement_validated", lambda *a: audits.append(a))
        same = {"read_file": '{"type":"object"}'}

        for stub, served, expected in (
            ("stub-r16", same, "verified: 1 tool(s) unchanged"),
            ("stub-r17", None, "not verified"),
        ):
            old, fresh = self._surface_pair(
                served=served,
                published=dict(same) if served is not None else None,
                stub=stub,
            )
            monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

            out = await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                stub,
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=CallerContext(session_key=f"dashboard:{stub}"),
            )

            assert out is not None
            await _drain_task(out[2])

        assert [a[2] for a in audits] == ["allowed", "allowed"]
        assert "verified: 1 tool(s) unchanged" in audits[0][3]
        assert "not verified" in audits[1][3]

    @pytest.mark.asyncio
    async def test_the_validated_surface_survives_into_the_next_respawn(self, monkeypatch):
        """The claim follows the SESSION, not the process. Without carrying it
        the replacement starts anchor-less, so a second respawn of the same stub
        adopts blindly while the client's frozen tool set is still its original
        listing — the guard would cover only the first swap in a session's life."""
        pool = BackendPool(max_backends=3)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, first = self._surface_pair(served=same, published=dict(same), stub="stub-r19")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(first, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r19",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )
        assert out is not None
        await _drain_task(out[2])

        # The adopted backend now holds the session's claim...
        assert first.served_tool_surface("stub-r19") == same

        # ...so when IT dies, the second replacement is validated too, and a
        # drifted one is refused rather than adopted.
        second = _fake_backend(_pool_key(server="respawn-surface-mcp"), pid=9494)
        second.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        second.attach_stub = AsyncMock(return_value=asyncio.Queue())  # type: ignore[method-assign]
        second.probe_tool_surface = AsyncMock(return_value={})  # type: ignore[method-assign]
        first.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(second, True)))

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r19",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                first,
                None,
                None,
            )

        second.attach_stub.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_co_pooled_stubs_listing_is_not_this_stubs_anchor(self, monkeypatch):
        """One backend serves several sessions. The comparison must be about the
        session being recovered, not whichever tenant listed most recently — a
        sibling's listing must neither supply nor suppress this stub's anchor."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        # Only the SIBLING was ever served a listing.
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'},
            published={"totally": '{"type":"object"}'},
            stub="stub-sibling",
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r12",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        # Adopted: stub-r12 holds no declaration, so the replacement's very
        # different tool set contradicts nothing it was told.
        assert out is not None
        fresh.probe_tool_surface.assert_not_awaited()
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_the_anchor_is_read_before_the_detach_that_prunes_it(self, monkeypatch):
        """Real ``detach_stub`` drops the stub's anchor. Reading it after the
        detach would report "nothing was ever served" for a session that was
        told plenty, and adopt a drifted replacement."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'},
            published={"readFile": '{"type":"object"}'},
            stub="stub-r13",
        )
        # NOT mocked: the real detach prunes the per-stub anchor.
        del old.detach_stub
        await old.attach_stub("stub-r13")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r13",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
            )

        fresh.attach_stub.assert_not_awaited()
