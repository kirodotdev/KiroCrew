"""MCP discovery: the probe cache and the probe quarantine.

A cached probe is never served for a different server identity (a name-only lookup skips
that check), and ages into ``outdated`` with its last tools; ``list_servers`` reaches that
identity check; and a server that wedges on every probe stops being spawned once its
quarantine count crosses the threshold.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _cache_probe,
    _get_cached,
    _probe_cache,
    _probe_identity,
    cached_probe_is_current,
    list_servers,
    probe_metadata,
)


class TestProbeCache:
    def setup_method(self) -> None:
        _clear_cache()

    def teardown_method(self) -> None:
        _clear_cache()

    def test_cache_miss_returns_unknown(self) -> None:
        status, tools, error, probed_at, probe_mode = _get_cached("nonexistent")
        assert status == "unknown"
        assert tools == []
        assert error == ""
        assert probed_at == 0.0
        assert probe_mode == "handshake"

    def test_cache_hit_within_ttl(self) -> None:
        server = McpServerInfo(
            name="test-srv", command="x", status="ok", tools=["t1", "t2"], error=""
        )
        # flake-ok: pre-existing wall-clock read bounding the cache stamp from below; moved verbatim
        before = time.time()
        _cache_probe(server)
        status, tools, error, probed_at, probe_mode = _get_cached("test-srv")
        assert status == "ok"
        assert tools == ["t1", "t2"]
        assert error == ""
        assert probed_at >= before
        assert probe_mode == "handshake"

    def test_cache_expired_returns_outdated_with_tools(self, monkeypatch) -> None:
        server = McpServerInfo(
            name="test-srv", command="x", status="ok", tools=["t1", "t2"], error=""
        )
        _cache_probe(server)
        # Simulate expiry by backdating probed_at
        # flake-ok: pre-existing monotonic read backdating the stamp past the TTL; moved verbatim
        _probe_cache["test-srv"].probed_at = time.monotonic() - 2000
        status, tools, error, probed_at, _mode = _get_cached("test-srv")
        assert status == "outdated"
        assert tools == ["t1", "t2"]
        assert error == ""
        # WHEN it was last true survives expiry — that is the whole value of
        # an "outdated" row.
        assert probed_at > 0

    def test_cache_expired_error_returns_outdated_without_error(self) -> None:
        # An aged failure reports no error text: the dashboard reads an error on
        # an ``outdated`` row as a probe refused before spawn, and an aged
        # failure is not one.
        server = McpServerInfo(name="test-srv", command="x", status="error", tools=[], error="boom")
        _cache_probe(server)
        # flake-ok: pre-existing monotonic read backdating the stamp past the TTL; moved verbatim
        _probe_cache["test-srv"].probed_at = time.monotonic() - 2000
        status, _tools, error, _probed_at, _mode = _get_cached("test-srv")
        assert status == "outdated"
        assert error == ""

    def test_cache_error_preserved(self) -> None:
        server = McpServerInfo(
            name="err-srv", command="x", status="error", tools=[], error="timeout"
        )
        _cache_probe(server)
        status, tools, error, _at, _mode = _get_cached("err-srv")
        assert status == "error"
        assert error == "timeout"

    def test_failed_probe_after_success_keeps_prior_tools(self) -> None:
        """A transient failure must not collapse a healthy server's tool list.

        `status`/`error` DO overwrite -- a fresh "error" should flip
        `mcp_gateway.shareability`'s `probe_ok` to False, which is the
        correct fail-closed behavior. What must survive is the server's last
        known-good shape, so a single timeout doesn't render as "0 tools".
        """
        good = McpServerInfo(
            name="flaky-srv",
            command="x",
            status="ok",
            tools=["a", "b"],
            tool_annotations=[{"readOnlyHint": True}, {}],
            capabilities={"tools": {}},
            protocol_version="2025-03-26",
        )
        _cache_probe(good)

        failed = McpServerInfo(name="flaky-srv", command="x", status="error", error="timeout")
        _cache_probe(failed)

        status, tools, error, _at, _mode = _get_cached("flaky-srv")
        assert status == "error"
        assert error == "timeout"
        assert tools == ["a", "b"]  # preserved, not collapsed to []

        meta = probe_metadata("flaky-srv")
        assert meta is not None
        assert meta.tool_annotations == [{"readOnlyHint": True}, {}]
        assert meta.capabilities == {"tools": {}}
        assert meta.protocol_version == "2025-03-26"
        # Both preserved together, never one alone -- a caller reading tool
        # metadata after this must see the same handshake's tools and
        # annotations, not a mix of stale and fresh.
        assert meta.tools == good.tools
        assert meta.tool_annotations == good.tool_annotations
        # The preserved shape keeps its original observation time, not the
        # failed probe's -- "as of" claims about the tool list must point to
        # when it was actually seen, not to the timeout that came later.
        assert meta.probed_at_wall == good.probed_at

    def test_first_ever_probe_failing_has_no_prior_to_preserve(self) -> None:
        """A server that has never succeeded gets the failure's own (empty)
        shape -- there is nothing stale to protect it from."""
        server = McpServerInfo(name="never-worked", command="x", status="error", error="refused")
        _cache_probe(server)
        status, tools, error, _at, _mode = _get_cached("never-worked")
        assert status == "error"
        assert error == "refused"
        assert tools == []

    def test_needs_auth_also_preserves_prior_shape(self) -> None:
        """needs_auth is a probe-side failure too (no token to present), not
        a success -- it must preserve prior tools the same way "error" does."""
        good = McpServerInfo(name="oauth-srv", command="x", status="ok", tools=["x", "y"])
        _cache_probe(good)
        challenged = McpServerInfo(name="oauth-srv", command="x", status="needs_auth", error="")
        _cache_probe(challenged)
        status, tools, _error, _at, _mode = _get_cached("oauth-srv")
        assert status == "needs_auth"
        assert tools == ["x", "y"]

    def test_cache_preserves_declared_probe_mode(self) -> None:
        """The in-process fallback's "declared" mode survives the cache round trip."""
        server = McpServerInfo(
            name="managed-srv", command="x", status="ok", tools=["t"], probe_mode="declared"
        )
        _cache_probe(server)
        *_rest, probe_mode = _get_cached("managed-srv")
        assert probe_mode == "declared"

    def test_edited_command_is_not_served_from_cache(self) -> None:
        """A name is not an identity: an edited command must not inherit the answer.

        The entry is reported as NOT CACHED rather than "outdated", because its
        tools are not aged evidence about this server -- they describe whatever
        the previous command pointed at.
        """
        good = McpServerInfo(name="srv", command="old-bin", status="ok", tools=["a", "b"])
        _cache_probe(good)

        edited = McpServerInfo(name="srv", command="new-bin")
        status, tools, error, at, mode = _get_cached("srv", _probe_identity(edited))
        assert status == "unknown"
        assert tools == []
        assert error == ""
        assert at == 0.0
        assert mode == "handshake"

        # Unchanged config still gets its answer -- the guard must not invalidate
        # every entry it inspects.
        status, tools, _error, _at, _mode = _get_cached("srv", _probe_identity(good))
        assert status == "ok"
        assert tools == ["a", "b"]

    def test_edited_env_is_not_served_from_cache(self) -> None:
        """Env is part of the identity, since it selects what the target does."""
        good = McpServerInfo(
            name="srv", command="bin", env={"PROFILE": "staging"}, status="ok", tools=["a"]
        )
        _cache_probe(good)
        edited = McpServerInfo(name="srv", command="bin", env={"PROFILE": "prod"})
        status, tools, _error, _at, _mode = _get_cached("srv", _probe_identity(edited))
        assert status == "unknown"
        assert tools == []

    def test_name_only_caller_keeps_previous_behavior(self) -> None:
        """Callers holding only a name pass no identity and are unaffected."""
        good = McpServerInfo(name="srv", command="bin", status="ok", tools=["a"])
        _cache_probe(good)
        status, tools, _error, _at, _mode = _get_cached("srv")
        assert status == "ok"
        assert tools == ["a"]

    def test_failed_probe_does_not_inherit_a_different_targets_tools(self) -> None:
        """The preservation path is identity-scoped, not name-scoped.

        Without this, editing a server's command and then failing to reach the
        NEW target would report the OLD target's tool list as this server's.
        """
        good = McpServerInfo(name="srv", command="old-bin", status="ok", tools=["a", "b"])
        _cache_probe(good)
        failed_new_target = McpServerInfo(
            name="srv", command="new-bin", status="error", error="timeout"
        )
        _cache_probe(failed_new_target)

        status, tools, error, _at, _mode = _get_cached("srv")
        assert status == "error"
        assert error == "timeout"
        assert tools == []  # NOT ["a", "b"]

        meta = probe_metadata("srv")
        assert meta is not None
        assert meta.tool_annotations == []
        assert meta.identity == _probe_identity(failed_new_target)

    def test_failed_probe_same_identity_still_preserves(self) -> None:
        """Regression guard: identity scoping must not defeat the preservation."""
        good = McpServerInfo(name="srv", command="bin", status="ok", tools=["a", "b"])
        _cache_probe(good)
        failed = McpServerInfo(name="srv", command="bin", status="error", error="timeout")
        _cache_probe(failed)
        status, tools, error, _at, _mode = _get_cached("srv")
        assert status == "error"
        assert error == "timeout"
        assert tools == ["a", "b"]

    def test_remote_url_edit_invalidates_but_header_value_does_not(self) -> None:
        """A rotated credential is the same endpoint; a new url is not.

        Header VALUES are deliberately outside the identity: rotating a token
        against the same server would otherwise discard a correct tool list on
        every rotation.
        """
        good = McpServerInfo(
            name="remote",
            url="https://a.example/mcp",
            headers={"Authorization": "Bearer old"},
            status="ok",
            tools=["a"],
        )
        _cache_probe(good)

        rotated = McpServerInfo(
            name="remote", url="https://a.example/mcp", headers={"Authorization": "Bearer new"}
        )
        status, tools, _error, _at, _mode = _get_cached("remote", _probe_identity(rotated))
        assert status == "ok"
        assert tools == ["a"]

        moved = McpServerInfo(
            name="remote", url="https://b.example/mcp", headers={"Authorization": "Bearer old"}
        )
        status, tools, _error, _at, _mode = _get_cached("remote", _probe_identity(moved))
        assert status == "unknown"
        assert tools == []

    def test_identity_excludes_scrubbed_secret_env_values(self) -> None:
        """Rotating a SCRUBBED secret must not invalidate an identical target.

        ``hash_effective_env`` owns which keys are secret -- today the
        ``ENV_SCRUB_PREFIXES`` set. This pins that the identity is built on top
        of that decision rather than hashing raw env, so the cache and the pool
        always agree on what counts as the same target.
        """
        a = McpServerInfo(
            name="srv", command="bin", env={"OAUTH_CLIENT_SECRET": "aaa", "MODE": "x"}
        )
        b = McpServerInfo(
            name="srv", command="bin", env={"OAUTH_CLIENT_SECRET": "bbb", "MODE": "x"}
        )
        assert _probe_identity(a) == _probe_identity(b)

    def test_identity_changes_for_a_non_scrubbed_env_value(self) -> None:
        """A non-prefixed value IS identity-relevant, and that is deliberate.

        Consequence worth stating: rotating a credential held in a key outside
        ``ENV_SCRUB_PREFIXES`` (``API_TOKEN``, say) discards a good tool list.
        That is the same trade ``PoolKey.effective_env_hash`` already makes, and
        agreeing with the pool is the point -- a cache that considered two
        configurations identical while the pool ran them as separate backends
        would serve one backend's tools as the other's.
        """
        a = McpServerInfo(name="srv", command="bin", env={"API_TOKEN": "aaa"})
        b = McpServerInfo(name="srv", command="bin", env={"API_TOKEN": "bbb"})
        assert _probe_identity(a) != _probe_identity(b)

    def test_local_and_remote_identities_never_collide(self) -> None:
        """A url row and a command row for one name are different targets."""
        local = McpServerInfo(name="srv", command="bin")
        remote = McpServerInfo(name="srv", url="https://a.example/mcp")
        assert _probe_identity(local) != _probe_identity(remote)

    def test_list_servers_merges_cache(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"my-srv": {"command": "cmd"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "x",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)

        # Before probe: unknown
        servers = list_servers()
        assert servers[0].status == "unknown"

        # Cache a probe result
        _cache_probe(McpServerInfo(name="my-srv", command="cmd", status="ok", tools=["a"]))

        # After probe: cached status and tools merged
        servers = list_servers()
        assert servers[0].status == "ok"
        assert servers[0].tools == ["a"]


class TestProbeIdentityWiring:
    """The identity check as reached THROUGH ``list_servers``.

    ``TestProbeCache`` covers ``_probe_identity`` and ``_get_cached`` directly,
    which leaves the wiring untested: with every one of those assertions green,
    dropping the ``_probe_identity`` argument at the ``_get_cached`` call site
    restores the original bug and nothing fails. These tests hold that call
    site, the ``probe_metadata`` read beside it, and the input coercion that
    running on real on-disk specs requires.
    """

    def setup_method(self) -> None:
        _clear_cache()

    def teardown_method(self) -> None:
        _clear_cache()

    @staticmethod
    def _configure(tmp_path, monkeypatch, spec: dict) -> None:
        """Point ``list_servers`` at a single-server config built from *spec*."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {"srv": spec}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (tmp_path / "absent",))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)

    def test_edited_command_is_not_served_by_list_servers(self, tmp_path, monkeypatch) -> None:
        """The user-visible bug, end to end.

        Reverted (``_get_cached(s.name)`` without the identity), this returns
        the old command's tools under the new command's row.
        """
        self._configure(tmp_path, monkeypatch, {"command": "old-bin"})
        _cache_probe(McpServerInfo(name="srv", command="old-bin", status="ok", tools=["old_tool"]))
        assert list_servers()[0].tools == ["old_tool"]

        self._configure(tmp_path, monkeypatch, {"command": "new-bin"})
        row = list_servers()[0]
        assert row.status == "unknown"
        assert row.tools == []

    def test_auth_challenge_is_not_inherited_across_a_config_edit(
        self, tmp_path, monkeypatch
    ) -> None:
        """``probe_metadata`` is a second read of the same entry, and needs the
        same gate: endpoint B must not render "sign-in required" on the
        strength of endpoint A's handshake."""
        self._configure(tmp_path, monkeypatch, {"url": "https://a.example/mcp"})
        _cache_probe(
            McpServerInfo(
                name="srv",
                url="https://a.example/mcp",
                status="needs_auth",
                auth_challenge=True,
                auth_grant_present=False,
            )
        )
        assert list_servers()[0].auth_challenge is True

        self._configure(tmp_path, monkeypatch, {"url": "https://b.example/mcp"})
        row = list_servers()[0]
        assert row.auth_challenge is False
        assert row.auth_grant_present is None

    @pytest.mark.parametrize(
        "spec",
        [
            pytest.param({"command": "bin", "env": None}, id="env-null"),
            pytest.param({"command": "bin", "args": None}, id="args-null"),
            pytest.param({"command": "bin", "args": [1, 2]}, id="args-non-string"),
            pytest.param({"command": "bin", "args": "abc"}, id="args-string"),
            pytest.param({"command": "bin", "args": 5}, id="args-non-iterable"),
            pytest.param({"command": "bin", "args": ["\ud800"]}, id="args-lone-surrogate"),
            pytest.param({"command": "bin", "env": {"N": "\udfff"}}, id="env-lone-surrogate"),
            pytest.param({"command": "bin", "env": {"\ud800": "v"}}, id="env-key-lone-surrogate"),
            pytest.param({"url": "https://a.example/\ud800"}, id="url-lone-surrogate"),
            pytest.param(
                {"url": "https://a.example", "headers": {"\ud800": "v"}},
                id="header-lone-surrogate",
            ),
            pytest.param({"command": "bin", "env": {"N": 1}}, id="env-non-string-value"),
            pytest.param({"command": None}, id="command-null"),
            pytest.param({"url": "https://a.example", "headers": None}, id="headers-null"),
            pytest.param({"url": "https://a.example", "headers": {"H": None}}, id="header-null"),
        ],
    )
    def test_a_malformed_spec_does_not_break_the_server_list(
        self, tmp_path, monkeypatch, spec
    ) -> None:
        """One bad row must fail in isolation, as ``probe_all`` guarantees.

        ``_server_from_spec`` passes on-disk JSON through unvalidated, so every
        one of these shapes reaches ``_probe_identity``. Uncoerced, each raises
        inside ``list_servers`` and takes the whole endpoint — and every other
        server's probe — down with it.
        """
        self._configure(tmp_path, monkeypatch, spec)
        rows = list_servers()
        assert [r.name for r in rows] == ["srv"]

    def test_string_args_are_fingerprinted_as_the_probe_spawns_them(self) -> None:
        """``probe_server`` spawns ``[resolved, *args]``, so a string splats into
        characters. Hashing a non-list as empty would make an edit to it
        invisible to this cache: the original bug, for that shape."""

        def ident(args: object) -> str:
            return _probe_identity(McpServerInfo(name="srv", command="bin", args=args))

        assert ident("abc") != ident("abd")
        assert ident("abc") == ident(["a", "b", "c"])
        assert ident(5) != ident(6)

    def test_distinct_lone_surrogates_fingerprint_differently(self) -> None:
        def ident(arg: str) -> str:
            return _probe_identity(McpServerInfo(name="srv", command="bin", args=[arg]))

        assert ident("\ud800") != ident("\ud801")

    def test_a_spec_with_both_url_and_command_is_fingerprinted_as_local(self) -> None:
        """Identity must dispatch the way ``probe_server`` dispatches.

        ``McpServerInfo.is_remote`` is ``bool(url) and not command``, so a spec
        carrying both is probed as LOCAL. Fingerprinting it as remote would
        ignore its command, leaving an edit to the thing actually being run
        invisible to this cache.
        """
        before = McpServerInfo(name="srv", command="old-bin", url="https://a.example/mcp")
        after = McpServerInfo(name="srv", command="new-bin", url="https://a.example/mcp")
        assert _probe_identity(before).startswith("local:")
        assert _probe_identity(before) != _probe_identity(after)

    def test_remote_identity_does_not_embed_the_url(self) -> None:
        """A url can carry a credential in its userinfo or query string, and the
        identity travels with the cached entry, so it is hashed like the local
        branch rather than stored verbatim."""
        secret = "https://user:pw@a.example/mcp?token=swordfish"
        identity = _probe_identity(McpServerInfo(name="srv", url=secret))
        assert "swordfish" not in identity
        assert "pw" not in identity
        assert identity.startswith("remote:")

    def test_cached_probe_is_current_reports_a_match(self) -> None:
        server = McpServerInfo(name="srv", command="bin", status="ok", tools=["t"])
        _cache_probe(server)
        assert cached_probe_is_current(McpServerInfo(name="srv", command="bin")) is True

    def test_cached_probe_is_current_reports_a_mismatch(self) -> None:
        _cache_probe(McpServerInfo(name="srv", command="bin", status="ok", tools=["t"]))
        assert cached_probe_is_current(McpServerInfo(name="srv", command="other")) is False

    def test_cached_probe_is_current_with_nothing_cached(self) -> None:
        """No entry is no claim to contradict, so a caller holding its own copy
        keeps its previous behaviour instead of losing the row."""
        assert cached_probe_is_current(McpServerInfo(name="srv", command="bin")) is True


class TestQuarantinedServersAreNotSpawned:
    """A server that wedges on every probe stops being spawned.

    ``PROBE_MAX_CONCURRENCY`` bounds how many probes run at once, never how many
    times a hopeless one is retried, so the thing under test is REPETITION. The
    consecutive-failure count is not kept here: ``mcp_quarantine`` already holds
    it, and these tests drive it exactly as the dashboard does — ``probe_all``,
    then ``record_verdicts`` with that round's outcome.
    """

    LIMIT = 3

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse quarantine store pin moved verbatim; no test of the class calls monkeypatch.undo()
    def _store(self, tmp_path, monkeypatch):
        from kiro_crew import mcp_quarantine
        from kiro_crew.mcp_discovery import _quarantine_warned

        monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", tmp_path / "quarantine.json")
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: self.LIMIT)
        _clear_cache()
        _quarantine_warned.clear()
        yield
        _quarantine_warned.clear()
        _clear_cache()

    @staticmethod
    def _wedged_config(tmp_path: Path, monkeypatch) -> None:
        """Point discovery at one enabled server and nothing else."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"wedged": {"command": "hangs-forever"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

    @staticmethod
    def _always_times_out(spawned: list[str]):
        """A ``probe_server`` stand-in for a server that wedges on every spawn."""

        async def fake_probe(server):
            spawned.append(server.name)
            server.status = "error"
            server.error = "probe timed out after 15s"
            _cache_probe(server)
            return server

        return fake_probe

    async def _pass(self, monkeypatch, spawned: list[str]) -> list:
        """One discovery pass, folded back into the store like the dashboard's.

        ``dashboard/handlers/mcp_probe.py`` calls ``probe_all`` and then hands
        ``_record_probe_verdicts`` EVERY row it returned -- it does not know which
        ones were spawned. This helper must do the same, or it tests a caller that
        does not exist and hides what the real one does with an unprobed row.
        """
        from kiro_crew import mcp_quarantine
        from kiro_crew.mcp_discovery import probe_all

        probed = await probe_all()
        mcp_quarantine.record_verdicts([(s.name, s.status, s.error) for s in probed])
        return probed

    @pytest.mark.asyncio
    async def test_wedged_server_leaves_the_spawn_set_at_the_threshold(
        self, tmp_path, monkeypatch
    ) -> None:
        """Five passes, three spawns: exclusion holds once the count crosses.

        Asserted against the configured threshold rather than a literal 3, so
        retuning ``agent.mcp_quarantine_after_failures`` cannot leave a test that
        only agrees with itself.
        """
        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        results = [await self._pass(monkeypatch, spawned) for _ in range(self.LIMIT + 2)]

        assert spawned == ["wedged"] * self.LIMIT
        # Excluded from the SPAWN set, not from the answer: a caller that decides
        # freshness by comparing returned names against its own cache would read a
        # dropped row as a brand-new server on every request and re-arm the very
        # fan-out this stops.
        for probed in results:
            assert [s.name for s in probed] == ["wedged"]
        # The rows that WERE probed report the failure; the excluded ones report
        # no fresh result, which is what test_an_unprobed_row_cannot_advance_the_count
        # depends on.
        assert results[self.LIMIT - 1][0].status == "error"
        assert results[-1][0].status == "outdated"

    @pytest.mark.asyncio
    async def test_an_unprobed_row_cannot_advance_the_count(self, tmp_path, monkeypatch) -> None:
        """The excluded row reports no fresh result, so it is not a verdict.

        Its caller folds every returned row back into the store, so a row still
        carrying the old ``error`` would re-count a probe that never ran: the
        count would climb with no handshake behind it, and raising the threshold
        afterwards could never release the server.
        """
        from kiro_crew import mcp_quarantine

        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        for _ in range(self.LIMIT + 3):
            probed = await self._pass(monkeypatch, spawned)

        assert probed[0].status == "outdated"
        assert probed[0].error == ""
        # Exactly the real failures, with nothing added by the passes that
        # returned the row without probing it.
        assert mcp_quarantine.state_for("wedged")["fails"] == self.LIMIT
        # And the count that governs exclusion is therefore still releasable by
        # the operator lever that reads it.
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: self.LIMIT + 1)
        spawned.clear()
        await self._pass(monkeypatch, spawned)
        assert spawned == ["wedged"]

    @pytest.mark.asyncio
    async def test_a_second_crossing_warns_again(self, tmp_path, monkeypatch, caplog) -> None:
        """Recovery then a fresh wedge is news, so it warns a second time.

        A ledger keyed by server name alone would stay silent here for the life of
        the process, which is the failure mode warn-once must not become.
        """
        from kiro_crew import mcp_quarantine
        from kiro_crew.mcp_discovery import _quarantine_warned

        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        with caplog.at_level(logging.WARNING):
            for _ in range(self.LIMIT + 1):
                await self._pass(monkeypatch, spawned)
            assert len([r for r in caplog.records if "will no longer" in r.getMessage()]) == 1

            mcp_quarantine.clear("wedged")
            # One pass to drop the cleared crossing from the ledger, proving the
            # prune keeps it bounded rather than growing one key per crossing.
            await self._pass(monkeypatch, spawned)
            assert _quarantine_warned == set()

            for _ in range(self.LIMIT + 1):
                await self._pass(monkeypatch, spawned)

        opened = [r for r in caplog.records if "will no longer" in r.getMessage()]
        assert len(opened) == 2
        assert all("wedged" in r.getMessage() for r in opened)

    @pytest.mark.asyncio
    async def test_exactly_one_warning_per_crossing(self, tmp_path, monkeypatch, caplog) -> None:
        """One warning for the whole wedge, naming both ways back out."""
        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        with caplog.at_level(logging.WARNING):
            for _ in range(self.LIMIT + 3):
                await self._pass(monkeypatch, spawned)

        opened = [r for r in caplog.records if "will no longer" in r.getMessage()]
        assert len(opened) == 1
        message = opened[0].getMessage()
        assert "wedged" in message
        # The remedy matters more than the diagnosis here: the exclusion outlives a
        # gateway restart, so the one line an operator gets has to name the reset
        # that clears it and the switch that turns the whole thing off.
        assert "/api/mcp/quarantine/clear" in message
        assert "agent.mcp_quarantine_after_failures" in message

    @pytest.mark.asyncio
    async def test_clearing_the_quarantine_restores_spawning(self, tmp_path, monkeypatch) -> None:
        """The operator reset that already ships is the reopen path."""
        from kiro_crew import mcp_quarantine

        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))
        for _ in range(self.LIMIT + 1):
            await self._pass(monkeypatch, spawned)
        assert spawned == ["wedged"] * self.LIMIT

        mcp_quarantine.clear("wedged")

        spawned.clear()
        await self._pass(monkeypatch, spawned)
        assert spawned == ["wedged"]

    @pytest.mark.asyncio
    async def test_a_successful_probe_clears_the_count(self, tmp_path, monkeypatch) -> None:
        """A success below the threshold clears the count rather than pausing it.

        Without this, two failures far apart on either side of a healthy probe
        would still add up and exclude a server that is working.
        """
        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))
        for _ in range(self.LIMIT - 1):
            await self._pass(monkeypatch, spawned)

        async def healthy(server):
            spawned.append(server.name)
            server.status = "ok"
            server.tools = ["t"]
            _cache_probe(server)
            return server

        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", healthy)
        await self._pass(monkeypatch, spawned)

        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))
        spawned.clear()
        for _ in range(self.LIMIT - 1):
            await self._pass(monkeypatch, spawned)
        assert spawned == ["wedged"] * (self.LIMIT - 1)

    @pytest.mark.asyncio
    async def test_threshold_zero_turns_exclusion_off_too(self, tmp_path, monkeypatch) -> None:
        """One switch, one meaning: ``0`` disables quarantining, badge and spawn.

        An operator who turns the feature off has to get the pre-change behaviour
        back, not a silent exclusion whose badge has been hidden.
        """
        from kiro_crew import mcp_quarantine

        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 0)
        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        for _ in range(self.LIMIT + 2):
            await self._pass(monkeypatch, spawned)

        assert spawned == ["wedged"] * (self.LIMIT + 2)

    @pytest.mark.asyncio
    async def test_a_hostile_store_record_cannot_break_discovery(
        self, tmp_path, monkeypatch
    ) -> None:
        """Any stored value must be inert on the probe path.

        The store is unfenced and its contents are attacker-influenced, and this
        read happens inside ``probe_all``: an exception here would take out the
        whole fleet's discovery, not one row. ``_as_time`` bounds NaN, infinities
        and negatives but passes a large finite value through, so nothing on this
        path may hand a stored number to an API with a platform range.
        """
        from kiro_crew import mcp_quarantine

        store = tmp_path / "quarantine.json"
        store.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": {
                        "wedged": {"fails": 10**9, "crossed_at": 1e30, "last_status": "error"}
                    },
                }
            )
        )
        monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", store)
        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        probed = await self._pass(monkeypatch, spawned)

        assert [s.name for s in probed] == ["wedged"]

    @pytest.mark.asyncio
    async def test_unreadable_store_still_probes(self, tmp_path, monkeypatch) -> None:
        """Fail OPEN: one bad file must not become a fleet-wide probe outage."""
        from kiro_crew import mcp_quarantine

        def boom():
            raise OSError("store unreadable")

        monkeypatch.setattr(mcp_quarantine, "snapshot", boom)
        self._wedged_config(tmp_path, monkeypatch)
        spawned: list[str] = []
        monkeypatch.setattr("kiro_crew.mcp_discovery.probe_server", self._always_times_out(spawned))

        for _ in range(self.LIMIT + 2):
            await self._pass(monkeypatch, spawned)

        assert spawned == ["wedged"] * (self.LIMIT + 2)
