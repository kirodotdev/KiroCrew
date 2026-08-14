"""Local verdict cache + the pre-flight that provokes sharing hazards early."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.mcp_gateway import preflight as pf
from kiro_crew.mcp_gateway import verdict_cache as vc

# The identities the pre-flight really sends, not copies of them. A double keyed
# by hardcoded names answers nothing when a name changes, and the pre-flight then
# reads as "server did not respond" while every test still describes a healthy
# server.
_ID_A, _ID_B = pf.PREFLIGHT_IDENTITY_NAMES


def _ident(**over: Any) -> vc.Identity:
    base = {
        "command_args_hash": "cmd1",
        "env_hash": "env1",
        "binary_version": "1.0.0",
    }
    base.update(over)
    return vc.Identity(**base)  # type: ignore[arg-type]


def _verdict(ran: bool = True, caller_sensitive: bool = False) -> vc.CachedPreflight:
    return vc.CachedPreflight(
        ran=ran,
        caller_sensitive=caller_sensitive,
        reasons=() if ran else (pf.REASON_PREFLIGHT_UNAVAILABLE,),
        evaluated_at=1.0,
    )


class TestIdentityInvalidation:
    """The identity is the whole point: a stale hit is a wrong answer, not a slow one."""

    def test_round_trip(self, tmp_path) -> None:
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put("srv", _ident(), _verdict(ran=True, caller_sensitive=True))
        cache.flush()

        fresh = vc.load_cache(tmp_path)
        hit = fresh.get("srv", _ident())
        assert hit is not None and hit.caller_sensitive is True

    @pytest.mark.parametrize(
        "changed",
        [
            {"command_args_hash": "cmd2"},
            {"env_hash": "env2"},
            {"binary_version": "1.0.1"},
            {"schema": vc.SCHEMA + 1},
        ],
    )
    def test_any_identity_change_misses(self, tmp_path, changed: dict[str, Any]) -> None:
        """Upgrading the MCP, editing its env, or shipping a smarter pre-flight
        must all re-derive rather than inherit."""
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put("srv", _ident(), _verdict())
        assert cache.get("srv", _ident(**changed)) is None

    def test_a_different_name_is_a_different_row(self, tmp_path) -> None:
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put("srv", _ident(), _verdict())
        assert cache.get("other", _ident()) is None

    def test_one_server_keeps_exactly_one_row(self, tmp_path) -> None:
        """The reason the file needs no size cap and no eviction policy.

        Keying by identity made every command edit and every binary upgrade add a
        row, so the file grew with config churn and needed a ceiling, an eviction
        rule, and a newest-wins rule for readers who only know a name. Overwriting
        one row per server removes all three: row count is bounded by the config.
        """
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        for i in range(10):
            cache.put("srv", _ident(binary_version=f"v{i}"), _verdict())
        cache.flush()

        assert len(vc.load_cache(tmp_path)) == 1
        # The surviving row is the newest measurement, and the superseded
        # identities are gone rather than kept as history to sort through.
        assert vc.load_cache(tmp_path).get("srv", _ident(binary_version="v9")) is not None
        assert vc.load_cache(tmp_path).get("srv", _ident(binary_version="v0")) is None


class TestCacheDegradesSafely:
    def test_absent_file_is_empty(self, tmp_path) -> None:
        assert len(vc.load_cache(tmp_path)) == 0

    def test_corrupt_file_is_empty(self, tmp_path) -> None:
        vc.cache_path(tmp_path).write_text("{{{", encoding="utf-8")
        assert len(vc.load_cache(tmp_path)) == 0

    def test_invalid_utf8_is_empty_rather_than_an_exception(self, tmp_path) -> None:
        """A single bad byte must degrade, not propagate.

        ``read_text`` raises ``UnicodeDecodeError`` before ``json.loads`` ever
        sees the bytes, and that is a ``ValueError`` — NOT a ``JSONDecodeError``
        — so catching only the two obvious clauses let it escape. This loader's
        whole contract is that unreadable reads as unevaluated.
        """
        vc.cache_path(tmp_path).write_bytes(b'{"entries": {"a\xff\xfe": {}}}')
        assert len(vc.load_cache(tmp_path)) == 0

    def test_invalid_utf8_in_the_ledger_does_not_break_daemon_startup(
        self, tmp_path
    ) -> None:
        """The same gap, on the path that decides whether gatewayd can bind.

        ``install_sink`` loads this file during startup, so an escaping decode
        error is not a degraded dashboard row — it is a daemon that never binds.
        """
        from kiro_crew.mcp_gateway import hazards

        (tmp_path / hazards.HAZARDS_FILENAME).write_bytes(
            b'{"schema": 1, "servers": {"a\xff\xfe": {}}}'
        )
        ledger = hazards.load_ledger(tmp_path)
        assert ledger.codes_for_name("a") == ()

    def test_entry_that_cannot_say_whether_it_ran_is_dropped(self, tmp_path) -> None:
        vc.cache_path(tmp_path).write_text(
            json.dumps({"entries": {"k": {"reasons": ["x"]}}}), encoding="utf-8"
        )
        assert len(vc.load_cache(tmp_path)) == 0

    def test_flush_is_a_no_op_when_clean(self, tmp_path) -> None:
        vc.VerdictCache(vc.cache_path(tmp_path)).flush()
        assert not vc.cache_path(tmp_path).exists()


class _FakeProbe:
    """Stands in for ``probe_server``, answering per clientInfo name.

    Mutates the passed server the way the real probe does, so the pre-flight is
    exercised through the same interface it uses in production.
    """

    def __init__(self, answers: dict[str, tuple[str, dict[str, Any] | None]]) -> None:
        self.answers = answers
        self.identities: list[str] = []

    async def __call__(
        self, server: McpServerInfo, *, client_info: dict[str, str] | None = None
    ) -> McpServerInfo:
        name = (client_info or {}).get("name", "default")
        self.identities.append(name)
        status, caps = self.answers[name]
        server.status = status
        server.capabilities = caps
        if status != "ok":
            server.error = "boom"
        return server


@pytest.fixture
def patch_probe(monkeypatch: pytest.MonkeyPatch):
    def _install(answers: dict[str, tuple[str, dict[str, Any] | None]]) -> _FakeProbe:
        fake = _FakeProbe(answers)
        # Patch the CONSUMER namespace: preflight imports probe_server at module
        # scope, so it holds its own reference and patching the source module
        # would leave the real prober in place — the test would pass while
        # spawning nothing, or spawn for real.
        import kiro_crew.mcp_gateway.preflight as pf_mod

        monkeypatch.setattr(pf_mod, "probe_server", fake)
        return fake

    return _install


def _server() -> McpServerInfo:
    return McpServerInfo(name="srv", command="/bin/true")


class TestEvaluateOnlyWhatChanged:
    """The orchestration policy: pay for a measurement once, per identity."""

    @pytest.mark.asyncio
    async def test_cached_identity_is_not_re_provoked(self, patch_probe, tmp_path) -> None:
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        server = _server()

        first = await ev.evaluate_new_servers([server], tmp_path)
        assert set(first) == {"srv"}
        spawns_after_first = len(fake.identities)
        assert spawns_after_first == 2, "a fresh server costs exactly two spawns"

        second = await ev.evaluate_new_servers([_server()], tmp_path)
        assert set(second) == {"srv"}
        assert len(fake.identities) == spawns_after_first, "cache hit must not spawn"

    @pytest.mark.asyncio
    async def test_changed_command_is_re_provoked(self, patch_probe, tmp_path) -> None:
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        await ev.evaluate_new_servers([_server()], tmp_path)
        before = len(fake.identities)

        upgraded = McpServerInfo(name="srv", command="/bin/true", args=["--v2"])
        await ev.evaluate_new_servers([upgraded], tmp_path)
        assert len(fake.identities) > before, "an upgraded MCP must be re-measured"

    @pytest.mark.asyncio
    async def test_the_pass_overlaps_its_preflights_within_the_shared_cap(
        self, tmp_path, monkeypatch
    ) -> None:
        """An operator waits on this pass, so it must not be a serial walk.

        Each pre-flight is two spawns that can each hit the probe timeout, so run
        serially the budget costs twice that many timeouts of dead wait and one
        hung server makes the whole pass feel hung. The ceiling is the prober's
        own constant, because the same executor sits underneath.
        """
        import asyncio

        from kiro_crew.mcp_discovery import PROBE_MAX_CONCURRENCY
        from kiro_crew.mcp_gateway import evaluate as ev

        live = 0
        peak = 0

        async def slow_preflight(server):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                await asyncio.sleep(0.05)
                return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())
            finally:
                live -= 1

        # Patch the CONSUMER namespace: evaluate holds its own reference.
        monkeypatch.setattr(ev, "preflight", slow_preflight)

        servers = [
            McpServerInfo(name=f"srv{i}", command="/bin/true")
            for i in range(ev.MAX_EVALUATIONS_PER_PASS)
        ]
        known = await ev.evaluate_new_servers(servers, tmp_path)

        assert len(known) == len(servers), "every server still gets a verdict"
        assert peak > 1, "the pre-flights ran one at a time"
        assert peak <= PROBE_MAX_CONCURRENCY, f"fan-out exceeded the shared cap: {peak}"

    @pytest.mark.asyncio
    async def test_an_unavailable_server_is_re_provoked_next_pass(
        self, patch_probe, tmp_path
    ) -> None:
        """A pre-flight that could not run says nothing about the server.

        A missing credential, an unreachable tunnel, a binary mid-install: none
        of those change the execution identity, so caching the failure against it
        would freeze the server at ``unknown`` for good. It must cost the spawns
        again rather than become permanently unevaluated.
        """
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe(
            {_ID_A: ("error", None), _ID_B: ("error", None)}
        )

        first = await ev.evaluate_new_servers([_server()], tmp_path)
        assert first["srv"].ran is False, "the unavailable verdict is still reported"
        spawns = len(fake.identities)
        assert spawns > 0

        await ev.evaluate_new_servers([_server()], tmp_path)

        assert len(fake.identities) > spawns, "an unavailable result must not be cached"

    @pytest.mark.asyncio
    async def test_a_successful_verdict_is_still_cached(self, patch_probe, tmp_path) -> None:
        """The other side of the rule: only failure is exempt from caching."""
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        await ev.evaluate_new_servers([_server()], tmp_path)
        spawns = len(fake.identities)

        await ev.evaluate_new_servers([_server()], tmp_path)

        assert len(fake.identities) == spawns

    @pytest.mark.asyncio
    async def test_disabled_server_is_never_spawned(self, patch_probe, tmp_path) -> None:
        """Probing IS the act consent gates; a disabled row must not be provoked."""
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        server = _server()
        server.disabled = True
        known = await ev.evaluate_new_servers([server], tmp_path)
        assert known == {}
        assert fake.identities == []

    @pytest.mark.asyncio
    async def test_pass_budget_is_respected(self, patch_probe, tmp_path) -> None:
        """Twenty newly added MCPs must not cost forty spawns in one request."""
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        servers = [McpServerInfo(name=f"s{i}", command="/bin/true") for i in range(20)]
        known = await ev.evaluate_new_servers(servers, tmp_path)
        assert len(known) == ev.MAX_EVALUATIONS_PER_PASS
        assert len(fake.identities) == 2 * ev.MAX_EVALUATIONS_PER_PASS

    def test_the_budget_value_itself_is_pinned(self) -> None:
        """The number is a product decision, not an implementation detail.

        Two servers per pass means four short-lived processes per probe, and a
        machine with twenty MCPs covers them over ten probes. Asserting only
        against the constant cannot catch a change to it — the expectation moves
        with the value — so the intended outcome is spelled out here.
        """
        from kiro_crew.mcp_gateway import evaluate as ev

        assert ev.MAX_EVALUATIONS_PER_PASS == 2
        assert ev.MAX_EVALUATIONS_PER_PASS * 2 == 4, "processes spawned per full pass"
        assert -(-20 // ev.MAX_EVALUATIONS_PER_PASS) == 10, "twenty MCPs in ten probes"

    @pytest.mark.asyncio
    async def test_a_server_absent_from_the_pass_keeps_its_measurement(
        self, patch_probe, tmp_path
    ) -> None:
        """Absence from the inventory is not evidence that a verdict is dead.

        The only caller gets its list from ``probe_all``, which excludes
        consent-disabled rows by design — so deleting entries that are missing
        from it would throw away the still-valid measurement of every disabled
        server, two spawns each, and make it read as unknown again the moment it
        is re-enabled.
        """
        from kiro_crew.mcp_gateway import evaluate as ev

        patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        await ev.evaluate_new_servers([_server()], tmp_path)
        assert len(vc.load_cache(tmp_path)) == 1

        await ev.evaluate_new_servers([], tmp_path)

        assert len(vc.load_cache(tmp_path)) == 1, "an absent server lost its verdict"

    @pytest.mark.asyncio
    async def test_the_file_tracks_the_config_not_the_history(
        self, patch_probe, tmp_path
    ) -> None:
        """Why no size cap and no eviction policy are needed.

        A pass overwrites one row per server it measured and touches nothing else,
        so the row count follows the number of configured servers rather than the
        number of times any of them changed.
        """
        from kiro_crew.mcp_gateway import evaluate as ev

        patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        for _ in range(5):
            await ev.evaluate_new_servers([_server()], tmp_path)

        after = vc.load_cache(tmp_path)
        assert len(after) == 1, "repeated passes over one server kept one row"
        assert after.server_names() == {"srv"}

    def test_an_in_place_binary_upgrade_is_re_measured(self, tmp_path) -> None:
        """Same path, same args, new bytes — the measurement must not be reused.

        Without a binary fingerprint the key hits and the pre-flight never re-runs,
        so a binary that BECAME caller-sensitive would hand its first caller's
        ``initialize`` result to every co-tenant. The hazard ledger only fires
        after a session has already lost its tools.

        Synchronous on purpose: ``identity_for`` refuses to run on the event
        loop, and production reaches it through ``asyncio.to_thread``.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for

        exe = tmp_path / "server-bin"
        exe.write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
        exe.chmod(0o755)
        srv = McpServerInfo(name="s", command=str(exe))
        before = identity_for(srv).as_str()

        exe.write_text("#!/bin/sh\necho v2-different-bytes\n", encoding="utf-8")
        after = identity_for(srv).as_str()

        assert before != after, "an in-place upgrade reused the old measurement"

    def test_editing_the_script_an_interpreter_runs_re_measures(self, tmp_path) -> None:
        """Most MCP servers are ``python server.py``, not a compiled binary.

        Fingerprinting only ``command`` identifies the interpreter, which does not
        change when the server's own code is edited in place — so the cache would
        hit for ever and a server that BECAME caller-sensitive would keep its
        clean verdict.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for

        script = tmp_path / "server.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        srv = McpServerInfo(name="s", command="/bin/sh", args=[str(script)])
        before = identity_for(srv).as_str()

        script.write_text("print('v2-different-bytes')\n", encoding="utf-8")
        after = identity_for(srv).as_str()

        assert before != after, "editing the script reused the old measurement"

    def test_non_file_arguments_do_not_make_the_key_unstable(self, tmp_path) -> None:
        """A flag or a port must not be treated as a file to fingerprint.

        Otherwise the key would change between passes for a server whose argv
        merely looks path-like, and every pass would re-spawn it.
        """
        from kiro_crew.mcp_gateway.evaluate import identity_for

        srv = McpServerInfo(
            name="s", command="/bin/sh", args=["--port", "8080", "/nope/missing.py"]
        )
        assert identity_for(srv).as_str() == identity_for(srv).as_str()

    def test_env_is_hashed_by_the_same_helper_the_pool_uses(self, tmp_path) -> None:
        """So a rotating credential does not look like a different server here."""
        from kiro_crew.mcp_gateway.evaluate import identity_for

        a = McpServerInfo(name="s", command="/bin/true", env={"AWS_SECRET_ACCESS_KEY": "one"})
        b = McpServerInfo(name="s", command="/bin/true", env={"AWS_SECRET_ACCESS_KEY": "two"})
        assert identity_for(a).as_str() == identity_for(b).as_str()

        c = McpServerInfo(name="s", command="/bin/true", env={"REGION": "us-west-2"})
        assert identity_for(a).as_str() != identity_for(c).as_str()


class TestPreflight:
    @pytest.mark.asyncio
    async def test_identical_capabilities_pass(self, patch_probe) -> None:
        caps = {"tools": {"listChanged": True}}
        fake = patch_probe({_ID_A: ("ok", caps), _ID_B: ("ok", caps)})
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive
        assert result.reasons == ()
        # Two DIFFERENT identities, or the check proves nothing.
        assert fake.identities == [_ID_A, _ID_B]

    @pytest.mark.asyncio
    async def test_divergent_capabilities_are_caught(self, patch_probe) -> None:
        patch_probe(
            {
                _ID_A: ("ok", {"tools": {}}),
                _ID_B: ("ok", {"tools": {}, "resources": {"subscribe": True}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        assert result.reasons == (pf.REASON_CALLER_SENSITIVE_INIT,)

    @pytest.mark.asyncio
    async def test_free_form_values_do_not_count_as_divergence(self, patch_probe) -> None:
        """A build id or session token in ``experimental`` is not caller sensitivity.

        Comparing raw dicts would flag every such server and make the check
        useless, so only the SHAPE is compared.
        """
        patch_probe(
            {
                _ID_A: ("ok", {"experimental": {"buildId": "abc"}}),
                _ID_B: ("ok", {"experimental": {"buildId": "zzz"}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive

    @pytest.mark.asyncio
    async def test_a_flipped_boolean_flag_does_count(self, patch_probe) -> None:
        """Flags ARE part of the contract a pooled backend must keep identical."""
        patch_probe(
            {
                _ID_A: ("ok", {"resources": {"subscribe": True}}),
                _ID_B: ("ok", {"resources": {"subscribe": False}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.caller_sensitive

    @pytest.mark.asyncio
    async def test_unstartable_server_is_not_a_failure(self, patch_probe) -> None:
        """"Could not ask" must never collapse into "answered no".

        A server needing a credential this host lacks would otherwise be marked
        unshareable for ever.
        """
        patch_probe({_ID_A: ("error", None), _ID_B: ("ok", {})})
        result = await pf.preflight(_server())
        assert result.ran is False
        assert result.caller_sensitive is False
        assert result.reasons == (pf.REASON_PREFLIGHT_UNAVAILABLE,)

    @pytest.mark.asyncio
    async def test_answering_once_but_not_twice_is_also_unavailable(self, patch_probe) -> None:
        patch_probe({_ID_A: ("ok", {}), _ID_B: ("error", None)})
        result = await pf.preflight(_server())
        assert result.ran is False

    @pytest.mark.asyncio
    async def test_the_caller_s_server_object_is_never_mutated(self, patch_probe) -> None:
        """The dashboard is showing this object; a pre-flight must not touch it."""
        patch_probe({_ID_A: ("ok", {}), _ID_B: ("ok", {})})
        server = _server()
        server.status = "unknown"
        server.tools = ["kept"]
        await pf.preflight(server)
        assert server.status == "unknown"
        assert server.tools == ["kept"]
        assert server.capabilities is None
