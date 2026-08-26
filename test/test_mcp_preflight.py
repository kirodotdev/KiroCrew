"""Local verdict cache + the pre-flight that provokes sharing hazards early."""

from __future__ import annotations

import asyncio
import json
import logging
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


class TestSchemaTracksTheComparedSurface:
    def test_widening_the_compared_surface_forces_a_schema_bump(self) -> None:
        """A stored row means "passed the checks THIS schema ran", nothing wider.

        The facet set IS what the pre-flight compares, so a row measured under a
        narrower set must never be read as having passed a wider one. Binding the
        two constants in one assertion is what makes that impossible to forget:
        adding a facet fails here until the schema moves with it.
        """
        assert (pf.FACET_INIT, pf.FACET_TOOLS) == ("initialize", "tools")
        assert vc.SCHEMA == 2


class TestReportedVersionInvalidation:
    """The blind spot the launch fingerprint cannot see, and its no-flap rule.

    A runtime-resolved launch (``npx some-server@latest``) keeps a byte-identical
    command, env and interpreter fingerprint while the code behind it is replaced.
    The version the server reports is the only signal that survives that, so it is
    allowed to invalidate — but only when both sides know one, or a single failed
    probe would discard a good measurement.
    """

    def test_the_same_version_still_hits(self, tmp_path) -> None:
        cache = vc.VerdictCache(tmp_path / "v.json")
        ident = _ident()
        cache.put(
            "srv",
            ident,
            vc.CachedPreflight(
                ran=True, caller_sensitive=False, reasons=(), evaluated_at=1.0,
                reported_version="1.2.3",
            ),
        )
        assert cache.get("srv", ident, "1.2.3") is not None

    def test_a_changed_version_misses_on_an_unchanged_fingerprint(self, tmp_path) -> None:
        """The npx case: identity identical, code replaced upstream."""
        cache = vc.VerdictCache(tmp_path / "v.json")
        ident = _ident()
        cache.put(
            "srv",
            ident,
            vc.CachedPreflight(
                ran=True, caller_sensitive=False, reasons=(), evaluated_at=1.0,
                reported_version="1.2.3",
            ),
        )
        assert cache.get("srv", ident, "1.2.4") is None

    def test_an_unknown_current_version_does_not_discard_the_row(self, tmp_path) -> None:
        """A probe that could not run is no information, not a mismatch."""
        cache = vc.VerdictCache(tmp_path / "v.json")
        ident = _ident()
        cache.put(
            "srv",
            ident,
            vc.CachedPreflight(
                ran=True, caller_sensitive=False, reasons=(), evaluated_at=1.0,
                reported_version="1.2.3",
            ),
        )
        assert cache.get("srv", ident, "") is not None

    def test_a_row_stored_without_a_version_is_not_discarded(self, tmp_path) -> None:
        """Rows written before versions were recorded stay usable."""
        cache = vc.VerdictCache(tmp_path / "v.json")
        ident = _ident()
        cache.put("srv", ident, _verdict())
        assert cache.get("srv", ident, "9.9.9") is not None

    def test_the_version_survives_a_round_trip_through_the_file(self, tmp_path) -> None:
        path = tmp_path / "v.json"
        cache = vc.VerdictCache(path)
        ident = _ident()
        cache.put(
            "srv",
            ident,
            vc.CachedPreflight(
                ran=True, caller_sensitive=False, reasons=(), evaluated_at=1.0,
                reported_version="1.2.3",
            ),
        )
        cache.flush()
        fresh = vc.VerdictCache(path)
        fresh.load()
        # The matching read first: the stored version still validates the row.
        assert fresh.get("srv", ident, "1.2.3") is not None
        # Then the mismatch, which both refuses AND drops -- see
        # TestSupersededRowIsNotReadable for why refusing alone is not enough.
        assert fresh.get("srv", ident, "1.2.4") is None
        assert fresh.get_by_name("srv") is None

    def test_a_non_string_version_in_the_file_reads_as_absent(self, tmp_path) -> None:
        """The file is operator-editable, so a wrong type must not raise."""
        path = tmp_path / "v.json"
        ident = _ident()
        path.write_text(
            json.dumps(
                {
                    "entries": {
                        "srv": {
                            "ran": True,
                            "callerSensitive": False,
                            "reasons": [],
                            "evaluatedAt": 1.0,
                            "identity": ident.as_str(),
                            "reportedVersion": {"not": "a string"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        cache = vc.VerdictCache(path)
        cache.load()
        assert cache.get("srv", ident, "1.2.3") is not None


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

    An answer is ``(status, capabilities)``, or ``(status, capabilities, extra)``
    where *extra* sets the other facets a real probe fills in — ``tools``,
    ``tool_annotations``, ``protocol_version``, ``server_info``. The two-element
    form is kept because most cases only care about capabilities, and spelling
    every field in every case would bury which one the test is about.
    """

    def __init__(self, answers: dict[str, tuple[Any, ...]]) -> None:
        self.answers = answers
        self.identities: list[str] = []

    async def __call__(
        self, server: McpServerInfo, *, client_info: dict[str, str] | None = None
    ) -> McpServerInfo:
        name = (client_info or {}).get("name", "default")
        self.identities.append(name)
        answer = self.answers[name]
        status, caps = answer[0], answer[1]
        server.status = status
        server.capabilities = caps
        for field_name, value in (answer[2] if len(answer) > 2 else {}).items():
            setattr(server, field_name, value)
        if status != "ok":
            server.error = "boom"
        return server


@pytest.fixture
def patch_probe(monkeypatch: pytest.MonkeyPatch):
    def _install(answers: dict[str, tuple[Any, ...]]) -> _FakeProbe:
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
    async def test_a_divergence_is_reported_but_never_frozen(
        self, patch_probe, tmp_path
    ) -> None:
        """The #4339 fix, and the reason it needs no expiry clock.

        Two spawns that disagree cannot say WHY they disagree: an answer computed
        from ``clientInfo`` and an answer that varies for the server's own reasons
        both look like this. Storing the guess turned one unlucky sample into a
        permanent mark, because a stored row is skipped by every later pass.

        So a divergence joins the branch above: reported for this pass, re-derived
        on the next one. The server keeps costing two spawns, which is the honest
        price of a question we cannot answer once and for all -- and it is exactly
        what makes the measure button able to clear a wrong row.
        """
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe(
            {_ID_A: ("ok", {"tools": {}}), _ID_B: ("ok", {"prompts": {}})}
        )

        first = await ev.evaluate_new_servers([_server()], tmp_path)
        assert first["srv"].ran is True, "it WAS measured"
        assert first["srv"].caller_sensitive is True, "and the divergence is reported"
        spawns = len(fake.identities)
        assert spawns > 0

        await ev.evaluate_new_servers([_server()], tmp_path)

        assert len(fake.identities) > spawns, "a divergence must not suppress a re-measure"

    @pytest.mark.asyncio
    async def test_a_divergence_is_still_visible_to_the_page(
        self, patch_probe, tmp_path
    ) -> None:
        """Not frozen must not mean not reported.

        The dashboard builds its assessment rows from the verdict cache and from
        nothing else -- both production callers of ``evaluate_new_servers`` discard
        the returned dict. So a result withheld from the store is not merely
        forgotten: the page calls the server UNMEASURED, about a server just
        spawned twice, and the divergence note becomes unreachable in production.

        Hence the split this test guards: the row is STORED so it can be shown, and
        separately it never suppresses the next measurement.
        """
        from kiro_crew.mcp_gateway import evaluate as ev
        from kiro_crew.mcp_gateway.verdict_cache import load_cache

        patch_probe({_ID_A: ("ok", {"tools": {}}), _ID_B: ("ok", {"prompts": {}})})
        await ev.evaluate_new_servers([_server()], tmp_path)

        row = load_cache(tmp_path).get_by_name("srv")
        assert row is not None, "the page reads this cache; an absent row reads as unmeasured"
        assert row.ran is True
        assert row.caller_sensitive is True

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
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)

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


class TestToolSurfaceIsCompared:
    """The tool list is a facet of its own, and the only one an old server has.

    Tool ANNOTATIONS arrived in MCP 2025-03-26, so a server older than that can
    be measured on nothing else — these pin that it is measured at all, and that
    the three ways this comparison could produce a false positive do not.
    """

    @pytest.mark.asyncio
    async def test_a_different_tool_set_per_caller_is_caught(self, patch_probe) -> None:
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["read", "write"]}),
                _ID_B: ("ok", caps, {"tools": ["read", "admin"]}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        # The reason must name the measurement that caught it, not the handshake.
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)
        # One reason code covers every facet; ``detail`` is what still names which
        # measurement caught it, and it is what the log line carries.
        assert result.detail == f"{pf.FACET_TOOLS}_shape_differs"

    @pytest.mark.asyncio
    async def test_a_server_with_no_annotations_is_still_measurable(self, patch_probe) -> None:
        """The case the whole facet exists for: no annotations, still decidable."""
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["a"], "tool_annotations": []}),
                _ID_B: ("ok", caps, {"tools": ["a", "b"], "tool_annotations": []}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)
        assert result.detail == f"{pf.FACET_TOOLS}_shape_differs"

    @pytest.mark.asyncio
    async def test_tool_ORDER_alone_is_not_divergence(self, patch_probe) -> None:
        """A pooled backend replays one cached list, so order is not a promise.

        Nothing in the protocol fixes enumeration order, and a server that walks
        a dict would otherwise be condemned on every probe.
        """
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["read", "write"]}),
                _ID_B: ("ok", caps, {"tools": ["write", "read"]}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive

    @pytest.mark.asyncio
    async def test_a_reordered_annotation_list_is_not_a_divergence(
        self, patch_probe
    ) -> None:
        """Annotations arrive positionally, so a reorder must not read as a change."""
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: (
                    "ok",
                    caps,
                    {
                        "tools": ["read", "write"],
                        "tool_annotations": [{"readOnlyHint": True}, {"readOnlyHint": False}],
                    },
                ),
                _ID_B: (
                    "ok",
                    caps,
                    {
                        "tools": ["write", "read"],
                        "tool_annotations": [{"readOnlyHint": False}, {"readOnlyHint": True}],
                    },
                ),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive

    @pytest.mark.asyncio
    async def test_a_changed_annotation_for_the_same_tool_is_caught(self, patch_probe) -> None:
        """Telling one caller a tool is read-only and another that it is not."""
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["x"], "tool_annotations": [{"readOnlyHint": True}]}),
                _ID_B: (
                    "ok",
                    caps,
                    {"tools": ["x"], "tool_annotations": [{"readOnlyHint": False}]},
                ),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)
        assert result.detail == f"{pf.FACET_TOOLS}_shape_differs"


class TestHandshakeFacetsBeyondCapabilities:
    """``initialize`` carries more than capabilities, and the backend replays it all."""

    @pytest.mark.asyncio
    async def test_a_different_protocol_version_per_caller_is_caught(self, patch_probe) -> None:
        patch_probe(
            {
                _ID_A: ("ok", {"tools": {}}, {"protocol_version": "2025-06-18"}),
                _ID_B: ("ok", {"tools": {}}, {"protocol_version": "2024-11-05"}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)

    @pytest.mark.asyncio
    async def test_server_info_gaining_a_key_per_caller_is_caught(self, patch_probe) -> None:
        patch_probe(
            {
                _ID_A: ("ok", {"tools": {}}, {"server_info": {"name": "s", "version": "1"}}),
                _ID_B: (
                    "ok",
                    {"tools": {}},
                    {"server_info": {"name": "s", "version": "1", "tier": "pro"}},
                ),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)

    @pytest.mark.asyncio
    async def test_a_varying_build_id_in_server_info_is_not_divergence(
        self, patch_probe
    ) -> None:
        """Values inside ``serverInfo`` are projected away, and must be.

        A build id or per-spawn nonce there is not caller sensitivity, and
        comparing values would condemn such a server on every single probe. The
        cost of this projection is that a server varying only its version STRING
        per caller reads as agreeing — which the pool can survive, because the
        replayed string is diagnostic rather than behavioural.
        """
        patch_probe(
            {
                _ID_A: ("ok", {"tools": {}}, {"server_info": {"name": "s", "build": "aaa"}}),
                _ID_B: ("ok", {"tools": {}}, {"server_info": {"name": "s", "build": "zzz"}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive

    @pytest.mark.asyncio
    async def test_the_handshake_is_named_when_both_facets_diverge(self, patch_probe) -> None:
        """One row reports one reason, so the earlier promise is the one named."""
        patch_probe(
            {
                _ID_A: ("ok", {"tools": {}}, {"tools": ["a"]}),
                _ID_B: ("ok", {"resources": {}}, {"tools": ["b"]}),
            }
        )
        result = await pf.preflight(_server())
        assert result.detail == f"{pf.FACET_INIT}_shape_differs"
        assert result.reasons == (pf.REASON_HANDSHAKE_NOT_REPRODUCIBLE,)


class TestAnnotationsAreNeverPairedWithATool:
    """Annotations are compared unpaired, because alignment is not recoverable.

    ``mcp_discovery`` builds ``tools`` and ``tool_annotations`` in two comprehensions
    over the same list with INDEPENDENT predicates: a tool joins ``tools`` when its
    name is truthy and joins ``tool_annotations`` when it carries an ``annotations``
    dict. Neither list records which tool an entry came from, so equal LENGTHS do not
    imply equal membership -- checking the lengths is as much a guess as pairing by
    index. These tests pin the comparison to a name set plus an unordered multiset of
    annotation shapes, which makes the alignment question unreachable rather than
    answered.
    """

    def _srv(self, tools, anns):
        return SimpleNamespace(
            capabilities={"tools": {}},
            protocol_version="2025-06-18",
            server_info={},
            tools=list(tools),
            tool_annotations=list(anns),
            name="srv",
        )

    def test_a_reordered_tool_list_is_not_a_divergence(self) -> None:
        # Same names, same annotation content, different enumeration ORDER --
        # which is exactly what index pairing misread as a per-caller difference.
        a = self._srv(["read", "write", "admin"], [{"readOnlyHint": True}])
        b = self._srv(["admin", "read", "write"], [{"readOnlyHint": True}])
        assert pf._tool_surface(a) == pf._tool_surface(b)

    def test_equal_lengths_do_not_imply_alignment(self) -> None:
        """The case that retired the length guard.

        A tool with an empty name is dropped from ``tools`` but its annotations are
        kept, so these two lists are both length 1 and describe DIFFERENT tools. A
        length check passes here and index pairing then attaches ``read``'s name to
        the other tool's claim. Comparing unpaired, the two servers agree -- because
        they did make the same claim -- instead of producing a verdict from a pairing
        that was never real.
        """
        # As the prober would emit for [{"name": "read"}, {"name": "", "annotations": ...}]
        a = self._srv(["read"], [{"readOnlyHint": True}])
        # ... and for [{"name": "read", "annotations": ...}, {"name": ""}]
        b = self._srv(["read"], [{"readOnlyHint": True}])
        assert pf._tool_surface(a) == pf._tool_surface(b)

    def test_a_dict_valued_annotation_does_not_raise(self) -> None:
        """Ordering the shapes must not repeat the tool-name crash class.

        The projection yields dicts, and ``sorted()`` over dicts raises TypeError --
        the same failure that malformed tool names caused. Canonicalising each shape
        to a string before sorting is what keeps this a comparison and not a crash.
        """
        a = self._srv(["x", "y"], [{"audience": {"a": 1}}, {"audience": {"b": 2}}])
        b = self._srv(["x", "y"], [{"audience": {"b": 2}}, {"audience": {"a": 1}}])
        assert pf._tool_surface(a) == pf._tool_surface(b), "a reorder is not a divergence"

    def test_a_changed_claim_is_caught_even_when_the_tool_is_unknown(self) -> None:
        """The signal the length guard used to throw away.

        The server told one caller ``readOnlyHint: true`` and the other ``false``.
        Which tool it was about is unknowable, and does not matter: a pooled backend
        replays the first answer, so the second session would receive a claim this
        server would not have made to it. Dropping the facet here -- as a length
        check did, since one list was shorter than the names -- discarded a real
        divergence to avoid guessing at an attribution nothing reads.
        """
        a = self._srv(["read", "write"], [{"readOnlyHint": True}])
        b = self._srv(["read", "write"], [{"readOnlyHint": False}])
        assert pf._tool_surface(a) != pf._tool_surface(b)

    def test_annotating_a_different_subset_is_not_a_divergence(self) -> None:
        """The other direction: same claims, different tool annotated.

        Both spawns made exactly one ``readOnlyHint: true`` claim. Nothing a pooled
        session could observe differs, so this must not read as caller-sensitive.
        """
        a = self._srv(["read", "write"], [{"readOnlyHint": True}])
        b = self._srv(["write", "read"], [{"readOnlyHint": True}])
        assert pf._tool_surface(a) == pf._tool_surface(b)

    def test_a_complete_annotation_list_is_still_compared(self) -> None:
        """The facet must still detect a differing claim in the ordinary case."""
        a = self._srv(["read", "write"], [{"readOnlyHint": True}, {"readOnlyHint": True}])
        b = self._srv(["read", "write"], [{"readOnlyHint": True}, {"readOnlyHint": False}])
        assert pf._tool_surface(a) != pf._tool_surface(b)


class TestOnePassAtATime:
    """The verdict store is rewritten whole, so passes must not interleave.

    Two overlapping passes each load the file, measure, and flush their own copy;
    the later flush drops every row the other wrote, reverting freshly measured
    servers to "not measured". The operator pass runs for minutes and the probe
    path calls the same evaluator, so the overlap is the normal case.
    """

    @pytest.mark.asyncio
    async def test_a_second_pass_does_not_erase_the_first_pass_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.mcp_gateway.evaluate as ev

        started: list[str] = []
        release = asyncio.Event()

        async def slow_preflight(server):
            started.append(server.name)
            if server.name == "slow-mcp":
                await release.wait()
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", slow_preflight)

        def srv(name):
            return McpServerInfo(name=name, command="/bin/true")

        first = asyncio.create_task(
            ev.evaluate_new_servers([srv("slow-mcp")], tmp_path, budget=None)
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            ev.evaluate_new_servers([srv("other-mcp")], tmp_path, budget=None)
        )
        await asyncio.sleep(0.05)

        # The lock must keep the second pass out until the first has flushed.
        assert started == ["slow-mcp"], started
        release.set()
        await first
        await second

        stored = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        stored.load()
        assert stored.server_names() == {"slow-mcp", "other-mcp"}, stored.server_names()


class TestProgressArrivesDuringThePass:
    """A readout that only updates at the end is not progress."""

    @pytest.mark.asyncio
    async def test_the_hook_fires_before_the_last_measurement_finishes(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.mcp_gateway.evaluate as ev

        gate = asyncio.Event()
        seen: list[tuple[int, int, int]] = []

        async def preflight(server):
            if server.name == "last-mcp":
                await gate.wait()
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", preflight)
        servers = [
            McpServerInfo(name="first-mcp", command="/bin/true"),
            McpServerInfo(name="last-mcp", command="/bin/true"),
        ]
        task = asyncio.create_task(
            ev.evaluate_new_servers(
                servers,
                tmp_path,
                budget=None,
                on_progress=lambda m, d, t: seen.append((m, d, t)),
            )
        )
        # Give the fast one time to land while the slow one is still blocked.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if seen:
                break
        assert seen == [(1, 1, 2)], f"progress did not arrive mid-pass: {seen}"
        gate.set()
        await task
        assert seen[-1] == (2, 2, 2), seen


class TestMeasuredIsCountedApartFromAttempted:
    """A server the pre-flight could not reach was attempted, not measured.

    The two counts are what separates "the pass ran" from "the pass produced
    something". A pre-flight that could not run leaves no verdict on purpose -- the
    failure belongs to the moment, not to the server -- so the row stays unmeasured
    and the operator's button keeps offering it. Reporting one number for both let
    a pass that measured nothing close by claiming it measured everything it tried,
    beside a table that still showed every row as unmeasured.
    """

    @pytest.mark.asyncio
    async def test_a_preflight_that_could_not_run_advances_only_the_attempt_count(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.mcp_gateway.evaluate as ev

        seen: list[tuple[int, int, int]] = []

        async def preflight(server):
            # ``ran=False`` is the module's own "could not ask": a missing
            # credential, a dead tunnel, a host where the probe cannot spawn.
            if server.name == "unreachable-mcp":
                return SimpleNamespace(ran=False, caller_sensitive=False, reasons=())
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", preflight)
        servers = [
            McpServerInfo(name="good-mcp", command="/bin/true"),
            McpServerInfo(name="unreachable-mcp", command="/bin/true"),
        ]
        await ev.evaluate_new_servers(
            servers,
            tmp_path,
            budget=None,
            on_progress=lambda m, d, t: seen.append((m, d, t)),
        )

        measured, attempted, total = seen[-1]
        assert (attempted, total) == (2, 2), seen
        # The whole point: both servers were tried, one produced a verdict.
        assert measured == 1, seen

        # And the count matches what is actually on disk, which is what the table
        # beside the readout renders. A closure line built on ``attempted`` would
        # claim two while this set holds one.
        stored = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        stored.load()
        assert stored.server_names() == {"good-mcp"}, stored.server_names()

    @pytest.mark.asyncio
    async def test_a_pass_that_reaches_nothing_reports_zero_measured(
        self, tmp_path, monkeypatch
    ) -> None:
        """The case that produced the contradiction: every server unreachable.

        This is not a corner -- the probe cannot spawn at all on some hosts, so
        every server in the configuration takes the ``ran=False`` branch and the
        readout used to say it had measured all of them.
        """
        import kiro_crew.mcp_gateway.evaluate as ev

        seen: list[tuple[int, int, int]] = []

        async def preflight(server):
            return SimpleNamespace(ran=False, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", preflight)
        servers = [
            McpServerInfo(name=f"srv-{i}-mcp", command="/bin/true") for i in range(3)
        ]
        await ev.evaluate_new_servers(
            servers,
            tmp_path,
            budget=None,
            on_progress=lambda m, d, t: seen.append((m, d, t)),
        )

        assert seen[-1] == (0, 3, 3), seen


class TestMalformedToolNames:
    """A server that cannot name its own tools must not take the pass down.

    The prober copies a tool's ``name`` straight from ``tools/list`` and only drops
    falsy values, so a non-string name survives into ``server.tools``. Ordering a
    mixed list raises ``TypeError``; because the evaluator flushes only after every
    measurement completes, that exception would discard the verdicts of every
    server already measured in the same pass.
    """

    @pytest.mark.asyncio
    async def test_a_non_string_tool_name_reads_as_unmeasurable(self, patch_probe) -> None:
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["read", 123]}),
                _ID_B: ("ok", caps, {"tools": ["read", 123]}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran is False
        assert result.caller_sensitive is False
        assert result.detail == "malformed_tool_names"
        # "Could not measure" must never read as evidence against the server.
        assert result.reasons == (pf.REASON_PREFLIGHT_UNAVAILABLE,)

    @pytest.mark.asyncio
    async def test_one_side_malformed_is_enough_to_abstain(self, patch_probe) -> None:
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["read"]}),
                _ID_B: ("ok", caps, {"tools": [456]}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran is False, "a malformed second answer must not be compared"

    @pytest.mark.asyncio
    async def test_all_string_names_are_still_measured(self, patch_probe) -> None:
        """The guard must not make every server unmeasurable."""
        caps = {"tools": {}}
        patch_probe(
            {
                _ID_A: ("ok", caps, {"tools": ["read", "write"]}),
                _ID_B: ("ok", caps, {"tools": ["write", "read"]}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran is True and result.caller_sensitive is False

    @pytest.mark.asyncio
    async def test_a_malformed_server_does_not_discard_the_pass(
        self, tmp_path, monkeypatch
    ) -> None:
        """The consequence, not just the exception: earlier verdicts must survive.

        Ordering is forced so the malformed server is measured LAST, which is the
        arrangement in which a raised TypeError would take the already-measured
        rows down with it.
        """
        import kiro_crew.mcp_gateway.evaluate as ev

        real = pf.preflight

        async def route(server):
            if server.name == "bad-mcp":
                probe = SimpleNamespace(
                    status="ok", capabilities={"tools": {}}, protocol_version="",
                    server_info={}, tools=["ok", 7], tool_annotations=[], name=server.name,
                )
                # Exercise the real guard against a real mixed list.
                assert pf._tool_names_are_comparable(probe) is False
                return pf.PreflightResult(ran=False, detail="malformed_tool_names")
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", route)
        assert real is not None
        servers = [
            McpServerInfo(name="good-mcp", command="/bin/true"),
            McpServerInfo(name="bad-mcp", command="/bin/true"),
        ]
        out = await ev.evaluate_new_servers(servers, tmp_path, budget=None)
        assert "good-mcp" in out, out

        stored = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        stored.load()
        # The good one is persisted; the malformed one is deliberately NOT cached,
        # because a pre-flight that could not run is never stored.
        assert stored.server_names() == {"good-mcp"}, stored.server_names()


class TestOneServerCannotEndThePass:
    """The pass survives a server whose payload breaks the projection.

    Every facet the pre-flight compares is JSON the server chose, walked by code
    that recurses. The varieties that can make that walk raise are not enumerable
    from the consumer side, and the flush happens only after the whole loop, so
    without a boundary at the measurement one bad server discards every verdict the
    pass already paid two spawns each for.
    """

    @pytest.mark.asyncio
    async def test_deep_annotations_do_not_discard_the_other_verdicts(
        self, tmp_path, monkeypatch
    ) -> None:
        """The exact trigger GPT named: nesting deep enough to exhaust the stack.

        Built as a real payload rather than a synthetic ``raise`` so the test fails
        if the projection stops recursing OR if the boundary is removed.
        """
        import kiro_crew.mcp_gateway.evaluate as ev

        deep: dict = {}
        node = deep
        for _ in range(2000):
            node["a"] = {}
            node = node["a"]

        # Confirm the payload really is hostile to the projection, so a future
        # change that makes the walk iterative turns this into a live assertion
        # rather than a silently vacuous one.
        with pytest.raises(RecursionError):
            pf._capability_shape(deep)

        async def route(server):
            if server.name == "deep-mcp":
                probe = SimpleNamespace(
                    status="ok", capabilities={"tools": {}}, protocol_version="",
                    server_info={}, tools=["x"], tool_annotations=[deep],
                    name=server.name, error="",
                )
                # Reach the real projection through the real comparison path.
                return pf._replayed_surface(probe)
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", route)
        servers = [
            McpServerInfo(name="good-mcp", command="/bin/true"),
            McpServerInfo(name="deep-mcp", command="/bin/true"),
        ]
        out = await ev.evaluate_new_servers(servers, tmp_path, budget=None)

        # The pass completed and reported BOTH servers.
        assert set(out) == {"good-mcp", "deep-mcp"}, out
        assert out["deep-mcp"].ran is False
        assert out["deep-mcp"].caller_sensitive is False

        # The healthy verdict was flushed -- this is the data loss being prevented.
        stored = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        stored.load()
        assert stored.server_names() == {"good-mcp"}, stored.server_names()

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed_as_a_verdict(
        self, tmp_path, monkeypatch
    ) -> None:
        """Shutdown must stay shutdown, not become 'this server is unmeasurable'."""
        import kiro_crew.mcp_gateway.evaluate as ev

        async def route(server):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ev, "preflight", route)
        with pytest.raises(asyncio.CancelledError):
            await ev.evaluate_new_servers(
                [McpServerInfo(name="s", command="/bin/true")], tmp_path, budget=None
            )


class TestSupersededRowIsNotReadable:
    """A row for replaced code must not survive where the dashboard can read it.

    ``get`` refusing to return a row is not enough on its own: the dashboard row
    builder reads through ``get_by_name``, which checks neither identity nor
    version. So a row left in place after the pre-flight fails to re-measure is
    still rendered -- as evidence about code that no longer exists, and in the
    permissive direction.
    """

    def _cache(self, tmp_path, reported_version: str):
        cache = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        cache.load()
        ident = vc.Identity(
            command_args_hash="cah", env_hash="eh", binary_version="bv"
        )
        cache.put(
            "srv",
            ident,
            vc.CachedPreflight(
                ran=True,
                caller_sensitive=False,
                reasons=(),
                evaluated_at=1.0,
                reported_version=reported_version,
            ),
        )
        return cache, ident

    def test_a_version_mismatch_drops_the_row_from_get_by_name_too(self, tmp_path) -> None:
        cache, ident = self._cache(tmp_path, "1.0")
        assert cache.get_by_name("srv") is not None, "row should start present"

        assert cache.get("srv", ident, "2.0") is None, "the read must refuse it"
        # The point of the fix: the dashboard's own reader can no longer see it.
        assert cache.get_by_name("srv") is None
        assert "srv" not in cache.server_names()

    def test_the_drop_is_persisted(self, tmp_path) -> None:
        """Marked dirty, or the row returns on the next load."""
        cache, ident = self._cache(tmp_path, "1.0")
        cache.flush()
        assert cache.get("srv", ident, "2.0") is None
        cache.flush()

        reloaded = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        reloaded.load()
        assert reloaded.get_by_name("srv") is None, "the drop did not reach the file"

    def test_an_identity_mismatch_does_NOT_drop_the_row(self, tmp_path) -> None:
        """The asymmetry, pinned deliberately.

        ``binary_version`` is the literal string ``"unknown"`` for a binary
        mid-install, an ``OSError`` and a ``which`` miss alike, so an identity
        mismatch can mean "could not tell" rather than "the program changed".
        Dropping there would discard a good measurement on a transient read.
        """
        cache, _ = self._cache(tmp_path, "1.0")
        moved = vc.Identity(
            command_args_hash="cah", env_hash="eh", binary_version="unknown"
        )
        assert cache.get("srv", moved, "1.0") is None, "must still refuse the read"
        assert cache.get_by_name("srv") is not None, "but the row must survive"

    def test_one_absent_side_neither_refuses_nor_drops(self, tmp_path) -> None:
        """The no-flap guarantee still holds after the drop was added."""
        cache, ident = self._cache(tmp_path, "1.0")
        assert cache.get("srv", ident, "") is not None, "an unprobed pass must not invalidate"
        assert cache.get_by_name("srv") is not None

        blank, ident2 = self._cache(tmp_path / "b", "")
        assert blank.get("srv", ident2, "9.9") is not None, "a server with no version is not stale"
        assert blank.get_by_name("srv") is not None

    @pytest.mark.asyncio
    async def test_the_dashboard_reads_nothing_after_a_failed_remeasure(
        self, tmp_path, monkeypatch
    ) -> None:
        """The whole chain GPT named, through the real evaluator.

        Upgraded server, version known on both sides, and the re-measure fails --
        so nothing is written back. The store must end up with no row for it.
        """
        import kiro_crew.mcp_gateway.evaluate as ev

        seed = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        seed.load()
        srv = McpServerInfo(name="up-mcp", command="/bin/true")
        # ``identity_for`` does blocking IO and evaluate.py asserts it is off-loop.
        ident = await asyncio.to_thread(ev.identity_for, srv)
        seed.put(
            "up-mcp",
            ident,
            vc.CachedPreflight(
                ran=True, caller_sensitive=False, reasons=(),
                evaluated_at=1.0, reported_version="1.0",
            ),
        )
        seed.flush()

        # The server now reports a different version, and the pre-flight fails.
        monkeypatch.setattr(ev, "reported_version", lambda s: "2.0")

        async def failed(server):
            return SimpleNamespace(ran=False, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", failed)
        out = await ev.evaluate_new_servers([srv], tmp_path, budget=None)
        assert out["up-mcp"].ran is False

        stored = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        stored.load()
        assert stored.get_by_name("up-mcp") is None, (
            "the stale row survived, so the dashboard would still render a verdict "
            "measured against replaced code"
        )


class TestVersionIsNotLoggedRaw:
    """A server-reported version reaches a log line, so it cannot go in raw.

    ``reported_version`` is whatever the server called itself in its handshake --
    remote-controlled text in a sink an operator reads in a terminal. A newline in
    it would forge a second gateway log line and an ESC would recolor or overwrite
    the surrounding output.
    """

    def _drop(self, tmp_path, stored: str, now: str):
        cache = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        cache.load()
        ident = vc.Identity(command_args_hash="c", env_hash="e", binary_version="b")
        cache.put(
            "srv",
            ident,
            vc.CachedPreflight(
                ran=True, caller_sensitive=False, reasons=(),
                evaluated_at=1.0, reported_version=stored,
            ),
        )
        return cache.get("srv", ident, now)

    @pytest.mark.parametrize(
        "hostile",
        [
            "9.9\nWARNING kirocrew: forged entry",
            "9.9\rforged",
            "9.9\x1b[31mrecoloured\x1b[0m",
            "9.9\x00hidden",
            "9.9\u2028forged",
            "9.9\u202eforged",
        ],
    )
    def test_no_control_character_reaches_the_log(self, tmp_path, caplog, hostile) -> None:
        with caplog.at_level(logging.INFO, logger="kiro_crew.mcp_gateway.verdict_cache"):
            assert self._drop(tmp_path, "1.0", hostile) is None

        assert caplog.records, "the drop must still be logged"
        for rec in caplog.records:
            line = rec.getMessage()
            # One record must render as ONE line, or the server has forged an entry.
            assert len(line.splitlines()) <= 1, f"forged a line break: {line!r}"
            leaked = [c for c in line if ord(c) < 0x20 or ord(c) == 0x7F]
            assert not leaked, f"raw control characters reached the log: {leaked!r}"
            assert all(c.isprintable() or c == " " for c in line), (
                f"an unprintable character reached the log: {line!r}"
            )

    def test_the_version_is_still_reported_for_diagnosis(self, tmp_path, caplog) -> None:
        """Escaping, not omitting -- the operator still sees what the server said."""
        with caplog.at_level(logging.INFO, logger="kiro_crew.mcp_gateway.verdict_cache"):
            assert self._drop(tmp_path, "1.0.0", "2.0.0") is None
        line = " ".join(r.getMessage() for r in caplog.records)
        assert "1.0.0" in line and "2.0.0" in line, line
        assert "srv" in line

    def test_an_overlong_version_is_bounded(self, tmp_path, caplog) -> None:
        """``%r`` escapes but does not bound length; one field is not a wall of text."""
        with caplog.at_level(logging.INFO, logger="kiro_crew.mcp_gateway.verdict_cache"):
            assert self._drop(tmp_path, "1.0", "9." + "A" * 10_000) is None
        line = " ".join(r.getMessage() for r in caplog.records)
        assert len(line) < 2 * vc._VERSION_LOG_LEN_CAP + 200, f"unbounded: {len(line)} chars"
        assert "A" * (vc._VERSION_LOG_LEN_CAP + 1) not in line


class TestBudgetedPassYields:
    """A request-path pass must not wait minutes behind an operator pass.

    ``probe_all`` has already produced the status and tools the request was for by
    the time the shareability step runs, so waiting on the lock holds a computed
    response rather than doing useful work. An uncapped pass runs for minutes and a
    probe firing during one is the normal case, not the unlucky one.
    """

    @pytest.fixture(autouse=True)
    def _own_lock(self, monkeypatch):
        """A lock belonging to THIS test's event loop.

        ``_PASS_LOCK`` is a module global, and an ``asyncio.Lock`` binds to the loop
        that first waits on it. pytest-asyncio gives each test its own loop, so
        touching the shared instance leaks state between tests in both directions.
        """
        import kiro_crew.mcp_gateway.evaluate as ev

        monkeypatch.setattr(ev, "_PASS_LOCK", asyncio.Lock())

    @pytest.mark.asyncio
    async def test_a_budgeted_call_serves_stored_rows_while_a_pass_runs(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.mcp_gateway.evaluate as ev

        srv = McpServerInfo(name="known-mcp", command="/bin/true")
        seed = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        seed.load()
        seed.put(
            "known-mcp",
            await asyncio.to_thread(ev.identity_for, srv),
            vc.CachedPreflight(
                ran=True, caller_sensitive=True, reasons=("caller_sensitive_initialize",),
                evaluated_at=1.0, reported_version="1.0",
            ),
        )
        seed.flush()

        measured: list[str] = []

        async def never_called(server):
            measured.append(server.name)
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", never_called)

        async with ev._PASS_LOCK:
            out = await ev.evaluate_new_servers([srv], tmp_path)

        assert measured == [], "a yielding pass must not measure"
        # It still served the stored verdict, which is what the row renders from.
        assert out["known-mcp"].caller_sensitive is True
        assert out["known-mcp"].ran is True

    @pytest.mark.asyncio
    async def test_an_uncapped_pass_still_waits_for_the_lock(
        self, tmp_path, monkeypatch
    ) -> None:
        """Only the BUDGETED path yields.

        Two operator passes must still serialize or they clobber each other's rows,
        which is the reason the lock exists.
        """
        import kiro_crew.mcp_gateway.evaluate as ev

        async def ok(server):
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", ok)
        srv = McpServerInfo(name="s", command="/bin/true")

        await ev._PASS_LOCK.acquire()
        task = asyncio.ensure_future(
            ev.evaluate_new_servers([srv], tmp_path, budget=None)
        )
        try:
            # Let the task reach the lock, then confirm it is parked there.
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "an uncapped pass must block on the lock"
        finally:
            ev._PASS_LOCK.release()
        out = await task
        assert "s" in out

    @pytest.mark.asyncio
    async def test_yielding_does_not_invent_a_verdict(self, tmp_path, monkeypatch) -> None:
        """A server with no stored row stays absent, so it reads as unmeasured."""
        import kiro_crew.mcp_gateway.evaluate as ev

        async def ok(server):
            return SimpleNamespace(ran=True, caller_sensitive=False, reasons=())

        monkeypatch.setattr(ev, "preflight", ok)
        async with ev._PASS_LOCK:
            out = await ev.evaluate_new_servers(
                [McpServerInfo(name="fresh-mcp", command="/bin/true")], tmp_path
            )
        assert out == {}, out


class TestStaleSchemaRowsAreDropped:
    """A verdict measured under a different compared-facet set is not evidence.

    Refusing it at read is not enough: the dashboard row builder reads by name
    without checking either validity input, so a surviving row renders as a
    verdict, the server counts as measured, and the action offering to measure it
    is disabled -- on exactly the installs a schema bump exists to re-derive.
    """

    def _write(self, tmp_path, schema: int):
        f = tmp_path / vc.VERDICT_CACHE_FILENAME
        ident = "\u0000".join(("cah", "eh", "bv", str(schema)))
        f.write_text(
            json.dumps(
                {
                    "entries": {
                        "srv": {
                            "ran": True,
                            "callerSensitive": False,
                            "reasons": [],
                            "evaluatedAt": 1.0,
                            "identity": ident,
                            "reportedVersion": "1.0",
                        }
                    },
                    "applied": [],
                }
            ),
            encoding="utf-8",
        )
        return f

    def test_an_older_schema_row_is_not_readable_by_name(self, tmp_path) -> None:
        self._write(tmp_path, vc.SCHEMA - 1)
        cache = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        cache.load()
        # The dashboard's own reader must not see it, or the row renders as a
        # verdict and the measure action is disabled for that server.
        assert cache.get_by_name("srv") is None
        assert cache.server_names() == set()
        assert len(cache) == 0

    def test_a_current_schema_row_survives(self, tmp_path) -> None:
        """The drop must not empty the cache on every load."""
        self._write(tmp_path, vc.SCHEMA)
        cache = vc.VerdictCache(tmp_path / vc.VERDICT_CACHE_FILENAME)
        cache.load()
        row = cache.get_by_name("srv")
        assert row is not None and row.ran is True
        assert row.reported_version == "1.0"

    def test_a_row_with_no_identity_is_dropped(self, tmp_path) -> None:
        """Written before identities were stored, so it describes an unknown check."""
        f = tmp_path / vc.VERDICT_CACHE_FILENAME
        f.write_text(
            json.dumps(
                {"entries": {"srv": {"ran": True, "callerSensitive": True}}, "applied": []}
            ),
            encoding="utf-8",
        )
        cache = vc.VerdictCache(f)
        cache.load()
        assert cache.get_by_name("srv") is None, "an identity-less row is not evidence"

    def test_the_matcher_reads_the_schema_field_not_a_substring(self) -> None:
        """A hash that merely ends in the schema digits must not pass."""
        good = vc.Identity(command_args_hash="a", env_hash="b", binary_version="c").as_str()
        assert vc._identity_matches_schema(good) is True
        assert vc._identity_matches_schema("") is False
        # No separator at all: a bare number is not an identity.
        assert vc._identity_matches_schema(str(vc.SCHEMA)) is False
        # Right shape, wrong schema.
        stale = "\u0000".join(("a", "b", "c", str(vc.SCHEMA - 1)))
        assert vc._identity_matches_schema(stale) is False
