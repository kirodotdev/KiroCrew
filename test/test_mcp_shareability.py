"""Verdict engine + hazard ledger for stub/share recommendations."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import hazards, shareability
from kiro_crew.mcp_gateway import verdict_cache as vc
from kiro_crew.mcp_gateway.hashing import ENV_SCRUB_PREFIXES
from kiro_crew.mcp_gateway.record_json import finite_float
from kiro_crew.mcp_gateway.shareability import ShareEvidence, Strength, assess


def _ident(**over: object) -> vc.Identity:
    base: dict = {
        "command_args_hash": "cmd1",
        "env_hash": "env1",
        "binary_version": "1.0.0",
    }
    base.update(over)
    return vc.Identity(**base)  # type: ignore[arg-type]


def _verdict() -> vc.CachedPreflight:
    return vc.CachedPreflight(ran=True, caller_sensitive=False, reasons=(), evaluated_at=1.0)


def _codes(verdict: shareability.ShareVerdict) -> set[str]:
    return {r.code for r in verdict.reasons}


class TestRefutationOutranksEverything:
    def test_observed_hazard_beats_a_positive_declaration(self) -> None:
        """A server may declare caller-identity and still be caught misbehaving.

        This ordering is the whole point of the ledger: a declaration is a
        promise, an observed hazard is what actually happened.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                observed_hazards=(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,),
            )
        )
        assert verdict.strength is Strength.REFUTED
        assert not verdict.recommend_stub
        assert not verdict.recommend_share
        assert _codes(verdict) == {"observed_hazard"}


class TestDisqualifiers:
    def test_rotating_secret_env_disqualifies_without_a_probe(self) -> None:
        """A config fact stands whether or not the server could be started."""
        verdict = assess(
            ShareEvidence(name="x", probe_ok=False, declared_env_names=("AWS_SECRET_ACCESS_KEY",))
        )
        assert verdict.strength is Strength.DISQUALIFIED
        assert _codes(verdict) == {"rotating_secret_env"}
        assert verdict.reasons[0].detail == "AWS_SECRET_ACCESS_KEY"

    def test_rotating_prefixes_cover_every_pool_key_exclusion(self) -> None:
        """Ratchet: hashing's scrub list is what makes co-tenants disagree.

        If ``hashing`` grows a prefix this module does not know about, a server
        authenticating from that variable would be recommended for sharing
        while the pool key ignores its value — the exact incorrectness this
        disqualifier exists to prevent.
        """
        for prefix in ENV_SCRUB_PREFIXES:
            assert shareability.rotating_secret_env((prefix + "_X",)) == (prefix + "_X",), prefix

    def test_first_party_is_never_recommended(self) -> None:
        verdict = assess(ShareEvidence(name="kirocrew-core", is_first_party=True, probe_ok=True))
        assert verdict.strength is Strength.DISQUALIFIED
        assert _codes(verdict) == {"first_party_session_scoped"}

    def test_non_stdio_is_out_of_scope_not_unsafe(self) -> None:
        verdict = assess(ShareEvidence(name="x", is_stdio=False, probe_ok=True))
        assert _codes(verdict) == {"not_stdio"}

    def test_resources_subscribe_disqualifies(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x", probe_ok=True, capabilities={"resources": {"subscribe": True}}
            )
        )
        assert verdict.strength is Strength.DISQUALIFIED
        assert verdict.reasons[0].detail == "resources_subscribe"

    def test_subscribe_false_is_an_explicit_no_and_does_not_count(self) -> None:
        """``{"subscribe": false}`` is the server saying it does NOT subscribe."""
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"resources": {"subscribe": False, "listChanged": True}},
            )
        )
        assert verdict.strength is Strength.NO_OBJECTION

    def test_list_changed_alone_is_not_a_disqualifier(self) -> None:
        """Those notifications are global broadcasts, safe to fan out."""
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={
                    "tools": {"listChanged": True},
                    "prompts": {"listChanged": True},
                    "resources": {"listChanged": True},
                },
            )
        )
        assert verdict.strength is Strength.NO_OBJECTION

    def test_declaring_logging_at_all_disqualifies(self) -> None:
        """In MCP an empty object ADVERTISES a capability, it does not withhold it.

        ``{"logging": {}}`` means ``logging/setLevel`` is supported, and that
        level is process-wide: one co-tenant's call changes what every other
        session receives. Testing truthiness here (an earlier version of this
        module) let exactly that server through as shareable.
        """
        for caps in ({"logging": {}}, {"logging": {"level": "info"}}):
            verdict = assess(ShareEvidence(name="x", probe_ok=True, capabilities=caps))
            assert verdict.strength is Strength.DISQUALIFIED, caps
            assert verdict.reasons[0].detail == "logging_level"

    def test_absent_logging_key_does_not_disqualify(self) -> None:
        verdict = assess(
            ShareEvidence(name="x", probe_ok=True, has_tools=True, capabilities={"tools": {}})
        )
        assert verdict.strength is Strength.NO_OBJECTION


