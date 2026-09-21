"""The tool-argument egress scope: what it records, what it refuses, what it preserves.

``tool.risk`` sends a category ``skills.select`` never did -- the tool's name and its
arguments -- and an owner who consented before that point existed consented to a
request carrying a message excerpt and candidate descriptions. So consent to SEND is
not consent to send this, and the keystone records the two separately.

The claim these tests exist for is the one a reader cannot check by looking: an
install that is already consented and has never seen the new switch must be INERT for
the new point, not retroactively signed up for it. That is asserted here from both
ends -- the reader's default, and the gate's refusal on a real keystone.

The preservation rules are the other half, and they mirror ``history_budget_chars``
exactly: an OMITTED field on a PUT leaves the recorded scope alone, so an ordinary
switch flip can neither grant nor erase it, and disabling clears it so a re-enable
cannot inherit a scope nobody re-reviewed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.decisions import consent, gate
from kiro_crew.decisions.points import tool_risk as tr

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
        assert consent.consented_tool_args() is False

    @pytest.mark.parametrize("value", [None, 0, 1, "true", "yes", [], {}, "True"])
    def test_only_a_literal_true_consents(self, keystone, value):
        """A truthy stand-in is not a deliberate yes about a new egress category."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": value}),
            encoding="utf-8",
        )
        assert consent.consented_tool_args() is False

    def test_a_literal_true_consents(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": True}),
            encoding="utf-8",
        )
        assert consent.consented_tool_args() is True

    def test_an_unreadable_keystone_reads_as_not_consented(self, keystone):
        keystone.write_text("{not json", encoding="utf-8")
        assert consent.consented_tool_args() is False


# ── the writer's round trip ────────────────────────────────────────────────────


class TestTheRoundTrip:
    def test_enabling_without_mentioning_it_grants_nothing(self, keystone):
        state = consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        assert state["tool_args"] is False
        assert consent.consented_tool_args() is False

    def test_it_records_the_scope_it_was_given(self, keystone):
        state = consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        assert state["tool_args"] is True
        assert consent.consented_tool_args() is True
        # On the sealed record, not just in the returned dict.
        assert json.loads(keystone.read_text())["tool_args"] is True

    def test_the_keep_sentinel_preserves_a_recorded_scope(self, keystone):
        """An ordinary switch flip must not erase it by omission."""
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=consent.KEEP_TOOL_ARGS)
        assert consent.consented_tool_args() is True

    def test_the_keep_sentinel_does_not_invent_one(self, keystone):
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=consent.KEEP_TOOL_ARGS)
        assert consent.consented_tool_args() is False

    def test_an_explicit_false_revokes_it(self, keystone):
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=False)
        assert consent.consented_tool_args() is False

    def test_disabling_clears_it_so_a_re_enable_cannot_inherit_it(self, keystone):
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT)
        assert consent.consented_tool_args() is False
        # And the re-enable starts from the narrowest scope again.
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=consent.KEEP_TOOL_ARGS)
        assert consent.consented_tool_args() is False

    @pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {}])
    def test_a_non_boolean_scope_is_refused_not_coerced(self, keystone, bad):
        with pytest.raises(ValueError):
            consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=bad)


# ── the gate refuses the point, and only that point ───────────────────────────


class TestTheGateRefusesWithoutIt:
    @pytest.fixture(autouse=True)
    def _forget_warnings(self):
        """The once-per-point warning is process state; a test must not inherit it."""
        gate._unscoped_warned.clear()
        yield
        gate._unscoped_warned.clear()

    def test_the_annotating_point_is_refused_and_the_other_is_not(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        assert gate.is_enabled("skills.select", config=_config()) is True
        assert gate.is_enabled("tool.risk", config=_config()) is False

    def test_the_scope_admits_it(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": True}),
            encoding="utf-8",
        )
        assert gate.is_enabled("tool.risk", config=_config()) is True

    def test_the_refusal_is_said_out_loud_once(self, keystone, caplog):
        """An owner whose feature does nothing needs to be able to tell why."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        with caplog.at_level("WARNING"):
            for _ in range(4):
                gate.is_enabled("tool.risk", config=_config())
        said = [r for r in caplog.records if "tool-call arguments" in r.getMessage()]
        assert len(said) == 1, "once per point, not once per tool call"

    @pytest.mark.asyncio
    async def test_decide_sends_nothing_for_the_unscoped_point(self, keystone, monkeypatch):
        """The refusal is on ``decide`` too, not only on the cheap preflight.

        ``is_enabled`` grants nothing by contract -- ``decide`` re-runs every refusal
        -- so a scope enforced only in the preflight would be no scope at all.
        """
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
        answers = await gate.decide("tool.risk", {"tool": "bash"}, tr.questions(), config=_config())
        assert answers is None
        assert asked == []

    @pytest.mark.asyncio
    async def test_the_point_itself_annotates_nothing_without_the_scope(self, keystone):
        """End to end through the point, which is what the tool card calls."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        record = await tr.risk_record(
            tool="bash", arguments="rm -rf /data", policy="trust", session_key="chat-1"
        )
        assert record is None


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

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        resp = await api_decisions_consent_get(_request())
        assert json.loads(resp.text)["tool_args"] is True

    @pytest.mark.asyncio
    async def test_the_put_records_it(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": True})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["tool_args"] is True
        assert consent.consented_tool_args() is True

    @pytest.mark.asyncio
    async def test_an_omitted_field_preserves_the_recorded_scope(self, keystone, quiet_route):
        """The card writes ``enabled`` and ``endpoint`` alone on an ordinary flip."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["tool_args"] is True
        assert consent.consented_tool_args() is True

    @pytest.mark.asyncio
    async def test_an_explicit_false_revokes_it(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": False})
        )
        assert resp.status == 200
        assert json.loads(resp.text)["tool_args"] is False
        assert consent.consented_tool_args() is False

    @pytest.mark.asyncio
    async def test_disabling_through_the_route_clears_it(self, keystone, quiet_route):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
        resp = await api_decisions_consent_put(_request({"enabled": False}))
        assert resp.status == 200
        assert json.loads(resp.text)["tool_args"] is False
        assert consent.consented_tool_args() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["true", 1, 0, [], {}])
    async def test_a_truthy_stand_in_is_a_400_not_a_silent_yes(self, keystone, quiet_route, bad):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "tool_args": bad})
        )
        assert resp.status == 400
        assert consent.consented_tool_args() is False

    @pytest.mark.asyncio
    async def test_the_route_hands_the_keep_sentinel_down_rather_than_a_boolean(
        self, keystone, quiet_route, monkeypatch
    ):
        """Resolving the omitted scope here would restore one a concurrent PUT cleared.

        Same race the history ceiling documents: two owner PUTs overlap, one revoking
        the scope and one that only flips the switch. A route that read the scope
        itself would hold a value read BEFORE the revocation landed and write it back.
        The writer resolves it from the same read its write is based on, under the
        same lock, so the route must pass the sentinel through untouched.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, tool_args=True)
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
            seen.append(tool_args)
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

        assert seen == [consent.KEEP_TOOL_ARGS], "the sentinel, not a resolved boolean"
