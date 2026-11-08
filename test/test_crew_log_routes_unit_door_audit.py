"""The agent's crew-log door: its transport, what each route answers, and the SEL audit.

A cookie caller is refused, so only the internal transport passes; reads and key
resolution go down the caller's own dispatch tree; the listing, resolve, page and
projection routes answer in their own shapes; every read leaves a SEL record under the
browser routes' action names; and the browser's session-keyed page still refuses the
internal caller. Shared fixtures and builders come from ``crew_log_routes_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from crew_log_routes_helpers import _fresh_dispatch_scan  # noqa: F401
from crew_log_routes_helpers import _is_owner  # noqa: F401
from crew_log_routes_helpers import _isolated_home  # noqa: F401
from crew_log_routes_helpers import (
    GATEWAY,
    OWNER_KEY,
    SESSION,
    _flag_on,
    _internal_request,
    _log,
    _opened,
    _Slot,
    _turn,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.dashboard.handlers import crew_log as routes


class TestOnlyTheInternalTransportReachesThese:
    """A cookie-authenticated caller is refused here, and told where its door is.

    Strict membership is NOT what refuses it. ``token_auth_middleware`` falls
    through to ordinary cookie auth when a LOOPBACK request carries no
    ``X-Internal-Secret``, on a strict path as much as a mixed one, and calls the
    handler once the cookie validates; strict decides only the NON-loopback caller,
    and ``local_only=False`` reclassifies strict as mixed anyway. So a same-machine
    tab reaches these handlers, and the refusal below is the only thing stopping it.
    """

    @pytest.mark.parametrize(
        "handler,path,match",
        [
            ("api_crew_log_sessions", "/api/crew-log/sessions", None),
            ("api_crew_log_resolve", f"/api/crew-log/resolve?key={OWNER_KEY}", None),
            (
                "api_crew_log_unit_page",
                f"/api/crew-log/units/{SESSION}/page",
                {"unit": SESSION},
            ),
            (
                "api_crew_log_unit_projection",
                f"/api/crew-log/units/{SESSION}/projection/status",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["sessions", "resolve", "page", "projection"],
    )
    def test_a_caller_with_no_internal_secret_is_refused(self, monkeypatch, handler, path, match):
        """All four, because one unguarded route is the whole hole."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(path, secret=False, match=match)
        response = asyncio.run(getattr(routes, handler)(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "/api/sessions/" in body["error"]

    def test_a_cookie_caller_reads_nothing_through_this_door(self, monkeypatch):
        """The refusal lands BEFORE any read, not after a second authorization test.

        This door and the browser's own pair keep separate authorization models, and
        a cookie admitted here would be a second path into the same bytes with a
        different gate in front of it. Pinned by asserting the shared read
        implementation is never entered, so the refusal cannot be satisfied by a
        handler that reads first and filters after.
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions", secret=False)
        with patch.object(routes, "_crew_log_read") as reader:
            response = asyncio.run(routes.api_crew_log_sessions(request))
        reader.assert_not_called()
        assert response.status == 403
        assert "/api/sessions/" in json.loads(response.text)["error"]

    def test_the_prefix_is_on_the_strict_transport(self):
        """Strict, not mixed: a forwarded browser is hard-denied rather than
        offered the cookie fall-through, because what is behind it is another live
        session's history. The browser's own pair must stay OUT of the prefix, or
        the panel could not load its own log at all."""
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        assert "/api/crew-log" in _STRICT_INTERNAL_API_PATHS
        assert "/api/crew-log" not in _MIXED_INTERNAL_API_PATHS
        assert "/api/sessions" not in _STRICT_INTERNAL_API_PATHS


class TestReadingAnotherSessionsUnit:
    """A DISPATCHED session's unit, by name. The capability the rule exists for.

    ``self`` is resolved server-side from the forwarded key, and naming another
    unit asks the gate the same question as naming your own, so these tests pin
    both forms against one rule. Every cross-unit read here is a read DOWN the
    caller's own dispatch tree; a read across to a stranger is pinned in
    ``TestTheDispatchFence``.
    """

    def test_a_subagent_may_read_its_own_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        _turn(handle, 1)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page",
            session_key="subagent:abc",
            slots={},
            sessions={"subagent:abc": SESSION},
            match={"unit": SESSION},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["entries"]

    def test_a_subagent_may_read_a_unit_it_dispatched(self, monkeypatch):
        """MUTATION-SENSITIVE: the rule, stated as a behaviour.

        A non-owner strict identity pages a unit that is NOT the one its own work
        lands in, and reaches it because that unit's ``session/opened`` names the
        caller's unit as its creator. This is the read a conductor needs to see what
        a session it dispatched actually did.
        """
        _flag_on(monkeypatch)
        child = _log("s-dispatched", slot="chat-dispatched")
        _opened(child, parent_slot="abc", parent_sid=SESSION)
        _turn(child, 1)
        request = _internal_request(
            "/api/crew-log/units/s-dispatched/page",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-dispatched"},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        body = json.loads(response.text)
        assert body["session_id"] == "s-dispatched"
        assert body["entries"]

    def test_a_subagent_may_fold_a_unit_it_dispatched(self, monkeypatch):
        """The fold door too, because one open route is the whole capability."""
        _flag_on(monkeypatch)
        child = _log("s-dispatched", slot="chat-dispatched")
        _opened(child, parent_slot="abc", parent_sid=SESSION)
        request = _internal_request(
            "/api/crew-log/units/s-dispatched/projection/status",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-dispatched", "name": "status"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 200
        assert json.loads(response.text)["name"] == "status"

    def test_a_subagent_may_resolve_the_key_of_a_session_it_dispatched(self, monkeypatch):
        """Resolving a dispatched session's key is the step BEFORE reading its unit.

        The route resolves the named key to a unit and scopes on THAT, so the answer
        is available exactly when the read that follows it would be. A key outside
        the caller's tree is refused here rather than one call later.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-dispatched", slot="chat-dispatched"), parent_slot="abc", parent_sid=SESSION)
        child_key = "dashboard:chat-child"
        request = _internal_request(
            f"/api/crew-log/resolve?key={child_key}",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION, child_key: "s-dispatched"},
        )
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 200
        assert json.loads(response.text) == {"key": child_key, "unit": "s-dispatched"}

    def test_resolving_a_stranger_key_is_refused(self, monkeypatch):
        """A resolve is a read, so it carries the same fence.

        Without this the route would answer which unit any session is landing in,
        which is the first half of reading it.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-stranger-unit"))
        stranger_key = "dashboard:chat-stranger"
        request = _internal_request(
            f"/api/crew-log/resolve?key={stranger_key}",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION, stranger_key: "s-stranger-unit"},
        )
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 403

    def test_a_dead_key_and_a_live_stranger_key_refuse_identically(self, monkeypatch):
        """MUTATION-SENSITIVE: the resolve route must not be a liveness oracle.

        The route resolves the key the caller named BEFORE authorizing, so the two
        refusals it can reach -- nothing resolved, and something resolved that is
        outside the caller's tree -- are reached by guessing a key. If their text
        differed, a dispatched agent could guess keys and learn which slots are
        live, which is an enumeration of the host's sessions rather than a read of
        one. The bodies are compared byte for byte, so any divergence fails.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-stranger-unit"))
        stranger_key = "dashboard:chat-stranger"
        live = _internal_request(
            f"/api/crew-log/resolve?key={stranger_key}",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION, stranger_key: "s-stranger-unit"},
        )
        dead = _internal_request(
            "/api/crew-log/resolve?key=dashboard:chat-does-not-exist",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
        )
        live_response = asyncio.run(routes.api_crew_log_resolve(live))
        dead_response = asyncio.run(routes.api_crew_log_resolve(dead))
        assert live_response.status == dead_response.status == 403
        assert json.loads(live_response.text) == json.loads(dead_response.text)

    def test_an_incognito_session_may_read_its_own_unit(self, monkeypatch):
        """Restriction is about what LEAVES the session, not about reading."""
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/status",
            slots={"chat-owner": _Slot(restricted=True)},
            match={"unit": SESSION, "name": "status"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 200
        assert json.loads(response.text)["name"] == "status"

    def test_an_unnamed_caller_reads_no_unit_at_all(self, monkeypatch):
        """Not even its own: "its own unit" is derived from the key that is missing."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page",
            session_key="",
            match={"unit": SESSION},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "forbidden"


class TestTheNewRoutes:
    def test_the_listing_returns_one_row_per_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        first = _log()
        _opened(first)
        _turn(first, 1)
        second = _log("s-second")
        _opened(second)
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        units = {row["unit"]: row for row in body["units"]}
        assert set(units) == {SESSION, "s-second"}
        assert units[SESSION]["slot"] == "dashboard:1"
        assert units[SESSION]["agent"] == "kirocrew"
        assert units[SESSION]["model"] == "opus"
        assert units[SESSION]["open"] is True
        assert units[SESSION]["last_seq"] >= 3
        assert body["scanned"] == 2
        assert body["truncated"] is False

    def test_type_counts_are_the_histogram_read_by_hand_today(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        _turn(handle, 1)
        _turn(handle, 2)
        request = _internal_request("/api/crew-log/sessions?with_type_counts=1")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        assert body["type_counts"]["turn/started"] == 2
        assert body["type_counts"]["turn/completed"] == 2
        assert body["units"][0]["type_counts"]["session/opened"] == 1

    def test_a_slot_filter_keeps_only_matching_units(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        other = _log("s-other-slot", slot="dashboard:99")
        other.append(
            "session/opened",
            {
                "agent": "kirocrew",
                "slot": "dashboard:99",
                "model": "opus",
                "cwd": "/w",
                "owner": "raymond",
                "resumed": False,
            },
            src=GATEWAY,
        )
        request = _internal_request("/api/crew-log/sessions?slot_contains=dashboard%3A99")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        assert [row["unit"] for row in body["units"]] == ["s-other-slot"]

    def test_an_unrecognized_query_does_not_change_what_is_listed(self, monkeypatch):
        """``kind`` is not part of this surface, so it is an unknown query.

        The listing holds sessions, because nothing writes a unit of another kind.
        A ``kind`` argument would therefore have one legal value equal to its own
        default, and refusing the illegal values would keep a decision alive on the
        surface that the storage layer does not offer. These routes ignore an
        unknown query parameter the way every other route does. What must NOT
        happen is a refusal, which reads to a caller as "this listing holds other
        kinds, and you named one wrong".
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions?kind=members")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        body = json.loads(response.text)
        assert body["kind"] == "session"
        assert [row["unit"] for row in body["units"]] == [SESSION]

    def test_resolve_answers_the_unit_a_key_is_landing_in(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request(f"/api/crew-log/resolve?key={OWNER_KEY}")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 200
        assert json.loads(response.text) == {"key": OWNER_KEY, "unit": SESSION}

    @pytest.mark.parametrize(
        ("query", "status"),
        [
            pytest.param(
                "?key=dashboard:chat-gone",
                404,
                id="test_resolve_refuses_a_key_with_no_live_acp_session",
            ),
            pytest.param("", 400, id="test_resolve_needs_a_key"),
        ],
    )
    def test_resolve_refuses_a_key_it_cannot_land(self, monkeypatch, query, status):
        _flag_on(monkeypatch)
        request = _internal_request(f"/api/crew-log/resolve{query}")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == status
        assert json.loads(response.text)["code"] == "unresolvable_key"

    def test_a_unit_with_no_log_is_unknown_rather_than_an_empty_page(self, monkeypatch):
        """``exists: false`` is the browser route's shape; an agent needs a code."""
        _flag_on(monkeypatch)
        request = _internal_request(
            "/api/crew-log/units/s-nothing/page", match={"unit": "s-nothing"}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 404
        assert json.loads(response.text)["code"] == "unknown_unit"

    def test_an_unknown_projection_is_named_as_such(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/nope",
            match={"unit": SESSION, "name": "nope"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "unknown_projection"

    def test_a_reversed_range_is_a_bad_range(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page?from=9&to=2", match={"unit": SESSION}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "bad_range"


class TestAudit:
    """Every read is SEL-audited under the SAME action names the browser routes use."""

    @pytest.mark.parametrize(
        "handler,operation,match",
        [
            ("api_crew_log_sessions", "session_crew_log.list", {}),
            ("api_crew_log_unit_page", "session_crew_log.read", {"unit": SESSION}),
            (
                "api_crew_log_unit_projection",
                "session_crew_log.projection",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["list", "read", "projection"],
    )
    def test_a_granted_read_is_audited_under_its_action(
        self, monkeypatch, handler, operation, match
    ):
        _flag_on(monkeypatch)
        _opened(_log())
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        path = "/api/crew-log/x"
        request = _internal_request(path, match=match)
        with patch("kiro_crew.sel.sel", return_value=sel):
            asyncio.run(getattr(routes, handler)(request))
        rows = [row for row in recorded if row["operation"] == operation]
        assert rows, recorded
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert rows[0]["source"] == "mcp"
        assert rows[0]["outcome"] == "granted"

    def test_a_denied_read_is_audited_with_its_reason(self, monkeypatch):
        """MUTATION-SENSITIVE: an unnamed caller is refused AND leaves the record.

        A ``cron:`` key is admitted now, so the denial pinned here is the one the
        identity gate still makes: a request carrying no session key at all.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", session_key="")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows and rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert "session identity" in rows[0]["error"]

    def test_a_dispatched_unit_read_is_audited_as_granted(self, monkeypatch):
        """MUTATION-SENSITIVE: the widened read is the one an operator must see.

        The decision makes a non-owner read of a unit it dispatched ordinary, so the
        audit row for it is the whole of what replaces the refusal. It names the
        operation, the calling component and the granted outcome.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-dispatched", slot="chat-dispatched"), parent_slot="abc", parent_sid=SESSION)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request(
            "/api/crew-log/units/s-dispatched/page",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-dispatched"},
        )
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        rows = [row for row in recorded if row["operation"] == "session_crew_log.read"]
        assert rows, recorded
        assert rows[0]["outcome"] == "granted"
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert rows[0]["source"] == "mcp"

    def test_a_read_outside_the_dispatch_tree_is_audited_as_denied(self, monkeypatch):
        """MUTATION-SENSITIVE: the refusal side of the same door.

        A fence is only auditable if the refusal it produces reaches the record, and
        this is the refusal an operator most wants: one session reaching for
        another's history.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-stranger-unit"))
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request(
            "/api/crew-log/units/s-stranger-unit/page",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-stranger-unit"},
        )
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.read"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert "does not fall inside that scope" in rows[0]["error"]

    def test_a_secretless_caller_is_audited_as_the_dashboard(self, monkeypatch):
        """The denial an operator most wants: something reached an MCP-only route
        with a browser-shaped credential. It is refused BEFORE the caller identity
        is settled, so the record has to come from ``request_origin``'s no-secret
        answer -- ``("dashboard", "dashboard")`` -- rather than from a claimed
        component name, which an unauthenticated caller could set to anything.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", secret=False)
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "dashboard"
        assert rows[0]["source"] == "dashboard"
        assert "/api/sessions/" in rows[0]["error"]

    def test_a_request_naming_another_component_is_audited(self, monkeypatch):
        """The other boundary-crossing shape: an authenticated internal caller that
        is not this server. It audits under the name ``request_origin`` resolved,
        which is a KNOWN component or the clamped ``unknown-internal`` -- never the
        raw header value.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", caller="kirocrew-dashboard")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "kirocrew-dashboard"
        assert rows[0]["source"] == "mcp"
        assert routes.CREW_LOG_MCP_CALLER in rows[0]["error"]

    def test_an_unrecognized_component_is_audited_as_unknown_internal(self, monkeypatch):
        """A name no release ships is clamped, so the audit log cannot be seeded
        with an arbitrary string by whoever set the header."""
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", caller="not-a-real-server")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows and rows[0]["caller"] == "unknown-internal"

    @pytest.mark.parametrize(
        "handler,operation,match",
        [
            ("api_crew_log_sessions", "session_crew_log.list", {}),
            ("api_crew_log_resolve", "session_crew_log.resolve", {}),
            ("api_crew_log_unit_page", "session_crew_log.read", {"unit": SESSION}),
            (
                "api_crew_log_unit_projection",
                "session_crew_log.projection",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["list", "resolve", "read", "projection"],
    )
    def test_no_route_refuses_without_leaving_a_record(
        self, monkeypatch, handler, operation, match
    ):
        """One unaudited denial path is the whole gap, so all four are pinned."""
        _flag_on(monkeypatch)
        _opened(_log())
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/x", secret=False, match=match)
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(getattr(routes, handler)(request))
        assert response.status == 403
        assert [row for row in recorded if row["outcome"] == "denied"], recorded

    def test_a_failing_audit_never_changes_the_outcome(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions")
        with patch("kiro_crew.sel.sel", side_effect=RuntimeError("sel is down")):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200

    def test_the_proxys_own_call_lands_in_the_callers_crew_log(self, monkeypatch):
        """The read is recorded twice on purpose: SEL for the operator, and a
        ``tool/called`` entry in the calling session's OWN log for the record the
        session itself carries. The second is automatic; this pins that it happens.
        """
        monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
        from kiro_crew.crew_log import emit

        handle = _log()
        _opened(handle)
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
        emit.on_tool_called(
            SESSION,
            1,
            name="crew_log_read",
            server="kirocrew-crew-log",
            kind="read",
            call_id="c-1",
            args='{"unit": "self"}',
        )
        emit.drain_for_shutdown()
        types = [entry.type for entry in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)]
        assert "tool/called" in types
        called = [
            entry
            for entry in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)
            if entry.type == "tool/called"
        ]
        assert called[0].data["server"] == "kirocrew-crew-log"
        assert called[0].data["name"] == "crew_log_read"


class TestTheBrowserRoutesAreUnchanged:
    def test_the_browser_page_route_still_refuses_the_internal_caller(self, monkeypatch):
        """The session-keyed pair is cookie-only: the agent's door is the unit-keyed
        prefix, and admitting the internal caller here would widen two routes."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(f"/api/sessions/{SESSION}/crew-log", match={"id": SESSION})
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            response = asyncio.run(routes.api_session_crew_log(request))
        assert response.status == 403