class TestUnknownVersusNoObjection:
    def test_never_probed_is_unknown_not_recommendable(self) -> None:
        verdict = assess(ShareEvidence(name="x", probe_ok=False))
        assert verdict.strength is Strength.UNKNOWN
        assert not verdict.recommend_stub
        assert _codes(verdict) == {"not_probed"}

    def test_probe_ok_with_no_capabilities_object_is_still_unknown(self) -> None:
        """``capabilities=None`` means we never saw a handshake result."""
        verdict = assess(ShareEvidence(name="x", probe_ok=True, capabilities=None))
        assert verdict.strength is Strength.UNKNOWN


class TestPositiveDeclaration:
    def test_caller_identity_plus_passed_preflight_recommends_both(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub and verdict.recommend_share
        assert "declares_caller_identity" in _codes(verdict)
        assert "preflight_passed" in _codes(verdict)

    def test_declaration_without_a_run_preflight_does_not_grant_sharing(self) -> None:
        """A promise nobody has tested is not evidence.

        The whole point of anticipating is that a real session must not be the
        first thing to discover the server was wrong about itself.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=None,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False
        assert "preflight_not_run" in _codes(verdict)

    def test_measurement_beats_the_declaration(self) -> None:
        """Caught answering initialize per-caller outranks advertising support."""
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
                preflight_caller_sensitive=True,
            )
        )
        assert verdict.strength is Strength.DISQUALIFIED
        assert _codes(verdict) == {"caller_sensitive_initialize"}
        assert not verdict.recommend_stub


class TestNoObjectionSplitsStubFromShare:
    def test_absence_of_evidence_recommends_stub_but_not_share(self) -> None:
        """The load-bearing product decision.

        Stubbing keeps the backend 1:1 with the session — same topology as no
        gateway — and is what unlocks server-authored UI. Sharing is the step
        that introduces co-tenancy. On weak evidence we offer the safe half.
        """
        verdict = assess(ShareEvidence(name="x", probe_ok=True, has_tools=True, capabilities={}))
        assert verdict.strength is Strength.NO_OBJECTION
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False

    def test_read_only_annotations_are_reported_as_supporting_evidence(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                protocol_version="2025-06-18",
                tool_annotations=[{"readOnlyHint": True}, {"readOnlyHint": True}],
            )
        )
        assert "all_tools_read_only" in _codes(verdict)

    def test_one_writing_tool_removes_the_read_only_claim(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                tool_annotations=[{"readOnlyHint": True}, {"readOnlyHint": False}],
            )
        )
        assert "all_tools_read_only" not in _codes(verdict)

    def test_missing_annotations_are_reported_as_unavailable_not_as_writes(self) -> None:
        """An older protocol version cannot send annotations; say so."""
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                protocol_version="2024-11-05",
            )
        )
        codes = _codes(verdict)
        assert "no_tool_annotations" in codes
        assert "all_tools_read_only" not in codes
        detail = next(r.detail for r in verdict.reasons if r.code == "no_tool_annotations")
        assert detail == "2024-11-05"


class TestHazardLedger:
    def test_record_flush_and_reload_round_trip(self, tmp_path) -> None:
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        assert led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST) is True
        assert led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST) is False
        led.flush()

        fresh = hazards.load_ledger(tmp_path)
        assert fresh.codes_for_name("srv") == (hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,)
        assert fresh.codes_for_name("other") == ()

    def test_unknown_code_is_refused(self, tmp_path) -> None:
        """The vocabulary is a UI contract; a typo must not disqualify forever."""
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        assert led.record("srv", "made_up") is False
        assert led.codes_for_name("srv") == ()
        led.flush()
        assert not hazards.ledger_path(tmp_path).exists()

    def test_missing_file_reads_as_no_hazards(self, tmp_path) -> None:
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_corrupt_file_reads_as_no_hazards(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_future_schema_is_ignored_rather_than_guessed(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text(
            json.dumps({"schema": 99, "servers": {"srv": {"codes": ["whatever"]}}}),
            encoding="utf-8",
        )
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_unknown_codes_are_dropped_on_read(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "servers": {
                        "a": {"codes": ["made_up"], "count": 3},
                        "b": {"codes": [hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]},
                    },
                }
            ),
            encoding="utf-8",
        )
        led = hazards.load_ledger(tmp_path)
        assert led.codes_for_name("a") == ()
        assert led.codes_for_name("b") == (hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,)

    def test_flush_is_a_no_op_when_clean(self, tmp_path) -> None:
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.flush()
        assert not hazards.ledger_path(tmp_path).exists()

    def test_an_observation_during_the_write_is_not_marked_persisted(self, tmp_path) -> None:
        """The flush runs off-loop while ``record`` keeps running on it.

        Clearing dirty unconditionally would drop an observation that landed
        mid-write: the ledger would look clean while the file lacked the entry, so
        a hazard the gateway really saw would never withdraw a recommendation.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)

        real_write = hazards.atomic_write

        def racing_write(path, content, **kw):  # noqa: ANN001
            # Simulate the loop recording while this thread is mid-write.
            led.record("other", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
            return real_write(path, content, **kw)

        hazards.atomic_write = racing_write  # type: ignore[assignment]
        try:
            led.flush()
        finally:
            hazards.atomic_write = real_write  # type: ignore[assignment]

        assert led._dirty is True, "the concurrent observation was marked persisted"
        led.flush()
        assert hazards.load_ledger(tmp_path).codes_for_name("other") == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
        )

    def test_concurrent_flushes_cannot_revert_the_file(self, tmp_path) -> None:
        """Two flush callers race, and the loser must not be the newer snapshot.

        The periodic sweep and the shutdown flush both run in worker threads and
        can overlap — cancellation starts the second while the first is mid-write.
        Each builds its payload from the in-memory records BEFORE writing, so
        without serialization the thread that writes last can be the one that read
        first, silently reverting an observation already on disk.
        """
        import threading

        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("first", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)

        real_write = hazards.atomic_write
        entered = threading.Event()
        release = threading.Event()

        def slow_write(path, text):
            # Hold the first writer inside the critical section long enough for a
            # second flush to attempt an overlap.
            if not entered.is_set():
                entered.set()
                release.wait(2)
            return real_write(path, text)

        hazards.atomic_write = slow_write  # type: ignore[assignment]
        try:
            t = threading.Thread(target=led.flush)
            t.start()
            assert entered.wait(2), "the first flush never reached the write"
            # A newer observation lands, then a second flush races the first.
            led.record("second", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
            second = threading.Thread(target=led.flush)
            second.start()
            release.set()
            t.join(5)
            second.join(5)
        finally:
            hazards.atomic_write = real_write  # type: ignore[assignment]
            release.set()

        on_disk = hazards.load_ledger(tmp_path).as_dict()
        assert "first" in on_disk
        assert "second" in on_disk, "the newer observation was reverted by an older flush"

    def test_ledger_feeds_the_verdict(self, tmp_path) -> None:
        """End to end: what gatewayd observed withdraws the recommendation."""
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
        led.flush()

        observed = hazards.load_ledger(tmp_path).codes_for_name("srv")
        verdict = assess(
            ShareEvidence(
                name="srv", probe_ok=True, capabilities={}, observed_hazards=observed
            )
        )
        assert verdict.strength is Strength.REFUTED


class TestHostileRecordNumbers:
    """A record file is not a protocol message -- nothing upstream validates it.

    Both records are loaded during gateway startup, so a loader that raises
    takes the daemon down before it binds its socket. ``isinstance(v, (int,
    float))`` reads as a sufficient guard and is not: JSON parses a bare number
    into ``int``, which has no size limit, and ``float()`` of a large enough one
    raises ``OverflowError`` -- an ``ArithmeticError``, so it escapes a guard
    spelled ``except (TypeError, ValueError)``.
    """

    #: 310 digits: past the largest representable double, still valid JSON.
    HUGE = "9" * 310

    def test_overflowing_int_is_not_a_float_conversion_away(self) -> None:
        """The premise, stated as a test so a Python change cannot silently void it."""
        import json

        parsed = json.loads(f'{{"n": {self.HUGE}}}')["n"]
        assert isinstance(parsed, int)
        with pytest.raises(OverflowError):
            float(parsed)
        assert not issubclass(OverflowError, (TypeError, ValueError))

    def test_the_string_form_yields_infinity_instead_of_raising(self) -> None:
        """The other half: no exception, but a timestamp nothing can order."""
        assert float(self.HUGE) == float("inf")
        assert finite_float(float("inf")) == 0.0
        assert finite_float(float("nan")) == 0.0

    def test_a_huge_timestamp_does_not_take_the_ledger_down(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text(
            '{"schema": 1, "servers": {"srv": {"codes": ["%s"], "count": 1,'
            ' "lastSeen": %s}}}'
            % (hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self.HUGE),
            encoding="utf-8",
        )

        led = hazards.load_ledger(tmp_path)

        assert led.codes_for_name("srv") == (hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,)

    def test_a_huge_timestamp_does_not_take_the_verdict_cache_down(self, tmp_path) -> None:
        vc.cache_path(tmp_path).write_text(
            '{"schema": 1, "entries": {"srv\\u0000c\\u0000e\\u0000b\\u00001":'
            ' {"ran": true, "callerSensitive": false, "reasons": [],'
            ' "evaluatedAt": %s}}}' % self.HUGE,
            encoding="utf-8",
        )

        cache = vc.load_cache(tmp_path)

        assert len(cache) == 1

    def test_bool_is_not_accepted_as_a_number(self) -> None:
        """``bool`` is an ``int`` subclass, so a JSON ``true`` would read as 1.0."""
        assert finite_float(True) == 0.0
        assert finite_float(False) == 0.0


class TestNameCollisionDoesNotInheritEvidence:
    """One server owns one row, so the reader is a direct lookup.

    The ambiguity that matters is one NAME covering two different launches in the
    merged agent config: the rows merge, and serving the measured definition's
    verdict to the merged row would hand an unmeasured one its
    ``recommend_share``. That is decided in the row loop, where both definitions
    are visible — not here.
    """

    @staticmethod
    def _write_row(tmp_path: Path, name: str, ident: vc.Identity) -> None:
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(name, ident, vc.CachedPreflight(True, False, (), 1.0))
        cache.flush()

    def test_a_stored_row_is_served_by_name(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import mcp as handler

        self._write_row(tmp_path, "srv", _ident(command_args_hash="cmdA"))
        monkeypatch.setattr(handler, "records_dir", lambda *_a, **_k: tmp_path)

        _observed, preflights = handler._load_shareability_state()

        assert preflights.get("srv") == (True, False)

    def test_a_changed_identity_overwrites_rather_than_accumulating(
        self, tmp_path, monkeypatch
    ) -> None:
        """Editing a server's command leaves ONE row, carrying the new measurement.

        Keying by identity would have left the superseded row behind, which is what
        forced a size cap, an eviction policy, and a newest-wins rule on every
        reader. There is no history to sort through now.
        """
        from kiro_crew.dashboard.handlers import mcp as handler

        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(
            "srv",
            _ident(command_args_hash="old"),
            vc.CachedPreflight(True, True, ("caller_sensitive_initialize",), 1.0),
        )
        cache.put(
            "srv",
            _ident(command_args_hash="new"),
            vc.CachedPreflight(True, False, (), 2.0),
        )
        cache.flush()
        assert len(vc.load_cache(tmp_path)) == 1
        monkeypatch.setattr(handler, "records_dir", lambda *_a, **_k: tmp_path)

        _observed, preflights = handler._load_shareability_state()

        assert preflights.get("srv") == (True, False), "the newest measurement wins"

    def test_the_same_definition_in_many_agents_is_not_ambiguous(self) -> None:
        """Listing one server in several agents is the ordinary case.

        Withholding on agent COUNT rather than on distinct launches would cost a
        correct recommendation for every server an operator shares across agents,
        which is most of them. The row keys on the launch hash for that reason.
        """
        from kiro_crew.mcp_gateway.hashing import hash_command

        assert len({hash_command("/bin/a", ["--x"]) for _ in range(5)}) == 1
        assert len({hash_command("/bin/a", []), hash_command("/bin/b", [])}) == 2


class TestNoBlockingRecordIoOnTheLoop:
    """One invariant, not one assertion per site.

    Every filesystem touch of the two shareability records happens off the event
    loop. Three separate rounds of this same finding — the row builder, the
    evaluation pass, then daemon startup — were each closed by patching the
    instance that was reported, because the rule named the blocking helpers in a
    hand-written list. A helper missing from that list was invisible, so the
    fourth instance (deriving cache keys, the most expensive of them) passed a
    green ratchet.

    So the blocking set is DERIVED from the modules themselves: any helper that
    reaches a filesystem or subprocess primitive counts, whether or not anyone
    remembered to list it.
    """

    #: Modules whose helpers own the record files and the binaries behind them.
    RECORD_MODULES = (
        "kiro_crew.mcp_gateway.hazards",
        "kiro_crew.mcp_gateway.verdict_cache",
        "kiro_crew.mcp_gateway.evaluate",
    )

    #: Primitives that make a helper blocking. Deliberately spelled as bare
    #: attribute/function names, because that is how they appear at the call
    #: site regardless of how the module imported them.
    IO_PRIMITIVES = frozenset(
        {
            "open",
            "atomic_write",
            "read_text",
            "write_text",
            "read_bytes",
            "write_bytes",
            "mkdir",
            "unlink",
            "replace",
            "stat",
            "which",
            "run",
            "Popen",
            "binary_fingerprint",
            "communicate",
        }
    )

    #: (module, async function) pairs that reach the records.
    SITES = (
        ("kiro_crew.dashboard.handlers.mcp", "api_mcp_gateway_servers"),
        ("kiro_crew.dashboard.handlers.mcp", "_evaluate_shareability"),
        ("kiro_crew.mcp_gateway.evaluate", "evaluate_new_servers"),
    )

    @classmethod
    def _blocking_helpers(cls) -> set[str]:
        """Names of helpers in the record modules that reach an IO primitive.

        Transitive on purpose. ``load_cache`` touches no primitive itself — it
        delegates to ``VerdictCache.load``, which does. A derivation that only
        looked one level deep, or only at module-level functions, would call the
        public entry points non-blocking and wave through exactly the calls this
        rule exists to stop.
        """
        import ast
        import importlib
        import inspect

        calls: dict[str, set[str]] = {}
        module_level: set[str] = set()
        for module_name in cls.RECORD_MODULES:
            mod = importlib.import_module(module_name)
            tree = ast.parse(inspect.getsource(mod))
            module_level.update(
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                called: set[str] = set()
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call):
                        continue
                    func = inner.func
                    if isinstance(func, ast.Attribute):
                        called.add(func.attr)
                    elif isinstance(func, ast.Name):
                        called.add(func.id)
                calls.setdefault(node.name, set()).update(called)

        blocking = {n for n, c in calls.items() if c & cls.IO_PRIMITIVES}
        while True:
            grown = {n for n, c in calls.items() if c & blocking}
            if not grown <= blocking:
                blocking |= grown
                continue
            # Methods drive the transitivity above but are not reported: a bare
            # method name is not unique to these modules, and matching one by
            # name alone flagged ``KiroCrewConfig.load()`` — an unrelated loader
            # the whole codebase calls on the loop by design. What another module
            # can actually reach is a module-level entry point, so that is what
            # the caller scans for.
            return blocking & module_level

    def test_the_blocking_set_is_actually_derived(self) -> None:
        """Guard the guard: an empty derivation would pass every check below."""
        found = self._blocking_helpers()
        assert {"load_cache", "load_ledger", "identity_for"} <= found, found

    @pytest.mark.parametrize("module_name,func_name", SITES)
    def test_every_record_touch_is_offloaded(self, module_name: str, func_name: str) -> None:
        import importlib
        import inspect

        blocking = self._blocking_helpers() | {"flush", "install_sink"}
        mod = importlib.import_module(module_name)
        src = inspect.getsource(getattr(mod, func_name))
        for line in src.splitlines():
            code = line.split("#")[0]
            if "to_thread" in code:
                continue
            for name in blocking:
                assert f"{name}(" not in code, (
                    f"{module_name}.{func_name}: {name} does blocking IO and runs "
                    f"on the event loop -- offload it\n    {line.strip()}"
                )

    def test_the_key_derivation_refuses_to_run_on_the_loop(self) -> None:
        """Belt and braces: the ratchet reads source, this catches a live call.

        A reviewer can restructure the call into a shape the line scan does not
        recognise; this fires regardless of how it is spelled.
        """
        import asyncio

        from kiro_crew.mcp_gateway import evaluate

        async def call_it() -> None:
            evaluate.identity_for(
                SimpleNamespace(name="s", command="/bin/true", args=[], env={})
            )

        with pytest.raises(RuntimeError, match="must not run on the event loop"):
            asyncio.run(call_it())

    def test_gatewayd_installs_the_sink_off_loop(self) -> None:
        """Startup counts too: a slow ledger would delay socket readiness."""
        import inspect

        from kiro_crew.mcp_gateway import gatewayd

        src = inspect.getsource(gatewayd.run_gatewayd)
        line = next(ln for ln in src.splitlines() if "install_sink" in ln)
        assert "to_thread" in line, line.strip()

    def test_the_expensive_modules_stay_off_the_gateway_boot_path(self) -> None:
        """``evaluate`` must not be imported at module scope by the handler.

        The handler is on the gateway's boot path, and ``evaluate`` pulls in
        ``preflight`` -> ``mcp_discovery`` and ``stub`` (the stub PROCESS entry
        point). Measured on this tree, hoisting it added 8 modules to that path
        and pushed a startup loop-responsiveness ceiling over on Windows. The
        top-level-imports convention loses to a measured startup regression —
        this is a boot-path decision, NOT an unproven circular-import excuse.

        Asserted at module scope rather than by importing and counting, so the
        test states the rule instead of re-measuring a machine-dependent number.
        """
        import ast
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        tree = ast.parse(inspect.getsource(handler))
        banned = {
            "kiro_crew.mcp_gateway.evaluate",
            "kiro_crew.mcp_gateway.preflight",
            "kiro_crew.mcp_gateway.stub",
        }
        for node in tree.body:  # module scope only
            if isinstance(node, ast.ImportFrom) and node.module in banned:
                raise AssertionError(
                    f"{node.module} imported at module scope (line {node.lineno}) -- "
                    "that puts the stub/preflight chain on the gateway boot path"
                )

    def test_the_new_modules_have_no_function_local_imports(self) -> None:
        """Hoisted to module scope, and proven cycle-free in both load orders.

        A function-local import here is not a style nit: it hides a real cycle if
        one exists, and the convention is to prove the absence instead.
        """
        import ast
        import inspect

        from kiro_crew.mcp_gateway import evaluate, preflight, seed, verdict_cache

        for mod in (shareability, hazards, preflight, verdict_cache, seed, evaluate):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        raise AssertionError(
                            f"{mod.__name__}.{node.name} imports at function scope "
                            f"(line {inner.lineno}) -- hoist it to module scope"
                        )

    def test_assess_server_is_pure(self) -> None:
        """It takes the loaded state as arguments and opens nothing."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        src = inspect.getsource(handler._assess_server)
        for forbidden in ("load_cache", "load_ledger", "KiroCrewConfig", "open("):
            assert forbidden not in src, f"{forbidden} moved back into the row path"
        params = inspect.signature(handler._assess_server).parameters
        assert "preflight" in params, "preflight must be passed in, not looked up"
        assert "observed_hazards" in params

    def test_the_loader_is_awaited_off_loop_exactly_once(self) -> None:
        """One ``to_thread`` call, outside the row loop."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        src = inspect.getsource(handler.api_mcp_gateway_servers)
        assert src.count("_load_shareability_state") == 1
        assert "asyncio.to_thread(_load_shareability_state)" in src
        # The call must precede the loop that builds rows, or it is per-row again.
        assert src.index("_load_shareability_state") < src.index("for name in sorted(rows)")

    def test_cache_exposes_names(self) -> None:
        cache = vc.VerdictCache(vc.cache_path(Path("/nonexistent")))
        cache.put("a", _ident(), _verdict())
        cache.put("b", _ident(command_args_hash="other"), _verdict())
        assert cache.server_names() == {"a", "b"}


class TestHazardInvalidation:
    """A hazard must survive what does not change the program, and only that.

    The ledger's neighbour — the pre-flight cache — re-measures when a server's
    launch identity changes. Without the same rule here a single observation
    disqualifies a server for ever, so wiring a pooling refusal to this ledger
    would strand a server on evidence about a version it no longer runs.
    """

    @staticmethod
    def _ident(binary: str = "v1") -> str:
        return hazards.launch_identity("cmd1", "env1", binary)

    def test_an_upgrade_discards_the_prior_observation(self, tmp_path) -> None:
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident("v1"))
        assert led.codes_for("srv", self._ident("v1")) != ()

        # Same path, same args, new bytes: the evidence described the old build.
        assert led.codes_for("srv", self._ident("v2")) == ()

    def test_the_discard_is_a_replacement_not_an_accumulation(self, tmp_path) -> None:
        """A new identity starts clean — it does not inherit the old codes.

        Inheriting would make an upgrade look like it exhibited behaviour it
        never showed, which is the same permanence bug wearing a new identity.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident("v1"))
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self._ident("v2"))

        assert led.codes_for("srv", self._ident("v2")) == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
        )

    def test_a_draining_backend_cannot_erase_the_new_builds_evidence(
        self, tmp_path
    ) -> None:
        """Two identities are live at once during a blue/green drain.

        The pool keeps the outgoing build serving its attached stubs while the
        incoming one starts, so BOTH can still record. Collapsing them into one
        row per name let a late frame from the draining backend overwrite what
        the new build had already observed — and that reads as "nothing observed"
        for the build actually being kept, the permissive direction.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        new = self._ident("v2")
        old = self._ident("v1")

        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, new)
        # The outgoing backend emits one more frame AFTER the new build recorded.
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, old)

        assert led.codes_for("srv", new) == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        ), "the new build's evidence survived a late frame from the old one"
        assert led.codes_for("srv", old) == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
        ), "each identity keeps its own observations"

        led.flush()
        reloaded = hazards.load_ledger(tmp_path)
        assert reloaded.codes_for("srv", new) == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_an_unchanged_server_keeps_its_hazard(self, tmp_path) -> None:
        """The direction that must NOT clear — otherwise the ledger is useless.

        Two codes under ONE identity, because a single observation cannot tell a
        correct implementation from one that resets on every record: both would
        end up holding exactly the code just written.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident())
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self._ident())
        led.flush()

        reloaded = hazards.load_ledger(tmp_path)
        assert reloaded.codes_for("srv", self._ident()) == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        ), "observations under one identity accumulate, they do not replace"

    def test_identity_is_not_the_preflight_identity(self) -> None:
        """The two stores must not share an invalidation trigger.

        ``verdict_cache.Identity`` folds in the pre-flight schema, because a
        measurement is void when the way we measure changes. A hazard is an
        observation of real traffic and stays true however the prober evolves, so
        sharing that field would let a schema bump erase witnessed evidence.
        """
        launch = hazards.launch_identity("cmd1", "env1", "1.0.0")
        preflight = vc.Identity("cmd1", "env1", "1.0.0").as_str()
        assert launch != preflight
        assert str(vc.SCHEMA) not in launch.split("\u0000")

    def test_a_legacy_row_reads_as_unobserved_but_still_shows(self, tmp_path) -> None:
        """A v1 file still loads; its rows just cannot be attributed.

        They must not be deleted — that would discard real evidence to add a
        field — and must not be trusted for a launch either, so they land under
        the empty identity: invisible to the checked read, still surfaced by the
        name-only one so the dashboard keeps showing the withdrawal.
        """
        (tmp_path / hazards.HAZARDS_FILENAME).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "servers": {
                        "srv": {
                            "codes": [hazards.HAZARD_UNROUTABLE_SERVER_REQUEST],
                            "count": 1,
                            "lastSeen": 1.0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        led = hazards.load_ledger(tmp_path)
        assert led.codes_for("srv", self._ident()) == ()
        assert led.codes_for_name("srv") == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_an_older_reader_still_sees_the_evidence(self, tmp_path) -> None:
        """The written file must not lock out a build that predates identities.

        Make Live can put an earlier worktree back in front of the same data
        home, so a version-gated shape change would read as "future, ignore"
        there and silently drop every observation. The writer therefore keeps the
        schema and emits the flat union alongside the nested map.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident("v1"))
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self._ident("v2"))
        led.flush()

        raw = json.loads((tmp_path / hazards.HAZARDS_FILENAME).read_text())
        assert raw["schema"] == 1, "a bump would make an older reader ignore the file"
        entry = raw["servers"]["srv"]
        # What a reader that knows nothing about identities parses:
        assert entry["codes"] == [
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        ], "the flat union carries every code an older reader would need"
        # ...and the shape this build actually reads, unaffected by it:
        assert set(entry["identities"]) == {self._ident("v1"), self._ident("v2")}

    def test_clear_drops_evidence_that_is_still_current(self, tmp_path) -> None:
        """The escape hatch identity cannot provide.

        A frame this gateway misattributed is our defect; fixing it changes
        nothing about the server, so without an explicit clear the record would
        stand for ever against a server that never misbehaved.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident())
        led.flush()

        assert led.clear("srv") is True
        assert led.clear("srv") is False, "a second clear reports nothing to forget"
        led.flush()
        assert hazards.load_ledger(tmp_path).codes_for_name("srv") == ()

    def test_nothing_expires_by_age(self, tmp_path) -> None:
        """Ageing an observation out would manufacture a safety nothing observed.

        A server that personalises state per caller does not become safe because
        time passed, so a very old hazard on an UNCHANGED identity still stands.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident())
        # Backdate well past any plausible expiry window.
        led._records["srv"][self._ident()].last_seen = 1.0
        led.flush()

        assert hazards.load_ledger(tmp_path).codes_for("srv", self._ident()) == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_the_recording_site_stamps_the_pool_key(self) -> None:
        """The identity must come from the launch, with no IO on the loop.

        ``PoolKey`` already carries the three fingerprints, which is what makes
        stamping free at a site that runs on the event loop.
        """
        from kiro_crew.mcp_gateway import backend as backend_mod

        src = inspect.getsource(backend_mod.Backend._record_hazard)
        assert "launch_identity" in src
        assert "key.command_args_hash" in src and "key.binary_version" in src
        for forbidden in ("open(", "read_bytes", "sha256", "to_thread"):
            assert forbidden not in src, f"{forbidden} would put IO on the loop"


