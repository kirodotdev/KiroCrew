"""Tests for the two MCP gateway pooling gaps fixed together (issue #1078).

Part 1 — shutdown: the supervisor's SIGTERM→SIGKILL grace must cover gatewayd's
own drain budget, and the drain must wait on IN-FLIGHT REQUESTS rather than on
the (never-empty) connection set.

Part 2 — declared env: a pooled backend may receive the operator's declared
non-secret env, and must never receive a rotating-secret or credential-prefixed
key.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import gatewayd, manager
from kiro_crew.mcp_gateway.backend import HEARTBEAT_PING_ID, Backend
from kiro_crew.mcp_gateway.hashing import (
    ENV_SCRUB_PREFIXES,
    hash_effective_env,
    is_secret_env_key,
    non_secret_env,
)
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.mcp_gateway.rewriter import (
    env_sidecar_dir,
    env_sidecar_name,
    resolve_overlay_dir,
)
from kiro_crew.mcp_gateway.shutdown_budget import (
    DRAIN_SECS,
    POOL_SHUTDOWN_SECS,
    TOTAL_SHUTDOWN_BUDGET_SECS,
)


def _pool_key(
    server: str = "demo-mcp",
    agent: str = "test-agent",
    env_hash: str = "def456",
) -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name=agent,
        command_args_hash="abc123",
        effective_env_hash=env_hash,
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        config_snapshot_hash="jkl012",
    )


def _make_backend(key: PoolKey | None = None) -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    return Backend(
        pool_key=key or _pool_key(),
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )


# --- Part 1: shutdown budgets ------------------------------------------------


class TestShutdownBudget:
    def test_grace_covers_drain_and_pool_shutdown(self):
        """The regression that caused SIGKILL on every restart: the supervisor's
        grace (5.0) was SHORTER than gatewayd's drain window (10.0)."""
        assert manager._SHUTDOWN_GRACE_SECS >= DRAIN_SECS + POOL_SHUTDOWN_SECS

    def test_grace_is_derived_not_a_literal(self):
        """Guard the derivation itself: raising the drain window must raise the
        grace automatically, so the pair can never be inverted again."""
        assert manager._SHUTDOWN_GRACE_SECS == TOTAL_SHUTDOWN_BUDGET_SECS
        assert TOTAL_SHUTDOWN_BUDGET_SECS > DRAIN_SECS + POOL_SHUTDOWN_SECS

    def test_gatewayd_drain_uses_shared_budget(self):
        assert gatewayd._SHUTDOWN_DRAIN_SECS == DRAIN_SECS


