"""The recalled-memory egress scope: what it records, what it refuses, what it preserves.

``memory.recall`` sends a category neither ``skills.select`` nor ``tool.risk`` did --
the TEXT of memories the agent wrote down in earlier conversations -- and an owner who
consented before that point existed consented to a request carrying a message excerpt
and candidate skill descriptions. A message excerpt is text they just typed and a
skill description is text this build shipped; a recalled memory is neither. So consent
to SEND is not consent to send this, and the keystone records the two separately.

The claim these tests exist for is the one a reader cannot check by looking: an install
that is already consented and has never seen the new switch must be INERT for the new
point, not retroactively signed up. That is asserted from both ends -- the reader's
default, and the gate's refusal on a real keystone.

The second claim is that the two scopes are INDEPENDENT. An owner may want risky tool
calls flagged without the contents of their memory store leaving the machine, so
granting one must not grant the other, in either order.

This suite is the ``tool_args`` one (``test_decisions_tool_args_scope.py``) adapted to
the second scope, deliberately: the two scopes are the same mechanism, and asserting
them in the same shape is what makes a divergence between them visible.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.decisions import consent, gate

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


def _config():
    """A snapshot the gate can read: consent is on, the bucket admits everyone."""
    provider = SimpleNamespace(endpoint=DEFAULT_ENDPOINT, model="", timeout_ms=1000, api_key="")
    return SimpleNamespace(
        decisions=SimpleNamespace(bucket=100, provider=provider, history_budget_chars=0)
    )


# ── the reader's default is the whole protection ───────────────────────────────


class TestTheDefaultIsNarrowest:
    def test_an_absent_scope_reads_as_not_consented(self, keystone):
        """Every consent recorded before this key existed lands here."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        assert consent.is_enabled() is True
        assert consent.permits(DEFAULT_ENDPOINT) is True
        assert consent.consented_memory_text() is False

    @pytest.mark.parametrize("value", [None, 0, 1, "true", "yes", [], {}, "True"])
    def test_only_a_literal_true_consents(self, keystone, value):
        """A truthy stand-in is not a deliberate yes about a new egress category."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": value}),
            encoding="utf-8",
        )
        assert consent.consented_memory_text() is False

    def test_a_literal_true_consents(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": True}),
            encoding="utf-8",
        )
        assert consent.consented_memory_text() is True

    def test_an_unreadable_keystone_reads_as_not_consented(self, keystone):
        keystone.write_text("{not json", encoding="utf-8")
        assert consent.consented_memory_text() is False

    def test_the_tool_scope_does_not_grant_this_one(self, keystone):
        """The two are independent decisions, so neither stands in for the other."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": True}),
            encoding="utf-8",
        )
        assert consent.consented_tool_args() is True
        assert consent.consented_memory_text() is False

    def test_this_scope_does_not_grant_the_tool_one(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": True}),
            encoding="utf-8",
        )
        assert consent.consented_memory_text() is True
        assert consent.consented_tool_args() is False


class TestEveryScopeReaderResolves:
    def test_each_map_entry_names_a_real_consent_reader(self):
        """The table holds a NAME, resolved at call time, so a typo is a silent refusal.

        `_scope_consented` catches the AttributeError and fails closed, which is the
        right direction and an invisible one: the point would simply never fire. This
        is the test that makes it visible instead.
        """
        assert gate._POINT_SCOPES, "the table must not be empty"
        for name, (reader_name, category) in gate._POINT_SCOPES.items():
            reader = getattr(consent, reader_name, None)
            assert callable(reader), f"{name} names no consent reader: {reader_name}"
            assert reader({}) is False, f"{reader_name} must read an empty state as no"
            assert category, name


# ── the writer's round trip ────────────────────────────────────────────────────


