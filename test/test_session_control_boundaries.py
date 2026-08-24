"""Session control: the channel-mirror boundary and the HTTP route layer.

Two surfaces the main suite did not reach.

The mirror checks exist because `linked_session_key` only marks a channel-BORN
slot. A dashboard-born slot given an OUTBOUND mirror link reaches a channel just
as surely, and the link lives in the session store rather than on the slot, so
the original checks read empty on exactly the session that republishes.

The route tests cover `handlers/session_control.py`, whose `_require_internal`
is the gate that stops a same-origin page with only a dashboard cookie from
messaging, stopping, or reading any session AS one of them.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _mirror(state, *keys: str) -> None:
    """Register an outbound mirror link for each of *keys*.

    Goes through the store's own setter so the test exercises the same shape
    production reads, rather than replacing the accessor.
    """
    for key in keys:
        state.sessions.set_mirror_link(key, "C123", "1777.0")


# -- the outbound-mirror boundary --------------------------------------------


class TestMirroredSessionsAreOutOfBounds:
    def _pair(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "caller")
        target = _slot(state, "peer")
        return state, caller, target

    def test_a_mirrored_target_is_refused(self, tmp_path):
        state, caller, target = self._pair(tmp_path)
        _mirror(state, slot_history_key(target))
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
        assert exc.value.code == "mirrored_target"

    def test_a_mirrored_caller_cannot_control_a_peer(self, tmp_path):
        state, caller, target = self._pair(tmp_path)
        _mirror(state, slot_history_key(caller))
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
        assert exc.value.code == "mirrored_caller"

    def test_an_unmirrored_pair_is_still_allowed(self, tmp_path):
        """The guard must not swallow the ordinary case."""
        state, caller, target = self._pair(tmp_path)
        _mirror(state)  # store answers None for every key
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
            is target
        )

    def test_an_unreadable_mirror_store_fails_closed(self, tmp_path):
        """A store that raises must not be read as 'not mirrored'."""
        state, caller, target = self._pair(tmp_path)

        def _boom(key: str):
            raise RuntimeError("store unavailable")

        state.sessions.get_mirror_link = _boom
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
        assert exc.value.code in {"mirrored_target", "mirrored_caller"}

    def test_a_store_without_the_accessor_is_not_treated_as_mirrored(self, tmp_path):
        """An older store lacking the method must not refuse everything.

        Absence of the feature is not evidence of a mirror, so it reads False --
        unlike a store that HAS the accessor and raises, which fails closed.
        """
        state, _caller, target = self._pair(tmp_path)
        state.sessions = object()  # no get_mirror_link at all
        assert sc._has_channel_mirror(state, target) is False


class TestCrewModeTargetsAreOutOfBounds:
    """A crew session's ingress is a durable queue entry, not a turn.

    `/api/chat` routes `mode == "crew"` to `state.crew.ingest` before anything
    else, which queues the message durably and fans it out to topic
    sub-sessions. Delivering it here as a turn would run generic work that is
    neither queued nor routed -- and would report success for it.
    """

    def _pair(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "caller")
        target = _slot(state, "peer")
        return state, caller, target

    def test_a_crew_target_is_refused(self, tmp_path):
        state, caller, target = self._pair(tmp_path)
        target.mode = "crew"
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
        assert exc.value.code == "crew_mode_target"

    def test_an_ordinary_target_is_unaffected(self, tmp_path):
        state, caller, target = self._pair(tmp_path)
        assert getattr(target, "mode", "") != "crew"
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
            is target
        )

    def test_a_crew_CALLER_may_still_control_a_peer(self, tmp_path):
        """The defect is in delivery semantics, not the caller's standing.

        A crew session is still the person's own; only its own INGRESS differs.
        Refusing it as a caller would narrow the surface for no stated reason.
        """
        state, caller, target = self._pair(tmp_path)
        caller.mode = "crew"
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=target.key,
                operation="read",
            )
            is target
        )


# -- cross-form target ambiguity ---------------------------------------------


class TestCrossFormAmbiguity:
    def test_a_title_matching_another_slots_key_is_refused(self, tmp_path):
        """Returning the key match would stop a session the caller never named.

        The caller reads titles off the screen. If one session's TITLE equals
        another's KEY, preferring the key silently addresses the wrong
        conversation -- and `session_stop` discards a live turn's work.
        """
        state = _make_state(tmp_path)
        _slot(state, "caller")
        keyed = _slot(state, "chat-donor")
        titled = _slot(state, "other")
        titled.title = keyed.key

        with pytest.raises(sc.SessionControlError) as exc:
            sc._resolve_slot(state, keyed.key)
        assert exc.value.code == "ambiguous_target"
        assert exc.value.status == 409

    def test_a_unique_key_still_resolves(self, tmp_path):
        state = _make_state(tmp_path)
        target = _slot(state, "peer")
        assert sc._resolve_slot(state, target.key) is target

    def test_a_unique_title_still_resolves(self, tmp_path):
        state = _make_state(tmp_path)
        target = _slot(state, "peer")
        target.title = "Release checklist"
        assert sc._resolve_slot(state, "release checklist") is target

    def test_two_slots_sharing_a_title_are_still_refused(self, tmp_path):
        state = _make_state(tmp_path)
        a = _slot(state, "a")
        b = _slot(state, "b")
        a.title = b.title = "Same"
        with pytest.raises(sc.SessionControlError) as exc:
            sc._resolve_slot(state, "Same")
        assert exc.value.code == "ambiguous_target"

    def test_one_slot_matching_two_forms_is_not_ambiguous(self, tmp_path):
        """A single slot answering by both key and title is one match, not two."""
        state = _make_state(tmp_path)
        target = _slot(state, "peer")
        target.title = target.key
        assert sc._resolve_slot(state, target.key) is target


# -- the HTTP route layer ----------------------------------------------------


def _request(path: str, *, internal: bool, body: object = None, app_state=None):
    """A request double carrying only what these handlers read."""
    req = MagicMock()
    req.path = path
    req.app = {"state": app_state}
    store = {"internal_auth": True} if internal else {}
    req.get = store.get
    req.headers = {}
    req.query = {}

    async def _json():
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    return req


class TestTheRoutesRequireTheInternalSecret:
    @pytest.mark.parametrize(
        "handler,path",
        [
            (handlers_sc.api_session_control_stop, "/api/session-control/stop"),
            (handlers_sc.api_session_control_read, "/api/session-control/read"),
        ],
    )
    def test_a_cookie_only_caller_is_refused(self, handler, path):
        """Strict paths are not self-enforcing: with the header absent the
        middleware falls through to cookie auth, so the handler re-asserts."""
        resp = asyncio.run(handler(_request(path, internal=False)))
        assert resp.status == 403


class TestRouteBodyValidation:
    def test_a_missing_target_is_400(self, tmp_path):
        state = _make_state(tmp_path)
        resp = asyncio.run(
            handlers_sc.api_session_control_stop(
                _request(
                    "/api/session-control/stop",
                    internal=True,
                    body={"force": True},
                    app_state=state,
                )
            )
        )
        assert resp.status == 400
        assert b"target_required" in resp.body

    def test_a_blank_target_is_rejected_like_a_missing_one(self, tmp_path):
        state = _make_state(tmp_path)
        resp = asyncio.run(
            handlers_sc.api_session_control_stop(
                _request(
                    "/api/session-control/stop",
                    internal=True,
                    body={"target": "   "},
                    app_state=state,
                )
            )
        )
        assert resp.status == 400
        assert b"target_required" in resp.body


class TestRefusalStatusMapping:
    """The renderer answers only from a closed set of statuses."""

    @pytest.mark.parametrize("status", [403, 404, 409, 429])
    def test_a_mapped_status_is_forwarded(self, status):
        exc = sc.SessionControlError("no", status=status, code="c")
        assert handlers_sc._refusal(exc).status == status

    def test_an_unmapped_status_degrades_to_400(self):
        """A status outside the set must not be forwarded verbatim."""
        exc = sc.SessionControlError("no", status=500, code="c")
        assert handlers_sc._refusal(exc).status == 400

    def test_the_code_is_always_present(self):
        exc = sc.SessionControlError("no", status=404, code="target_not_found")
        assert b"target_not_found" in handlers_sc._refusal(exc).body
