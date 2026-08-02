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
        user_identity="testuser",
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


class TestDeclaredEnvForwarding:
    """``_declared_non_secret_env`` reads the rewriter's sidecar and applies both
    filters. The sidecar path is recomputed from the PoolKey using the SAME
    helpers the rewriter writes with, so these tests also pin that round-trip."""

    @staticmethod
    def _write_sidecar(tmp_path, monkeypatch, pairs: dict, key: PoolKey) -> PoolKey:
        """Write the sidecar and return a PoolKey whose ``effective_env_hash`` is
        COHERENT with it — i.e. what a stub that read this same sidecar would
        have registered. The forwarding path enforces that equality, so tests
        must model it rather than using a placeholder hash."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sidecar_dir = env_sidecar_dir(resolve_overlay_dir())
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        path = sidecar_dir / env_sidecar_name(key.agent_name, key.server_name)
        path.write_text(json.dumps(pairs), encoding="utf-8")
        return _pool_key(
            server=key.server_name,
            agent=key.agent_name,
            env_hash=hash_effective_env(
                {str(k): str(v) for k, v in pairs.items() if k}
            ),
        )

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
        from kiro_crew.mcp_gateway.rewriter import _build_stub_entry

        return _build_stub_entry(
            stubs_dir=tmp_path / "stubs",
            server_name="demo-mcp",
            agent_name="test-agent",
            original={"command": "/bin/true", "args": [], "env": env_value},
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
    def test_defaults_to_off(self):
        from kiro_crew.config.loader import McpGatewayConfig

        assert McpGatewayConfig().forward_declared_env is False

    def test_string_false_does_not_enable_forwarding(self):
        """``bool("false")`` is True, so a hand-edited string value would
        silently ENABLE credential-adjacent forwarding. The parse must
        type-check, not coerce."""
        cfg = _load_config_from_dict({"mcp_gateway": {"forward_declared_env": "false"}})
        assert cfg.mcp_gateway.forward_declared_env is False

    def test_non_bool_values_fail_closed(self):
        for bad in ("true", 1, "yes", [], {}, None):
            cfg = _load_config_from_dict(
                {"mcp_gateway": {"forward_declared_env": bad}}
            )
            assert cfg.mcp_gateway.forward_declared_env is False, bad

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