class TestTheRoundTrip:
    def test_enabling_without_mentioning_it_grants_nothing(self, keystone):
        state = consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        assert state["memory_text"] is False
        assert consent.consented_memory_text() is False

    def test_it_records_the_scope_it_was_given(self, keystone):
        state = consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        assert state["memory_text"] is True
        assert consent.consented_memory_text() is True
        # On the sealed record, not just in the returned dict.
        assert json.loads(keystone.read_text())["memory_text"] is True

    def test_the_keep_sentinel_preserves_a_recorded_scope(self, keystone):
        """An ordinary switch flip must not erase it by omission."""
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=consent.KEEP_MEMORY_TEXT)
        assert consent.consented_memory_text() is True

    def test_the_keep_sentinel_does_not_invent_one(self, keystone):
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=consent.KEEP_MEMORY_TEXT)
        assert consent.consented_memory_text() is False

    def test_an_explicit_false_revokes_it(self, keystone):
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=False)
        assert consent.consented_memory_text() is False

    def test_disabling_clears_it_so_a_re_enable_cannot_inherit_it(self, keystone):
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT)
        assert consent.consented_memory_text() is False
        # And the re-enable starts from the narrowest scope again.
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=consent.KEEP_MEMORY_TEXT)
        assert consent.consented_memory_text() is False

    @pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {}])
    def test_a_non_boolean_scope_is_refused_not_coerced(self, keystone, bad):
        with pytest.raises(ValueError):
            consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=bad)

    def test_granting_one_scope_leaves_the_other_alone(self, keystone):
        """Both directions, because a shared write would break exactly one of them."""
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        consent.save_enabled(
            True,
            endpoint=DEFAULT_ENDPOINT,
            tool_args=True,
            memory_text=consent.KEEP_MEMORY_TEXT,
        )
        assert consent.consented_memory_text() is True
        assert consent.consented_tool_args() is True
        consent.save_enabled(
            True,
            endpoint=DEFAULT_ENDPOINT,
            tool_args=False,
            memory_text=consent.KEEP_MEMORY_TEXT,
        )
        assert consent.consented_memory_text() is True
        assert consent.consented_tool_args() is False


# ── the gate refuses the point, and only that point ───────────────────────────