class TestInFlightRequests:
    def test_idle_backend_reports_zero(self):
        assert _make_backend().outstanding_work == 0

    def test_counts_pending_forwarded_requests(self):
        backend = _make_backend()
        backend._pending_requests["1"] = MagicMock()
        backend._pending_requests["2"] = MagicMock()
        assert backend.outstanding_work == 2

    def test_heartbeat_ping_is_not_tracked_as_in_flight(self):
        """The heartbeat ping is written straight to stdin under the reserved id
        and never registered in ``_pending_requests`` — assert that invariant,
        because the drain predicate depends on it."""
        backend = _make_backend()
        assert str(HEARTBEAT_PING_ID) not in backend._pending_requests
        assert backend.outstanding_work == 0

    @pytest.mark.asyncio
    async def test_unfinished_mcp_apps_delivery_counts_as_in_flight(self):
        """MCP Apps interception CONSUMES the pending entry and then delivers the
        response from a background task (``_fetch_and_deliver_ui``). Counting
        only ``_pending_requests`` would report 0 while a response is still
        undelivered, letting shutdown cancel the connection and drop the app
        render."""
        backend = _make_backend()
        started = asyncio.Event()

        async def _slow_delivery() -> None:
            started.set()
            await asyncio.sleep(5)

        task = asyncio.create_task(_slow_delivery())
        backend._apps_tasks.add(task)
        task.add_done_callback(backend._apps_tasks.discard)
        await started.wait()
        try:
            assert not backend._pending_requests, "pending entry already consumed"
            assert backend.outstanding_work == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_queued_undelivered_frame_counts_as_outstanding(self):
        """Regression: the stdout pump pops the pending entry and ENQUEUES the
        serialised reply; the connection handler's writer task drains it onto
        the stub socket. Between those steps the reply is completed but
        undelivered — counting only ``_pending_requests`` let shutdown cancel
        the writer and lose it."""
        backend = _make_backend()
        inbox = await backend.attach_stub("stub-1")
        await inbox.put(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')

        assert not backend._pending_requests, "pending entry already consumed"
        assert backend.outstanding_work == 1

        # Once the writer drains it, the backend owes nothing.
        await inbox.get()
        assert backend.outstanding_work == 0

    @pytest.mark.asyncio
    async def test_finished_apps_task_does_not_count(self):
        backend = _make_backend()

        async def _done() -> None:
            return None

        task = asyncio.create_task(_done())
        await task
        backend._apps_tasks.add(task)
        assert backend.outstanding_work == 0


class TestDrainPredicate:
    @pytest.mark.asyncio
    async def test_attached_but_idle_backend_does_not_block_shutdown(self):
        """THE regression: a pooled stub's bridge stays attached for the life of
        the session (refcount > 0) while having nothing in flight. Draining on
        that condition burned the whole window on every restart."""
        pool = BackendPool(max_backends=4)
        key = _pool_key()
        backend = _make_backend(key)
        await pool.add(key, backend)
        await backend.attach_stub("stub-1")

        assert backend.refcount > 0, "stub is attached"
        assert gatewayd._has_outstanding_work(pool) is False

    @pytest.mark.asyncio
    async def test_in_flight_request_does_block_shutdown(self):
        pool = BackendPool(max_backends=4)
        key = _pool_key()
        backend = _make_backend(key)
        await pool.add(key, backend)
        backend._pending_requests["7"] = MagicMock()

        assert gatewayd._has_outstanding_work(pool) is True

    @pytest.mark.asyncio
    async def test_empty_pool_has_nothing_in_flight(self):
        assert gatewayd._has_outstanding_work(BackendPool(max_backends=2)) is False

    @pytest.mark.asyncio
    async def test_active_stub_write_blocks_shutdown(self):
        """Stage 4: the writer has already dequeued the frame (queue reports
        empty) but has not finished write+drain. Without this counter the drain
        exits and Phase 2 cancels the writer mid-delivery."""
        pool = BackendPool(max_backends=2)
        assert gatewayd._has_outstanding_work(pool) is False
        with gatewayd._counted_stub_write():
            assert gatewayd._has_outstanding_work(pool) is True
        assert gatewayd._has_outstanding_work(pool) is False

    def test_write_counter_is_released_on_exception(self):
        """A leaked counter would wedge EVERY later shutdown into the full drain
        window, so the decrement must survive an error."""
        before = gatewayd._active_stub_writes
        with contextlib.suppress(RuntimeError):
            with gatewayd._counted_stub_write():
                raise RuntimeError("write failed")
        assert gatewayd._active_stub_writes == before

    def test_write_counter_is_released_on_cancellation(self):
        before = gatewayd._active_stub_writes
        with contextlib.suppress(asyncio.CancelledError):
            with gatewayd._counted_stub_write():
                raise asyncio.CancelledError()
        assert gatewayd._active_stub_writes == before


# --- Part 2: declared env forwarding ----------------------------------------


class TestEffectiveEnvHash:
    def test_matches_the_pre_refactor_wire_format(self):
        """The hash moved from stub.py into hashing.py; it is a PoolKey
        dimension, so a byte-format change would silently re-partition every
        live pool. Recompute the original algorithm inline as a golden."""
        env = {"B": "2", "A": "1", "AWS_SECRET_ACCESS_KEY": "shh", "OAUTH_TOKEN": "t"}
        h = hashlib.sha256()
        for k in sorted(env):
            if any(k.startswith(p) for p in ("AWS_SECRET", "AWS_SESSION", "OAUTH")):
                continue
            h.update(k.encode("utf-8"))
            h.update(b"=")
            h.update(env[k].encode("utf-8"))
            h.update(b"\0")
        assert hash_effective_env(env) == h.hexdigest()

    def test_secret_keys_do_not_change_the_hash(self):
        """Why the hash is non-injective for secrets — and therefore why a
        secret can never be forwarded to a shared backend."""
        base = {"FLAG": "on"}
        rotated = {"FLAG": "on", "AWS_SESSION_TOKEN": "rotated-value"}
        assert hash_effective_env(base) == hash_effective_env(rotated)

    def test_scrub_prefixes_are_recognised(self):
        for prefix in ENV_SCRUB_PREFIXES:
            assert is_secret_env_key(f"{prefix}_ANYTHING")
        assert not is_secret_env_key("TOOL_PERSONALIZATION_ENABLED")

    def test_naming_a_key_makes_the_hash_injective_over_it(self):
        """The whole basis of ``mcp_gateway.pool_identity_env``.

        ``test_secret_keys_do_not_change_the_hash`` above states the problem: two
        sessions differing only in a rotating secret collide onto one hash, so
        they share a backend and no single value for that key is correct. Naming
        the key removes the collision — which is what makes forwarding it safe,
        by the same argument that already makes every other hashed key safe.
        """
        a = {"FLAG": "on", "AWS_SESSION_TOKEN": "token-a"}
        b = {"FLAG": "on", "AWS_SESSION_TOKEN": "token-b"}
        named = {"AWS_SESSION_TOKEN"}

        assert hash_effective_env(a) == hash_effective_env(b), "collides by default"
        assert hash_effective_env(a, identity_keys=named) != hash_effective_env(
            b, identity_keys=named
        ), "a named key must partition the pool by its value"

    def test_an_installation_that_names_nothing_keeps_its_pool_keys(self):
        """No cold-start wave on upgrade.

        Adding the argument must not change the hash for anyone who has not opted
        in, or every existing PoolKey would be invalidated and every backend would
        cold-start once. Naming a key the env does not declare is likewise inert.
        """
        env = {"A": "1", "OAUTH_TOKEN": "t"}
        assert hash_effective_env(env) == hash_effective_env(env, identity_keys=())
        assert hash_effective_env(env) == hash_effective_env(
            env, identity_keys={"SOME_OTHER_TOKEN"}
        )


class TestDeclaredEnvForwarding:
    """``_declared_non_secret_env`` reads the rewriter's sidecar and applies both
    filters. The sidecar path is recomputed from the PoolKey using the SAME
    helpers the rewriter writes with, so these tests also pin that round-trip."""

    @staticmethod
    def _write_sidecar(
        tmp_path, monkeypatch, pairs: dict, key: PoolKey, identity_keys=()
    ) -> PoolKey:
        """Write the sidecar and return a PoolKey whose ``effective_env_hash`` is
        COHERENT with it — i.e. what a stub that read this same sidecar would
        have registered. The forwarding path enforces that equality, so tests
        must model it rather than using a placeholder hash.

        ``identity_keys`` is the set THE STUB hashed with. Passing a set the
        daemon does not share is how a lying stub is modelled: the daemon
        recomputes under its own configured set and the equality fails."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sidecar_dir = env_sidecar_dir(resolve_overlay_dir())
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        path = sidecar_dir / env_sidecar_name(key.agent_name, key.server_name)
        path.write_text(json.dumps(pairs), encoding="utf-8")
        return _pool_key(
            server=key.server_name,
            agent=key.agent_name,
            env_hash=hash_effective_env(
                {str(k): str(v) for k, v in pairs.items() if k},
                identity_keys=identity_keys,
            ),
        )

    def test_the_eligibility_count_matches_the_forwarder_key_for_key(
        self, tmp_path, monkeypatch
    ):
        """Drift guard for the two-places-must-agree contract.

        ``_withheld_env_count`` claims to mirror
        ``gatewayd._declared_non_secret_env``. Pin it against the REAL forwarder
        rather than a reproduction of its filter chain: a filter added to the
        forwarder and not to the classifier has to fail this test, which it
        cannot do if the test re-implements the chain it is meant to police.

        Load-bearing because of the default flip: with forwarding off the
        classifier short-circuits on ``len(entry_env)`` and never consults the
        forwarder's rules, so only the ON path can drift.
        """
        from kiro_crew.mcp_gateway import gatewayd
        from kiro_crew.mcp_gateway.hashing import is_secret_env_key
        from kiro_crew.mcp_gateway.rewriter import _withheld_env_count

        declared = {
            "LOG_LEVEL": "debug",
            "REGION": "ap-southeast-2",
            # A declared PATH forwards: the rewriter already RESOLVED the command
            # through it (``spec_env_path``), so stripping it at spawn would run
            # the resolved binary under a different search path than resolution
            # assumed. The MCP probe path makes the same exemption explicitly.
            "PATH": "/opt/toolbox/bin:/usr/bin",
            # Loader/interpreter keys DO forward today. Asserted rather than
            # omitted so the policy is visible and any future change to it is a
            # deliberate edit to this test, not a silent behaviour change.
            "PYTHONPATH": "/opt/x",
            "LD_PRELOAD": "/usr/lib/legit.so",
            # Excluded from effective_env_hash -> the hash is non-injective over
            # these, so co-tenants may disagree and no single correct value
            # exists in a shared backend. Never forwarded.
            "OAUTH_TOKEN": "rotating",
            # In the hash, but the daemon scrubs credentials from its own env, so
            # forwarding must not re-introduce them.
            "AWS_ACCESS_KEY_ID": "cred",
            "SSH_AUTH_SOCK": "/tmp/sock",
        }
        forwarded = gatewayd._declared_non_secret_env(
            self._write_sidecar(
                tmp_path, monkeypatch, declared, _pool_key(env_hash="unused")
            )
        )

        withheld = _withheld_env_count(declared, True)
        assert len(forwarded) + withheld == len(declared), (
            f"forwarded={sorted(forwarded)} withheld={withheld} "
            f"declared={sorted(declared)} -- the classifier and the forwarder "
            "disagree about at least one key"
        )
        assert sorted(forwarded) == [
            "LD_PRELOAD", "LOG_LEVEL", "PATH", "PYTHONPATH", "REGION",
        ]
        assert not any(is_secret_env_key(k) for k in forwarded)

    def test_a_named_key_forwards_and_the_mirror_still_holds(
        self, tmp_path, monkeypatch
    ):
        """The opt-in path, pinned against the REAL forwarder like the mirror above.

        Both sides must consult the same resolved set. If only the forwarder
        learned about ``pool_identity_env`` the count identity below breaks, which
        is the same drift this class already guards for the default set.
        """
        from kiro_crew.mcp_gateway import gatewayd
        from kiro_crew.mcp_gateway.rewriter import _withheld_env_count

        named = frozenset({"OAUTH_TOKEN"})
        monkeypatch.setattr(gatewayd, "pool_identity_env_keys", lambda: named)

        declared = {
            "LOG_LEVEL": "debug",
            "OAUTH_TOKEN": "named-so-part-of-identity",
            # Still withheld: naming a key does not lift the daemon's own scrub.
            "AWS_ACCESS_KEY_ID": "cred",
        }
        forwarded = gatewayd._declared_non_secret_env(
            self._write_sidecar(
                tmp_path,
                monkeypatch,
                declared,
                _pool_key(env_hash="unused"),
                identity_keys=named,
            )
        )

        assert sorted(forwarded) == ["LOG_LEVEL", "OAUTH_TOKEN"]
        withheld = _withheld_env_count(declared, True, named)
        assert len(forwarded) + withheld == len(declared), (
            f"forwarded={sorted(forwarded)} withheld={withheld} "
            "-- the classifier and the forwarder disagree under an opt-in"
        )

    def test_a_credential_scrub_name_cannot_be_opted_in(self, tmp_path, monkeypatch):
        """``pool_identity_env`` does not lift ``is_credential_env_key``.

        That scrub is the broader guard and predates pooling — it keeps one
        session's credentials out of another session's backend in the per-session
        topology too. ``pool_identity_env_keys`` drops such names at the source so
        hash, forwarder and classifier cannot end up in a half-state where a key
        is in the identity but still refused, which would re-partition the pool on
        every rotation while leaving the entry unpoolable anyway.
        """
        from kiro_crew.mcp_gateway.rewriter import pool_identity_env_keys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(
            json.dumps(
                {"mcp_gateway": {"pool_identity_env": ["AWS_SECRET_KEY", "OAUTH_TOKEN"]}}
            ),
            encoding="utf-8",
        )
        assert pool_identity_env_keys() == frozenset({"OAUTH_TOKEN"})

    def test_a_stub_claiming_a_wider_set_than_the_daemon_forwards_nothing(
        self, tmp_path, monkeypatch
    ):
        """A stub cannot widen what reaches a SHARED backend.

        The stub is handed the identity set on argv so it can compute the hash,
        and stubs are untrusted clients. A stub that claims a key the operator did
        not name registers a hash the daemon does not reproduce, because the
        daemon recomputes under ITS OWN configured set — so the existing coherence
        gate refuses the whole sidecar. No new check: the gate that guards a spec
        edited mid-session guards this identically.
        """
        from kiro_crew.mcp_gateway import gatewayd

        # Daemon: nothing opted in. Stub: claims OAUTH_TOKEN is identity.
        monkeypatch.setattr(gatewayd, "pool_identity_env_keys", lambda: frozenset())
        key = self._write_sidecar(
            tmp_path,
            monkeypatch,
            {"LOG_LEVEL": "debug", "OAUTH_TOKEN": "stolen"},
            _pool_key(env_hash="unused"),
            identity_keys=frozenset({"OAUTH_TOKEN"}),
        )

        # Not "OAUTH_TOKEN was filtered out" — the whole read fails closed, so the
        # non-secret keys do not forward either. Failing closed is the point.
        assert gatewayd._declared_non_secret_env(key) == {}

    def test_one_identity_snapshot_governs_both_the_gate_and_the_filter(
        self, tmp_path, monkeypatch
    ):
        """The coherence hash and the forward filter must observe ONE snapshot.

        Reading ``pool_identity_env_keys()`` twice would make them two separate
        observations of a file an operator can edit at any moment, so the gate
        could accept a sidecar under one set while the filter applied another --
        and the wider of the two would decide what reaches a shared backend.
        Counting the reads pins the structural fix (the set is a required
        parameter of ``_declared_env_pairs``) rather than the current line order.
        """
        from kiro_crew.mcp_gateway import gatewayd

        named = frozenset({"OAUTH_TOKEN"})
        reads = {"n": 0}

        def counting() -> frozenset[str]:
            reads["n"] += 1
            return named

        monkeypatch.setattr(gatewayd, "pool_identity_env_keys", counting)
        key = self._write_sidecar(
            tmp_path,
            monkeypatch,
            {"LOG_LEVEL": "debug", "OAUTH_TOKEN": "named"},
            _pool_key(env_hash="unused"),
            identity_keys=named,
        )

        reads["n"] = 0
        assert sorted(gatewayd._declared_non_secret_env(key)) == [
            "LOG_LEVEL",
            "OAUTH_TOKEN",
        ]
        assert reads["n"] == 1, "shared path must snapshot the identity set once"

        reads["n"] = 0
        assert gatewayd._declared_env_for_private_backend(key) != {}
        assert reads["n"] == 1, "private path must snapshot the identity set once"

    def test_forwards_non_secret_declared_env(self, tmp_path, monkeypatch):
        key = _pool_key(server="builder-mcp", agent="gpu-dev")
        key = self._write_sidecar(
            tmp_path, monkeypatch, {"TOOL_PERSONALIZATION_ENABLED": "false"}, key
        )
        assert gatewayd._declared_non_secret_env(key) == {
            "TOOL_PERSONALIZATION_ENABLED": "false"
        }

    def test_drops_rotating_secret_keys(self, tmp_path, monkeypatch):
        key = _pool_key()
        key = self._write_sidecar(
            tmp_path,
            monkeypatch,
            {
                "KEEP": "yes",
                "AWS_SECRET_ACCESS_KEY": "shh",
                "AWS_SESSION_TOKEN": "shh",
                "OAUTH_CLIENT_SECRET": "shh",
            },
            key,
        )
        assert gatewayd._declared_non_secret_env(key) == {"KEEP": "yes"}

    def test_drops_credential_prefixed_keys_the_daemon_scrubs(self, tmp_path, monkeypatch):
        """These are IN the PoolKey hash (so co-tenants agree) but the daemon's
        own scrub strips them, so forwarding must not re-introduce them."""
        key = _pool_key()
        key = self._write_sidecar(
            tmp_path,
            monkeypatch,
            {
                "KEEP": "yes",
                "AWS_ACCESS_KEY_ID": "AKIA...",
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
                "GNUPGHOME": "/home/u/.gnupg",
                "GIT_ASKPASS": "/usr/bin/askpass",
            },
            key,
        )
        forwarded = gatewayd._declared_non_secret_env(key)
        assert forwarded == {"KEEP": "yes"}

    def test_every_forwarded_key_is_also_a_hashed_key(self, tmp_path, monkeypatch):
        """The invariant that makes forwarding safe: a forwarded key is part of
        ``effective_env_hash``, so every session sharing the backend agreed on
        its value."""
        declared = {
            "TOOL_PERSONALIZATION_ENABLED": "false",
            "JAVA_OPTS": "-Xmx1g,-Xms512m",
            "AWS_SECRET_ACCESS_KEY": "shh",
            "AWS_ACCESS_KEY_ID": "AKIA...",
        }
        key = _pool_key()
        key = self._write_sidecar(tmp_path, monkeypatch, declared, key)
        forwarded = gatewayd._declared_non_secret_env(key)
        hashed = non_secret_env(declared)
        assert forwarded, "precondition: something was forwarded"
        assert set(forwarded).issubset(set(hashed))
        for k in forwarded:
            assert not is_secret_env_key(k)
            assert not manager.is_credential_env_key(k)

    def test_sidecar_edited_after_the_stub_registered_is_not_forwarded(
        self, tmp_path, monkeypatch
    ):
        """COHERENCE GATE. The stub hashes the sidecar when its session starts;
        gatewayd re-reads it at cold spawn. If the operator edits
        ``mcpServers.<name>.env`` in between, a respawn under the OLD PoolKey
        would apply the NEW values — co-tenants would run under configuration
        they never declared. Forwarding must fail closed on hash mismatch."""
        key = self._write_sidecar(
            tmp_path, monkeypatch, {"TOOL_PERSONALIZATION_ENABLED": "false"}, _pool_key()
        )
        # Operator edits the spec; rewrite_agents rewrites the sidecar, but the
        # running stub keeps the PoolKey it registered with.
        sidecar = env_sidecar_dir(resolve_overlay_dir()) / env_sidecar_name(
            key.agent_name, key.server_name
        )
        sidecar.write_text(
            json.dumps({"TOOL_PERSONALIZATION_ENABLED": "true"}), encoding="utf-8"
        )
        assert gatewayd._declared_non_secret_env(key) == {}

    def test_coherent_sidecar_is_forwarded(self, tmp_path, monkeypatch):
        """Positive control for the gate: an untouched sidecar still forwards, so
        the check cannot be passing merely by rejecting everything."""
        key = self._write_sidecar(
            tmp_path, monkeypatch, {"TOOL_PERSONALIZATION_ENABLED": "false"}, _pool_key()
        )
        assert gatewayd._declared_non_secret_env(key) == {
            "TOOL_PERSONALIZATION_ENABLED": "false"
        }

    def test_placeholder_hash_is_rejected(self, tmp_path, monkeypatch):
        """A PoolKey whose hash was never derived from this sidecar must not
        forward — this is the shape of the bug the gate closes."""
        self._write_sidecar(
            tmp_path, monkeypatch, {"KEEP": "yes"}, _pool_key()
        )
        stale = _pool_key(env_hash="not-the-real-hash")
        assert gatewayd._declared_non_secret_env(stale) == {}

    def test_missing_sidecar_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert gatewayd._declared_non_secret_env(_pool_key()) == {}

    def test_malformed_sidecar_is_ignored(self, tmp_path, monkeypatch):
        key = _pool_key()
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sidecar_dir = env_sidecar_dir(resolve_overlay_dir())
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / env_sidecar_name(key.agent_name, key.server_name)).write_text(
            "{not json", encoding="utf-8"
        )
        assert gatewayd._declared_non_secret_env(key) == {}

    def test_non_object_sidecar_is_ignored(self, tmp_path, monkeypatch):
        key = _pool_key()
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sidecar_dir = env_sidecar_dir(resolve_overlay_dir())
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / env_sidecar_name(key.agent_name, key.server_name)).write_text(
            '["a", "b"]', encoding="utf-8"
        )
        assert gatewayd._declared_non_secret_env(key) == {}


class TestSidecarNaming:
    def test_components_cannot_collide_across_the_boundary(self):
        """``agent-a`` + ``server-b.c`` must not land on the same file as
        ``agent-a.b`` + ``server-c``."""
        assert env_sidecar_name("agent-a", "server-b.c") != env_sidecar_name(
            "agent-a.b", "server-c"
        )

    def test_lossy_sanitization_cannot_collide_within_a_component(self):
        """Regression: sanitization maps both ``foo.bar`` and ``foo_bar`` to
        ``foo_bar``, so without the digest suffix two servers declared by ONE
        agent shared a sidecar and the second write handed the first server the
        wrong environment."""
        a = env_sidecar_name("agent", "foo.bar")
        b = env_sidecar_name("agent", "foo_bar")
        assert a != b
        assert a.startswith("agent.foo_bar.") and b.startswith("agent.foo_bar.")

    def test_name_is_deterministic(self):
        """Writer and reader recompute the name independently, so it must be a
        pure function of the raw components."""
        assert env_sidecar_name("gpu-dev", "builder-mcp") == env_sidecar_name(
            "gpu-dev", "builder-mcp"
        )

    def test_readable_components_are_preserved_and_sanitized(self):
        name = env_sidecar_name("a.b", "c")
        assert name.startswith("a_b.c.")
        assert name.endswith(".json")

    def test_sidecar_dir_is_a_sibling_of_the_agents_overlay(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        overlay = resolve_overlay_dir()
        assert env_sidecar_dir(overlay) == overlay.parent / "stubs" / "env"


def _load_config_from_dict(data: object):
    """Write ``data`` to a temp config file and load it through the real
    ``KiroCrewConfig.load()`` parse path (mirrors ``test_config_loader``)."""
    import tempfile
    import unittest.mock
    from pathlib import Path

    from kiro_crew.config.loader import KiroCrewConfig

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path", return_value=tmp
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestMalformedDeclaredEnv:
    """``~/.kiro/agents/*.json`` is hand-editable, so ``env`` can parse as a
    non-dict. The secret-key scan calls ``str.startswith`` on every key, so a
    list would raise out of ``_build_stub_entry`` and abort the ENTIRE rewrite
    pass — disabling pooling for every agent because one spec was malformed."""

    @staticmethod
    def _build(tmp_path, env_value):
        from kiro_crew.mcp_gateway.rewriter import _build_stub_entry, _normalized_env

        original = {"command": "/bin/true", "args": [], "env": env_value}
        return _build_stub_entry(
            stubs_dir=tmp_path / "stubs",
            server_name="demo-mcp",
            agent_name="test-agent",
            original=original,
            env_pairs=_normalized_env(original, context="server 'demo-mcp'"),
            target_command="/bin/true",
            socket_path=tmp_path / "gateway.sock",
            work_dir=tmp_path,
            sandbox_mode="none",
            approval_mode="reads",
        )

    @pytest.mark.parametrize("bad_env", [[{}], "x", 5, [{"A": "1"}]])
    def test_non_dict_env_does_not_raise(self, tmp_path, bad_env):
        entry = self._build(tmp_path, bad_env)
        assert entry["env"] == {}
        # A malformed env yields no --env-file: nothing to hash or apply.
        assert "--env-file" not in entry["args"]

    def test_valid_env_still_produces_a_sidecar(self, tmp_path):
        entry = self._build(tmp_path, {"TOOL_PERSONALIZATION_ENABLED": "false"})
        assert "--env-file" in entry["args"]


class TestForwardDeclaredEnvFlag:
    def test_defaults_to_on(self):
        """Absence means forward.

        The forwarded set is a strict subset of the hashed set and gatewayd
        re-hashes the sidecar at spawn, forwarding nothing on mismatch -- so a
        forwarded key is one every co-tenant of that backend declared
        identically. With the flag off, declaring a single ordinary key costs the
        whole server its pooling, on the strength of a disagreement that check
        has already ruled out.
        """
        from kiro_crew.config.loader import McpGatewayConfig

        assert McpGatewayConfig().forward_declared_env is True

    def test_an_absent_key_takes_the_on_default(self):
        cfg = _load_config_from_dict({"mcp_gateway": {}})
        assert cfg.mcp_gateway.forward_declared_env is True

    def test_string_false_is_not_read_as_a_boolean(self):
        """``bool("false")`` is True, so a hand-edited string must not decide this.

        The schema type-checks before the parse and strips a non-boolean, so the
        dataclass default applies. With the default ON that means a typo now
        resolves to forwarding rather than to not-forwarding -- a deliberate
        consequence of the flip, and a safe one: the forwarded set is a strict
        subset of the hashed set and the spawn-time hash check refuses anything
        the co-tenants did not agree on. What a typo can no longer do is silently
        cost a server its pooling.
        """
        cfg = _load_config_from_dict({"mcp_gateway": {"forward_declared_env": "false"}})
        assert cfg.mcp_gateway.forward_declared_env is True

    def test_non_bool_values_resolve_to_the_default(self):
        for bad in ("true", "false", 1, 0, "yes", [], {}, None):
            cfg = _load_config_from_dict(
                {"mcp_gateway": {"forward_declared_env": bad}}
            )
            assert cfg.mcp_gateway.forward_declared_env is True, bad

    def test_a_real_false_still_turns_it_off(self):
        """The escape hatch for a server that must not share a backend."""
        cfg = _load_config_from_dict({"mcp_gateway": {"forward_declared_env": False}})
        assert cfg.mcp_gateway.forward_declared_env is False

    def test_real_true_still_enables(self):
        cfg = _load_config_from_dict({"mcp_gateway": {"forward_declared_env": True}})
        assert cfg.mcp_gateway.forward_declared_env is True

    def test_forward_helper_returns_nothing_when_flag_is_off(
        self, tmp_path, monkeypatch
    ):
        """The combined off-loop helper must not forward anything while the flag
        is off, even with a perfectly readable sidecar present."""
        key = _pool_key()
        key = TestDeclaredEnvForwarding._write_sidecar(
            tmp_path, monkeypatch, {"TOOL_PERSONALIZATION_ENABLED": "false"}, key
        )
        monkeypatch.setattr(gatewayd, "forward_declared_env_enabled", lambda: False)
        assert gatewayd._declared_env_to_forward(key) == {}

    def test_forward_helper_reads_sidecar_when_flag_is_on(self, tmp_path, monkeypatch):
        key = _pool_key()
        key = TestDeclaredEnvForwarding._write_sidecar(
            tmp_path, monkeypatch, {"TOOL_PERSONALIZATION_ENABLED": "false"}, key
        )
        monkeypatch.setattr(gatewayd, "forward_declared_env_enabled", lambda: True)
        assert gatewayd._declared_env_to_forward(key) == {
            "TOOL_PERSONALIZATION_ENABLED": "false"
        }


class TestPrivateBackendDeclaredEnv:
    """A connection-private backend gets its declared env in full.

    Both filters the pooled path applies exist for co-tenancy: no single value is
    correct when several sessions share one process. A private backend has one
    stub, so the declaring session and the only consuming session are the same
    one -- and withholding the env would be a regression, since the same server
    spawned without a gateway receives it from the agent runtime.
    """

    def test_forwards_declared_env_with_the_flag_off(self, tmp_path, monkeypatch):
        """The flag governs the co-tenancy hazard, which does not exist here."""
        key = _pool_key(server="builder-mcp", agent="gpu-dev")
        key = TestDeclaredEnvForwarding._write_sidecar(
            tmp_path, monkeypatch, {"TOOL_PERSONALIZATION_ENABLED": "false"}, key
        )
        monkeypatch.setattr(gatewayd, "forward_declared_env_enabled", lambda: False)

        assert gatewayd._declared_env_for_private_backend(key) == {
            "TOOL_PERSONALIZATION_ENABLED": "false"
        }
        # The pooled path is unchanged and still withholds it.
        assert gatewayd._declared_env_to_forward(key) == {}

    def test_forwards_secret_bearing_keys_the_pooled_path_drops(
        self, tmp_path, monkeypatch
    ):
        """The case that breaks servers: a token the server needs to start.

        The pooled path drops it because co-tenants may hold different values.
        For a private backend there is no other holder, and dropping it leaves
        the server unable to authenticate.
        """
        pairs = {
            "AWS_SECRET_ACCESS_KEY": "shh",
            "OAUTH_CLIENT_SECRET": "shh",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "REGION": "us-west-2",
        }
        key = _pool_key(server="gh-mcp", agent="dev")
        key = TestDeclaredEnvForwarding._write_sidecar(
            tmp_path, monkeypatch, pairs, key
        )
        monkeypatch.setattr(gatewayd, "forward_declared_env_enabled", lambda: True)

        assert gatewayd._declared_env_for_private_backend(key) == pairs
        # The pooled path keeps only the key every co-tenant agrees on.
        assert gatewayd._declared_env_to_forward(key) == {"REGION": "us-west-2"}

    def test_incoherent_sidecar_still_yields_nothing(self, tmp_path, monkeypatch):
        """The coherence gate is not a co-tenancy filter and still applies: a
        spec edited after this session started must not reach the backend under a
        hash the running stub never registered.
        """
        key = _pool_key(server="builder-mcp", agent="gpu-dev")
        key = TestDeclaredEnvForwarding._write_sidecar(
            tmp_path, monkeypatch, {"A": "1"}, key
        )
        sidecar = env_sidecar_dir(resolve_overlay_dir()) / env_sidecar_name(
            key.agent_name, key.server_name
        )
        sidecar.write_text(json.dumps({"A": "2"}), encoding="utf-8")

        assert gatewayd._declared_env_for_private_backend(key) == {}