class TestRecordsDirIsNotGatedOnABroker:

    """A machine that never enabled stubbing must still get verdicts.

    Deriving the records directory from ``mcp_gateway.socket_path`` made the
    whole feature inert for exactly its audience: that field is empty until a
    broker is configured, and the recommendation exists to tell an operator
    whether configuring one is safe.
    """

    def test_unconfigured_socket_still_resolves_a_directory(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.mcp_gateway.rewriter import records_dir, runtime_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert records_dir("") == runtime_dir()
        assert records_dir("") == tmp_path / "mcp-gateway"

    def test_configured_socket_wins_so_writer_and_reader_agree(self, tmp_path) -> None:
        """gatewayd writes beside its real socket; the page must read there."""
        from kiro_crew.mcp_gateway.rewriter import records_dir

        sock = tmp_path / "custom" / "gateway.sock"
        assert records_dir(str(sock)) == sock.parent
        # gatewayd passes a Path, the dashboard passes a str — both must work.
        assert records_dir(sock) == sock.parent

    def test_an_empty_path_does_not_resolve_to_the_cwd(self, monkeypatch, tmp_path) -> None:
        """``Path("")`` is ``PosixPath(".")`` and therefore truthy.

        Branching on the Path instead of its string form would put the hazard
        ledger in whatever directory the process happened to start in.
        """
        from kiro_crew.mcp_gateway.rewriter import records_dir, runtime_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert records_dir(Path("")) == runtime_dir()
        assert records_dir(None) == runtime_dir()  # type: ignore[arg-type]
        assert records_dir(".") == runtime_dir()
        # But a REAL relative socket keeps its own directory — the guard is on
        # the input being bare ".", not on the parent resolving to ".".
        assert records_dir("gateway.sock") == Path(".")

    def test_the_default_socket_lives_in_that_same_directory(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.mcp_gateway.rewriter import default_socket_path, runtime_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert default_socket_path().parent == runtime_dir()

    def test_neither_reader_nor_trigger_short_circuits_on_an_empty_socket(self) -> None:
        """Ratchet: the early `if not socket_path: return` must not come back."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        for fn in (handler._load_shareability_state, handler._evaluate_shareability):
            src = inspect.getsource(fn)
            assert "records_dir" in src, f"{fn.__name__} must use the shared resolver"
            assert "if not socket_path" not in src, f"{fn.__name__} regained the broker gate"


@pytest.mark.parametrize(
    "evidence",
    [
        ShareEvidence(name="a", probe_ok=False),
        ShareEvidence(name="b", probe_ok=True, capabilities={}),
        ShareEvidence(name="c", is_stdio=False),
    ],
)
def test_to_dict_is_json_serialisable(evidence: ShareEvidence) -> None:
    """The verdict crosses an HTTP boundary; it must survive json.dumps."""
    json.dumps(assess(evidence).to_dict())


class TestBackendRecordsHazards:
    """The writer side. Without these the ledger is a declaration, not a signal."""

    @staticmethod
    def _backend(server_name: str, *, exclusive_token: str = "", refcount: int = 2) -> object:
        """A Backend-shaped stand-in for the fields ``_record_hazard`` reads.

        Deliberately not a MagicMock: the point is to pin that the method reads
        ``pool_key.server_name``, the three launch fingerprints,
        ``exclusive_token`` and ``refcount`` — the REAL field names on
        ``Backend`` and ``PoolKey`` — so a rename cannot leave this passing
        against a mock that would answer to anything. That the fingerprints are
        already ON the pool key is what lets the recording site stamp an identity
        without doing IO on the event loop.

        ``refcount`` defaults to 2 because that is the only state in which a
        hazard means anything: two clients attached, so an unattributable frame
        could have reached the wrong one.
        """
        from kiro_crew.mcp_gateway.backend import Backend

        obj = object.__new__(Backend)
        obj.pool_key = SimpleNamespace(  # type: ignore[attr-defined]
            server_name=server_name,
            command_args_hash="cmd1",
            effective_env_hash="env1",
            binary_version="v1",
        )
        obj.exclusive_token = exclusive_token  # type: ignore[attr-defined]
        obj.refcount = refcount  # type: ignore[attr-defined]
        return obj

    def test_shared_backend_records(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(hazards.ledger_path(tmp_path)))
        backend = self._backend("srv")
        backend._record_hazard(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)  # type: ignore[attr-defined]
        hazards.flush_sink()
        assert hazards.load_ledger(tmp_path).codes_for_name("srv") == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_exclusive_backend_does_not_record(self, tmp_path, monkeypatch) -> None:
        """A 1:1 backend legitimately owns its single client.

        The same frame there proves nothing about shareability, so recording it
        would disqualify servers for behaviour that is correct when unshared.
        """
        monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(hazards.ledger_path(tmp_path)))
        backend = self._backend("srv", exclusive_token="stub-uuid")
        backend._record_hazard(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)  # type: ignore[attr-defined]
        hazards.flush_sink()
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    @pytest.mark.parametrize("refcount", [0, 1])
    def test_a_pooled_backend_with_one_client_does_not_record(
        self, tmp_path, monkeypatch, refcount: int
    ) -> None:
        """Being poolable is not the same as currently serving two clients.

        A pooled backend serves exactly one client from the moment it starts
        until a second stub attaches, and none at all between sessions. An
        unattributable frame in either state had no second tenant to reach, so it
        says nothing about shareability — and a hazard is permanent, so recording
        one here would kill a server for behaviour that is correct.
        """
        monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(hazards.ledger_path(tmp_path)))
        backend = self._backend("srv", refcount=refcount)
        backend._record_hazard(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)  # type: ignore[attr-defined]
        hazards.flush_sink()
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_no_sink_installed_is_a_silent_no_op(self, monkeypatch) -> None:
        """The stub process and unit tests never install a sink."""
        monkeypatch.setattr(hazards, "_sink", None)
        assert hazards.record_observed("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST) is False
        hazards.flush_sink()  # must not raise

    def test_both_observation_sites_call_the_recorder(self) -> None:
        """Ratchet: the two hazard sites must stay wired to the ledger.

        Asserted on the source because reproducing either frame end to end
        needs a live shared backend and a misbehaving server; the value here is
        catching a future edit that deletes the call while keeping the log line.
        """
        from pathlib import Path as _Path

        import kiro_crew.mcp_gateway.backend as backend_mod

        src = _Path(backend_mod.__file__).read_text(encoding="utf-8")
        assert src.count("self._record_hazard(") == 2, "expected exactly two hazard sites"
        assert "hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION" in src
        assert "hazards.HAZARD_UNROUTABLE_SERVER_REQUEST" in src