class TestTheGateRefusesWithoutIt:
    @pytest.fixture(autouse=True)
    def _forget_warnings(self):
        """The once-per-point warning is process state; a test must not inherit it."""
        gate._unscoped_warned.clear()
        yield
        gate._unscoped_warned.clear()

    def test_the_recalling_point_is_refused_and_the_others_are_not(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        assert gate.is_enabled("skills.select", config=_config()) is True
        assert gate.is_enabled("message.steer", config=_config()) is True
        assert gate.is_enabled("memory.recall", config=_config()) is False

    def test_the_scope_admits_it(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": True}),
            encoding="utf-8",
        )
        assert gate.is_enabled("memory.recall", config=_config()) is True

    def test_the_tool_scope_does_not_admit_it(self, keystone):
        """The map keys each point to ITS OWN reader, not to "some scope is on"."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": True}),
            encoding="utf-8",
        )
        assert gate.is_enabled("tool.risk", config=_config()) is True
        assert gate.is_enabled("memory.recall", config=_config()) is False

    def test_the_refusal_is_said_out_loud_once_and_names_the_category(self, keystone, caplog):
        """An owner whose feature does nothing needs to know what was not consented to."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        with caplog.at_level("WARNING"):
            for _ in range(4):
                gate.is_enabled("memory.recall", config=_config())
        said = [r for r in caplog.records if "recalled memories" in r.getMessage()]
        assert len(said) == 1, "once per point, not once per turn"
        assert "memory.recall" in said[0].getMessage()
        assert "turn on that switch in Settings" in said[0].getMessage()

    @pytest.mark.asyncio
    async def test_decide_sends_nothing_for_the_unscoped_point(self, keystone, monkeypatch):
        """The refusal is on ``decide`` too, not only on the cheap preflight.

        ``is_enabled`` grants nothing by contract -- ``decide`` re-runs every refusal
        -- so a scope enforced only in the preflight would be no scope at all.
        """
        from kiro_crew.decisions.points import memory_recall as mr

        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        asked: list = []

        class _Oracle:
            def __init__(self, _provider):
                pass

            async def ask(self, state, questions):
                asked.append(state)
                raise AssertionError("nothing may be sent for an unscoped point")

        monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", _Oracle)
        rows = [{"key": "mem-a", "snippet": "an episode"}]
        answers = await gate.decide(
            mr.POINT, mr.build_state("hi", rows), mr.build_questions(rows), config=_config()
        )
        assert answers is None
        assert asked == []

    def test_the_point_keeps_the_similarity_topk_without_the_scope(self, keystone, monkeypatch):
        """End to end through the point: the block is the one similarity chose.

        And NO row is written. The scope refusal arrives as one of the gate's three
        cheap refusals, which touch no disk by design -- the property that makes "no
        consent leaves the log directory empty" checkable. A row here would trade
        that for a diagnostic the application log already carries as the
        once-per-point warning above.
        """
        import threading

        from kiro_crew.decisions import log as _log
        from kiro_crew.decisions.points import memory_recall as mr

        log_dir = keystone.parent / "decisions"
        monkeypatch.setattr(_log, "log_dir", lambda: log_dir)
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        monkeypatch.setattr(gate, "_snapshot", lambda: _config())

        import asyncio

        loop = asyncio.new_event_loop()
        ready = threading.Event()
        loop.call_soon(ready.set)
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            assert ready.wait(10)
            kept = mr.kept_memories(
                [{"id": "mem-a", "text": "an episode"}],
                "hi",
                session_key="chat-1",
                loop=loop,
                owner_turn=True,
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()
        assert kept is None, "the similarity top-k is injected unchanged"
        assert not log_dir.exists(), "a cheap refusal writes nothing at all"


# ── the route reports and preserves it ─────────────────────────────────────────


def _request(body=None):
    """A request shaped like a real dashboard OWNER call to the consent route."""
    request = MagicMock()
    request.path = "/api/decisions/consent"
    store = {"app": "", "user": "owner-1"}
    request.get = lambda key, default=None: store.get(key, default)
    request.__contains__ = lambda _self, key: key in store
    request.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    request.app = {"state": state}
    request.query = {}
    request.json = AsyncMock(return_value=body if body is not None else {})
    return request


@pytest.fixture
def audited(quiet_route, monkeypatch):
    """The rows the handler hands SEL, so the AUDIT VERB can be read off a real PUT."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


@pytest.fixture
def quiet_route(monkeypatch):
    """Silence the SEL audit and pin the configured endpoint the PUT must echo."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())
    monkeypatch.setattr(gate, "configured_endpoint", lambda *_a, **_kw: DEFAULT_ENDPOINT)
    monkeypatch.setattr(
        "kiro_crew.decisions.capability.is_decisions_denied", lambda *_a, **_kw: False
    )


class TestTheRoute:
    @pytest.mark.asyncio
    async def test_the_get_reports_the_scope(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_get

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_get(_request())
        assert json.loads(resp.text)["memory_text"] is True

    @pytest.mark.asyncio
    async def test_the_put_records_it(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": True})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["memory_text"] is True
        assert consent.consented_memory_text() is True

    @pytest.mark.asyncio
    async def test_an_omitted_field_preserves_the_recorded_scope(self, keystone, quiet_route):
        """The card writes ``enabled`` and ``endpoint`` alone on an ordinary flip."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["memory_text"] is True
        assert consent.consented_memory_text() is True

    @pytest.mark.asyncio
    async def test_granting_one_scope_through_the_route_preserves_the_other(
        self, keystone, quiet_route
    ):
        """The card sends ONE scope per click, so the other must survive untouched."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": True})
        )
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["memory_text"] is True
        assert body["tool_args"] is True

    @pytest.mark.asyncio
    async def test_an_explicit_false_revokes_it(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": False})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["memory_text"] is False
        assert consent.consented_memory_text() is False

    @pytest.mark.asyncio
    async def test_disabling_through_the_route_clears_it(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(_request({"enabled": False}))
        assert resp.status == 200
        assert json.loads(resp.text)["memory_text"] is False
        assert consent.consented_memory_text() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["true", 1, 0, [], {}])
    async def test_a_truthy_stand_in_is_a_400_not_a_silent_yes(self, keystone, quiet_route, bad):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "memory_text": bad})
        )
        assert resp.status == 400
        assert consent.consented_memory_text() is False

    @pytest.mark.asyncio
    async def test_a_scope_only_put_after_a_disable_leaves_consent_off(self, keystone, quiet_route):
        """A scope write must not re-commit a consent the owner withdrew.

        Sending `enabled` alongside a scope is a write against a switch the owner did not
        touch, and a read taken before a concurrent revoking PUT is stale -- so such a
        write can assert a consent that is gone. A scope-only body says nothing about
        consent, and the writer preserves the recorded flag.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT)
        assert consent.is_enabled() is False

        # The scope write lands AFTER the disable, carrying no `enabled`.
        resp = await api_decisions_consent_put(_request({"memory_text": True}))
        assert resp.status == 200
        assert consent.is_enabled() is False, "a scope write must not re-commit consent"
        assert json.loads(resp.text)["enabled"] is False

    @pytest.mark.asyncio
    async def test_a_scope_only_put_preserves_the_recorded_endpoint(self, keystone, quiet_route):
        """Consent is bound to an ADDRESS, so preserving one without the other would
        leave a keystone consenting to nowhere -- which `permits` reads as off."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        resp = await api_decisions_consent_put(_request({"memory_text": True}))
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["enabled"] is True
        assert body["endpoint"] == DEFAULT_ENDPOINT
        assert body["permits"] is True
        assert consent.consented_memory_text() is True

    @pytest.mark.asyncio
    async def test_a_scope_only_put_needs_no_endpoint_echo(self, keystone, quiet_route):
        """The echo is what an ENABLING write owes, because it is consenting to an
        address. A scope write is not, so demanding one would make the switch unusable."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        resp = await api_decisions_consent_put(_request({"tool_args": True}))
        assert resp.status == 200
        assert consent.consented_tool_args() is True

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_still_a_400(self, keystone, quiet_route):
        """Absent `enabled` is an omission; a non-object body is a malformed request."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(_request("nope"))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_a_scope_grant_is_refused_under_a_governance_pin(
        self, keystone, quiet_route, monkeypatch
    ):
        """The ceiling is held against what a write GRANTS, not against `enabled`.

        A scope-only write asserts nothing about consent, but turning a scope ON under a
        pin would still record an egress permission the fleet withdrew.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        monkeypatch.setattr(
            "kiro_crew.decisions.capability.is_decisions_denied", lambda *_a, **_kw: True
        )
        resp = await api_decisions_consent_put(_request({"memory_text": True}))
        assert resp.status == 403
        assert consent.consented_memory_text() is False

    @pytest.mark.asyncio
    async def test_a_scope_REVOKE_still_works_under_a_pin(self, keystone, quiet_route, monkeypatch):
        """Same reason a disabling PUT stays available: trapping an owner with a record
        they cannot clear is worse than the record."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        monkeypatch.setattr(
            "kiro_crew.decisions.capability.is_decisions_denied", lambda *_a, **_kw: True
        )
        resp = await api_decisions_consent_put(_request({"memory_text": False}))
        assert resp.status == 200
        assert consent.consented_memory_text() is False

    @pytest.mark.asyncio
    async def test_the_route_hands_the_keep_sentinel_down_rather_than_a_boolean(
        self, keystone, quiet_route, monkeypatch
    ):
        """Resolving the omitted scope here would restore one a concurrent PUT cleared.

        The same race the history ceiling documents, and the reason the writer owns
        the resolution: a route that read the scope itself would hold a value read
        BEFORE a revocation landed and write it back.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        seen: list = []
        real = consent.save_enabled

        def _spy(
            enabled,
            *,
            endpoint,
            history_budget_chars=0,
            tool_args=False,
            compaction=False,
            memory_text=False,
        ):
            seen.append(memory_text)
            return real(
                enabled,
                endpoint=endpoint,
                history_budget_chars=history_budget_chars,
                tool_args=tool_args,
                compaction=compaction,
                memory_text=memory_text,
            )

        monkeypatch.setattr(consent, "save_enabled", _spy)
        await api_decisions_consent_put(_request({"enabled": True, "endpoint": DEFAULT_ENDPOINT}))

        assert seen == [consent.KEEP_MEMORY_TEXT], "the sentinel, not a resolved boolean"


class TestTheAuditVerbNamesWhatTheWriteAsserted:
    """One row per consent write, and the verb has to say which way this one went.

    The row is what an auditor reconstructs "when did egress start" from, so a write
    that TURNS THE SCOPE OFF must not read as a grant just because the consent switch it
    left alone is still on.
    """

    @pytest.mark.asyncio
    async def test_a_scope_only_revocation_audits_as_revoked(self, keystone, audited):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(_request({"memory_text": False}))

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["revoked"]
        assert "memory_text=False" in audited[-1]["resources"]
        assert consent.is_enabled() is True, "consent itself is untouched by a scope write"

    @pytest.mark.asyncio
    async def test_a_scope_only_grant_audits_as_granted(self, keystone, audited):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=False)
        resp = await api_decisions_consent_put(_request({"memory_text": True}))

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["granted"]

    @pytest.mark.asyncio
    async def test_a_body_naming_nothing_is_refused_before_any_verb(self, keystone, audited):
        """The route will not read-modify-write on a body that asks for nothing.

        So the verb's "asserts nothing" case is not reachable through the route, and an
        empty or misspelled body is a refusal rather than a silent rewrite of the record
        as itself. The scope stays exactly as it was.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(_request({}))

        assert resp.status == 400
        assert [r["outcome"] for r in audited] == ["denied"]
        assert consent.consented_memory_text() is True

    def test_the_verb_is_neither_when_the_write_carried_only_sentinels(self):
        """The third case, as a claim about the rule rather than about the route.

        A value that is not a boolean is a KEEP sentinel, meaning the field was absent,
        so it is not counted -- and a call carrying none of them asserts nothing.
        """
        from kiro_crew.dashboard.handlers.decisions import _consent_write_verb

        assert _consent_write_verb() == "allowed"
        assert _consent_write_verb(consent.KEEP_ENABLED, consent.KEEP_MEMORY_TEXT) == "allowed"
        assert _consent_write_verb(False, consent.KEEP_MEMORY_TEXT) == "revoked"
        assert _consent_write_verb(False, True) == "granted"

    @pytest.mark.asyncio
    async def test_raising_the_history_ceiling_audits_as_granted(self, keystone, audited):
        """The ceiling is an egress width, so a bigger one is a grant like any other.

        The scope rides along at ``False`` because the route refuses a body naming
        neither the switch nor a scope. It is the REVOKING value, so the grant this
        asserts can only have come from the ceiling.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=100)
        resp = await api_decisions_consent_put(
            _request({"memory_text": False, "history_budget_chars": 5_000})
        )

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["granted"]
        assert "history_budget_chars=5000" in audited[-1]["resources"]

    @pytest.mark.asyncio
    async def test_clearing_the_history_ceiling_audits_as_revoked(self, keystone, audited):
        """`0` takes prior turns back, which is a revocation and not a bare read-write."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=5_000)
        resp = await api_decisions_consent_put(
            _request({"memory_text": False, "history_budget_chars": 0})
        )

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["revoked"]

    @pytest.mark.asyncio
    async def test_disabling_beside_a_ceiling_still_audits_as_revoked(self, keystone, audited):
        """The writer forces the ceiling to 0 on a disabling write, so it widens nothing."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=100)
        resp = await api_decisions_consent_put(
            _request({"enabled": False, "history_budget_chars": 9_000}),
        )

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["revoked"]
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_a_mixed_write_names_the_widest_thing_it_did(self, keystone, audited):
        """Granting one scope while revoking another is a grant, and `resources` has both."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True, tool_args=False)
        resp = await api_decisions_consent_put(
            _request({"memory_text": False, "tool_args": True}),
        )

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["granted"]
        assert "memory_text=False" in audited[-1]["resources"]
        assert "tool_args=True" in audited[-1]["resources"]

    @pytest.mark.asyncio
    async def test_disabling_consent_still_audits_as_revoked(self, keystone, audited):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, memory_text=True)
        resp = await api_decisions_consent_put(_request({"enabled": False}))

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["revoked"]

    @pytest.mark.asyncio
    async def test_enabling_consent_audits_as_granted(self, keystone, audited):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT}),
        )

        assert resp.status == 200
        assert [r["outcome"] for r in audited] == ["granted"]
