"""Tests for the blocking ask_question round-trip.

Covers the three states the agent can observe (answered / timed out /
dismissed), the redaction pass on the broadcast payload, the slot-scoped
cancel, and the two HTTP handlers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState

# ── Helpers ──


def _state() -> DashboardState:
    """A DashboardState with WS broadcast captured instead of sent.

    The two channels are captured separately on purpose: question payloads must
    go to the OWNER channel only, so a test can assert the all-clients channel
    stays empty. ``broadcasts`` is the owner channel because that is where every
    question event belongs.
    """
    st = DashboardState.__new__(DashboardState)
    st._pending_questions = {}
    st._question_futures = {}
    st.broadcasts: list[tuple[str, dict]] = []  # type: ignore[attr-defined]
    st.broadcasts_all: list[tuple[str, dict]] = []  # type: ignore[attr-defined]
    st.broadcast_ws_owners = lambda kind, payload: st.broadcasts.append(  # type: ignore[assignment,attr-defined]
        (kind, payload)
    )
    st.broadcast_ws = lambda kind, payload: st.broadcasts_all.append(  # type: ignore[assignment,attr-defined]
        (kind, payload)
    )
    st._log = MagicMock()
    return st


def _questions(text: str = "Which approach?") -> list[dict]:
    return [
        {
            "question": text,
            "header": "SCOPE",
            "options": [
                {"label": "Option A", "description": "the safe one"},
                {"label": "Option B", "description": ""},
            ],
            "multiSelect": False,
        }
    ]


# ── request_question / resolve_question ──


@pytest.mark.asyncio
async def test_answered_question_returns_answer_map() -> None:
    st = _state()

    async def answer_soon() -> None:
        # Yield until request_question has registered its future.
        for _ in range(50):
            if "a1" in st._question_futures:
                break
            await asyncio.sleep(0)
        assert st.resolve_question("a1", {"Which approach?": "Option B"})

    _, result = await asyncio.gather(
        answer_soon(),
        st.request_question("a1", "chat-1", _questions(), timeout=5),
    )
    assert result == {"Which approach?": "Option B"}
    # Registries are cleaned up so a late duplicate answer cannot land.
    assert st._pending_questions == {}
    assert st._question_futures == {}


@pytest.mark.asyncio
async def test_broadcasts_question_card_then_resolved() -> None:
    st = _state()

    async def answer_soon() -> None:
        for _ in range(50):
            if "a2" in st._question_futures:
                break
            await asyncio.sleep(0)
        st.resolve_question("a2", {"Which approach?": "Option A"})

    await asyncio.gather(
        answer_soon(),
        st.request_question("a2", "chat-7", _questions(), timeout=5),
    )
    kinds = [k for k, _ in st.broadcasts]  # type: ignore[attr-defined]
    assert kinds == ["question_card", "question_card_resolved"]
    card = st.broadcasts[0][1]  # type: ignore[attr-defined]
    assert card["ask_id"] == "a2"
    assert card["slot"] == "chat-7"
    # The resolved event carries the id so a stale one cannot clear a newer card.
    assert st.broadcasts[1][1] == {"ask_id": "a2"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_timeout_returns_none_and_clears_card() -> None:
    st = _state()
    result = await st.request_question("a3", "chat-1", _questions(), timeout=1)
    assert result is None
    assert st._pending_questions == {}
    # The card must be retracted, otherwise it stays clickable and 404s.
    assert ("question_card_resolved", {"ask_id": "a3"}) in st.broadcasts  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_dismissal_is_indistinguishable_from_timeout() -> None:
    st = _state()

    async def dismiss_soon() -> None:
        for _ in range(50):
            if "a4" in st._question_futures:
                break
            await asyncio.sleep(0)
        st.resolve_question("a4", None)

    _, result = await asyncio.gather(
        dismiss_soon(),
        st.request_question("a4", "chat-1", _questions(), timeout=5),
    )
    assert result is None


@pytest.mark.asyncio
async def test_timeout_is_clamped_to_max() -> None:
    st = _state()
    seen: dict[str, float] = {}

    async def fake_wait_for(fut, timeout):  # type: ignore[no-untyped-def]
        seen["timeout"] = timeout
        raise asyncio.TimeoutError

    with patch("asyncio.wait_for", fake_wait_for):
        await st.request_question("a5", "chat-1", _questions(), timeout=999_999)
    assert seen["timeout"] == DashboardState._QUESTION_TIMEOUT_MAX


@pytest.mark.asyncio
async def test_question_text_is_redacted_before_broadcast() -> None:
    st = _state()
    # The stub redactors below key off opaque sentinels rather than a hostname
    # substring. Matching on a hostname fragment (`"evil.example.com" in s`) is
    # the incomplete-URL-substring-sanitization anti-pattern — CodeQL flags it
    # even in a test double, and rightly so: it is the exact shape of a real
    # sanitizer bug. What this test actually asserts is that request_question
    # routes every text field through the redactors, so the stubs need no URL
    # parsing at all.
    leaky = _questions("Post to LEAKY_URL_SENTINEL please")
    leaky[0]["options"][0]["label"] = "LEAKY_CRED_SENTINEL"

    with patch(
        "kiro_crew.dashboard.state.redact_exfiltration_urls",
        side_effect=lambda s: (
            ("<url-redacted>", 1) if "LEAKY_URL_SENTINEL" in s else (s, 0)
        ),
    ), patch(
        "kiro_crew.dashboard.state.redact_credentials",
        side_effect=lambda s: (
            ("<cred-redacted>", 1) if "LEAKY_CRED_SENTINEL" in s else (s, 0)
        ),
    ):
        await st.request_question("a6", "chat-1", leaky, timeout=1)

    card = st.broadcasts[0][1]  # type: ignore[attr-defined]
    assert card["questions"][0]["question"] == "<url-redacted>"
    assert card["questions"][0]["options"][0]["label"] == "<cred-redacted>"
    # The caller's list must not be mutated in place.
    assert "LEAKY_URL_SENTINEL" in leaky[0]["question"]


def test_resolve_unknown_question_returns_false() -> None:
    st = _state()
    assert st.resolve_question("nope", {"q": "a"}) is False


@pytest.mark.asyncio
async def test_cancel_questions_for_slot_only_targets_that_slot() -> None:
    st = _state()
    task_a = asyncio.ensure_future(
        st.request_question("mine", "chat-1", _questions(), timeout=30)
    )
    task_b = asyncio.ensure_future(
        st.request_question("other", "chat-2", _questions(), timeout=30)
    )
    for _ in range(50):
        if len(st._question_futures) == 2:
            break
        await asyncio.sleep(0)

    assert st.cancel_questions_for_slot("chat-1") == 1
    assert await task_a is None
    assert not task_b.done()

    st.resolve_question("other", {"Which approach?": "Option A"})
    assert await task_b == {"Which approach?": "Option A"}


# ── HTTP handlers ──


def _as_owner(request: MagicMock, user: str = "local-app") -> MagicMock:
    """Give a fake request the dashboard-owner identity.

    ``is_owner_dashboard_request`` needs BOTH an explicit empty app claim and a
    dashboard-user subject. With no ``owner_id`` configured on the state
    fixture, the signed local bootstrap subject ``local-app`` IS the owner --
    the same identity the ``ask_question`` MCP tool carries, since its token is
    minted as ``generate_token(owner_id or "local-app")``.

    It reads the claim three ways (``in``, ``[]`` and ``.get``), so all three are
    wired to one dict rather than left as default MagicMock attributes -- a bare
    ``request["app"]`` returns a MagicMock, which is not ``""`` and silently
    fails the gate.
    """
    claims = {"app": "", "user": user}
    request.__contains__.side_effect = lambda k: k in claims
    request.__getitem__.side_effect = lambda k: claims[k]
    request.get = lambda k, d="": claims.get(k, d)
    return request


@pytest.mark.asyncio
async def test_handler_rejects_unknown_slot() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question

    st = _state()
    st._slots = {}
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)

    async def _json() -> dict:
        return {"session_key": "dashboard:chat-9", "questions": _questions()}

    request.json = _json
    resp = await api_ask_question(request)
    # 404 rather than blocking for the full window on a card nobody renders.
    assert resp.status == 404


@pytest.mark.asyncio
async def test_handler_rejects_invalid_question_payload() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question

    st = _state()
    st._slots = {"chat-1": MagicMock()}
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)

    async def _json() -> dict:
        return {"session_key": "dashboard:chat-1", "questions": []}

    request.json = _json
    resp = await api_ask_question(request)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_answer_handler_resolves_pending_question() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("h1", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if "h1" in st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "h1"}

    async def _json() -> dict:
        return {"answers": {"Which approach?": "Option B"}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 200
    assert await task == {"Which approach?": "Option B"}


@pytest.mark.asyncio
async def test_answer_handler_404s_on_expired_question() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "gone"}

    async def _json() -> dict:
        return {"answers": {"q": "a"}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_answer_handler_coerces_nested_values_to_str() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("h2", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if "h2" in st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "h2"}

    async def _json() -> dict:
        return {"answers": {"q": {"nested": ["structure"]}}}

    request.json = _json
    await api_ask_question_answer(request)
    answers = await task
    assert answers is not None
    # Structure is flattened so it cannot smuggle shape into the transcript.
    assert isinstance(answers["q"], str)


@pytest.mark.asyncio
async def test_oversized_answer_is_rejected_not_truncated() -> None:
    """Truncating would resolve the wait on input the user cannot see was cut.

    Answers are echoed into the model context, so they are bounded — but slicing
    silently clears the card and lets the agent proceed on a mangled answer with
    no way to resend. A 400 leaves the card up (the frontend clears only on
    success or 404) so the user can shorten and retry.
    """
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer
    from kiro_crew.validation import _ASK_MAX_ANSWER_LEN

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("cap1", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if "cap1" in st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "cap1"}

    async def _json() -> dict:
        return {"answers": {"q": "x" * (_ASK_MAX_ANSWER_LEN + 1)}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 400
    assert str(_ASK_MAX_ANSWER_LEN) in json.loads(resp.text)["error"]
    # Critically, the wait is still open: the answer was not accepted, so the
    # user can retry rather than the agent resuming on truncated input.
    assert not task.done()

    assert st.resolve_question("cap1", None)
    assert await task is None


@pytest.mark.asyncio
async def test_answer_at_the_limit_is_accepted() -> None:
    """The boundary itself must still work, or the cap is off by one."""
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer
    from kiro_crew.validation import _ASK_MAX_ANSWER_LEN

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("cap2", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if "cap2" in st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "cap2"}

    async def _json() -> dict:
        return {"answers": {"q": "x" * _ASK_MAX_ANSWER_LEN}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 200
    answers = await task
    assert answers is not None and len(answers["q"]) == _ASK_MAX_ANSWER_LEN


@pytest.mark.asyncio
async def test_too_many_answer_entries_rejected() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer
    from kiro_crew.validation import _ASK_MAX_QUESTIONS

    st = _state()
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "cap2"}

    async def _json() -> dict:
        return {"answers": {f"q{i}": "a" for i in range(_ASK_MAX_QUESTIONS + 1)}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_dismissed_body_unblocks_with_no_answer() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("h3", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if "h3" in st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    request.match_info = {"ask_id": "h3"}

    async def _json() -> dict:
        return {"dismissed": True}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 200
    assert await task is None


# ── Route registration ──


def test_ask_question_routes_are_registered() -> None:
    """Guards the wiring itself: a handler nobody can reach is a silent no-op."""
    from aiohttp import web

    from kiro_crew.dashboard.server import _register_mcp_routes

    app = web.Application()
    _register_mcp_routes(app)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
    assert ("POST", "/api/ask-question") in routes
    assert ("POST", "/api/ask-question/{ask_id}/answer") in routes


# ── Authorization: app tokens are refused (GPT HIGH, round 3) ──


@pytest.mark.asyncio
async def test_app_token_cannot_ask_a_question() -> None:
    """An app token must not be able to post a card into any slot.

    `_enforce_app_scope` only checks the route is in the app's manifest
    allowlist, not slot ownership — so without this gate an app listing
    /api/ask-question could target the owner's slot, broadcast a crafted card,
    and read the typed answer out of its own blocked response.
    """
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question

    st = _state()
    st._slots = {"chat-1": MagicMock()}
    request = MagicMock()
    request.app = {"state": st}
    request.__contains__.return_value = True
    request.get = lambda k, d="": "evil-app" if k == "app" else d

    async def _json() -> dict:
        return {"session_key": "dashboard:chat-1", "questions": _questions()}

    request.json = _json
    resp = await api_ask_question(request)
    assert resp.status == 403
    # Specifically the app gate, not the owner gate that follows it: otherwise
    # this test would keep passing if the app denial were deleted.
    assert "app token" in json.loads(resp.text)["error"]
    # And it must not have broadcast anything.
    assert st.broadcasts == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_app_token_cannot_answer_a_question() -> None:
    """The answer endpoint resolves by ask_id alone, so it needs the same gate."""
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("authz1", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if "authz1" in st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    request.match_info = {"ask_id": "authz1"}
    request.__contains__.return_value = True
    request.get = lambda k, d="": "evil-app" if k == "app" else d

    async def _json() -> dict:
        return {"answers": {"Which approach?": "Option A"}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 403
    # The question must still be pending — the app cannot resolve it.
    assert not task.done()
    st.resolve_question("authz1", None)
    await task


@pytest.mark.asyncio
async def test_dashboard_user_token_is_still_allowed() -> None:
    """The gate must not lock out the legitimate caller (empty app)."""
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question

    st = _state()
    st._slots = {"chat-1": MagicMock()}
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)

    async def _json() -> dict:
        return {
            "session_key": "dashboard:chat-1",
            "questions": _questions(),
            "timeout_secs": 15,
        }

    request.json = _json
    with patch("kiro_crew.dashboard.handlers.ask_question.sel"):
        resp = await api_ask_question(request)
    # Times out (nobody answers) rather than being refused.
    assert resp.status == 200


# ── Body shape: valid JSON that is not an object (GPT MEDIUM, round 3) ──


@pytest.mark.asyncio
async def test_non_object_body_is_400_not_500() -> None:
    """`[]` / `null` / scalars parse fine then blow up on .get() as a 500."""
    from kiro_crew.dashboard.handlers.ask_question import (
        api_ask_question,
        api_ask_question_answer,
    )

    for payload in ([], None, "str", 7):
        st = _state()
        st._slots = {"chat-1": MagicMock()}

        ask = MagicMock()
        ask.app = {"state": st}
        _as_owner(ask)

        async def _json(p=payload):
            return p

        ask.json = _json
        resp = await api_ask_question(ask)
        assert resp.status == 400, f"ask: {payload!r}"

        ans = MagicMock()
        ans.app = {"state": st}
        ans.match_info = {"ask_id": "x"}
        _as_owner(ans)
        ans.json = _json
        resp = await api_ask_question_answer(ans)
        assert resp.status == 400, f"answer: {payload!r}"


# ── cancel_questions_for_slot is actually wired (Arbiter BLOCK item 1) ──


def test_unblock_pending_waits_releases_both_waits() -> None:
    """The shared chokepoint must release approvals AND questions.

    `cancel_questions_for_slot` previously had no production caller while
    agent-questions.md documented it as a guarantee — a documented safety
    property with no call site is worse than no property.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.dashboard.chat_handlers import _unblock_pending_waits

    state = MagicMock()
    state.cancel_questions_for_slot.return_value = 2
    slot = MagicMock()
    slot.key = "chat-1"

    with patch(
        "kiro_crew.dashboard.chat_handlers._reject_pending_approvals"
    ) as rejected:
        _unblock_pending_waits(state, slot)

    rejected.assert_called_once_with(slot)
    state.cancel_questions_for_slot.assert_called_once_with("chat-1")


def test_every_stop_path_uses_the_combined_chokepoint() -> None:
    """No stop path may call the approval half alone.

    Asserted on source because the alternative — three separate integration
    tests through the stop handlers — would still not catch a FOURTH path added
    later, which is precisely how this defect arose.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "src/kiro_crew/dashboard/chat_handlers.py"
    ).read_text(encoding="utf-8")

    # The only permitted _reject_pending_approvals reference outside its own
    # definition is the one inside _unblock_pending_waits.
    body = src.split("def _unblock_pending_waits", 1)
    assert len(body) == 2, "the combined chokepoint helper is gone"
    before, after = body
    # Its definition and docstring reference are fine; count real call sites in
    # the rest of the module (after the helper).
    stray = [
        ln
        for ln in after.splitlines()
        if "_reject_pending_approvals(slot)" in ln
    ]
    assert len(stray) == 1, (
        "every stop/interrupt/delete path must call _unblock_pending_waits, not "
        f"_reject_pending_approvals directly; stray call sites: {stray}"
    )
    assert after.count("_unblock_pending_waits(state, slot)") >= 4, (
        "expected the force-stop, soft-stop, interrupt and slot-delete paths to "
        "use the combined chokepoint"
    )


# ── Authorization: owner-only, not merely "not an app" (GPT HIGH, round 4) ──


@pytest.mark.asyncio
async def test_non_owner_dashboard_token_cannot_ask() -> None:
    """A non-owner dashboard session must not be able to address a card.

    Every allowed Slack user can mint a dashboard token (`!dashboard`), and that
    token carries an EMPTY app claim -- so it clears the app-token gate while
    belonging to someone who is not the owner. Such a caller could target any
    slot, phish the owner with crafted options, and read the typed answer out of
    its own blocked response.
    """
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question

    st = _state()
    st.owner_id = "U_OWNER"
    st._slots = {"chat-1": MagicMock()}
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request, user="U_SOMEONE_ELSE")

    async def _json() -> dict:
        return {"session_key": "dashboard:chat-1", "questions": _questions()}

    request.json = _json
    resp = await api_ask_question(request)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_non_owner_dashboard_token_cannot_answer() -> None:
    """Nor resolve a card the owner is still looking at.

    Otherwise a non-owner feeds the blocked agent an answer the owner never gave.
    """
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    st.owner_id = "U_OWNER"
    task = asyncio.ensure_future(
        st.request_question("ask-1", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    request.match_info = {"ask_id": "ask-1"}
    _as_owner(request, user="U_SOMEONE_ELSE")

    async def _json() -> dict:
        return {"answers": {"Which approach?": "Option A"}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 403
    # The owner's card is untouched: still pending, still blocking.
    assert not task.done()

    assert st.resolve_question("ask-1", None)
    assert await task is None


@pytest.mark.asyncio
async def test_configured_owner_is_allowed() -> None:
    """The gate must not lock out the legitimate owner."""
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_answer

    st = _state()
    st.owner_id = "U_OWNER"
    task = asyncio.ensure_future(
        st.request_question("ask-2", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    request.match_info = {"ask_id": "ask-2"}
    _as_owner(request, user="U_OWNER")

    async def _json() -> dict:
        return {"answers": {"Which approach?": "Option A"}}

    request.json = _json
    resp = await api_ask_question_answer(request)
    assert resp.status == 200
    assert await task == {"Which approach?": "Option A"}


# ── Reconnect rehydration (GPT MEDIUM, round 4) ──


@pytest.mark.asyncio
async def test_pending_endpoint_lists_unanswered_cards() -> None:
    """`question_card` is one-shot, so a reload needs a rehydration source.

    Without this the agent stays blocked with nothing on screen until its window
    elapses.
    """
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_pending

    st = _state()
    task = asyncio.ensure_future(
        st.request_question("ask-3", "chat-7", _questions(), timeout=30)
    )
    for _ in range(50):
        if st._question_futures:
            break
        await asyncio.sleep(0)

    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)
    resp = await api_ask_question_pending(request)
    assert resp.status == 200
    rows = json.loads(resp.text)
    assert [(r["ask_id"], r["slot"]) for r in rows] == [("ask-3", "chat-7")]
    assert rows[0]["questions"][0]["question"] == "Which approach?"

    st.resolve_question("ask-3", None)
    assert await task is None

    # Once resolved it must disappear, or a reload resurrects a dead card.
    resp = await api_ask_question_pending(request)
    assert json.loads(resp.text) == []


@pytest.mark.asyncio
async def test_pending_endpoint_is_owner_only() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_pending

    st = _state()
    st.owner_id = "U_OWNER"
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request, user="U_SOMEONE_ELSE")
    resp = await api_ask_question_pending(request)
    assert resp.status == 403


# ── Session resets release the blocking wait (GPT MEDIUM, round 4) ──


def test_every_session_reset_goes_through_the_chokepoint() -> None:
    """Switch handlers reset the session, which tears down the agent.

    A pending question lives in dashboard state rather than in the session, so
    it survives the reset: the card stays on screen and the blocked request holds
    an MCP worker with no agent left to receive the answer. Asserted on source
    because the alternative -- an integration test per switch handler -- still
    would not catch a SIXTH handler added later, which is exactly how the stop
    paths drifted before.
    """
    src = (
        Path(__file__).resolve().parents[1]
        / "src/kiro_crew/dashboard/chat_handlers.py"
    ).read_text(encoding="utf-8")

    body = src.split("async def _reset_slot_session", 1)
    assert len(body) == 2, "the reset chokepoint is gone"
    # Exactly one raw `sessions.reset` may remain: the one inside the helper.
    assert body[1].count("await state.sessions.reset(") == 1, (
        "a switch handler resets the session directly, so a pending "
        "ask_question would outlive the agent it was waiting on"
    )
    assert body[1].count("await _reset_slot_session(") >= 5, (
        "expected the agent, model, bulk-model, reasoning-effort and workspace "
        "switches to reset through the chokepoint"
    )


@pytest.mark.asyncio
async def test_reset_chokepoint_cancels_pending_questions() -> None:
    """The helper itself must actually release the wait, not just exist."""
    from kiro_crew.dashboard.chat_handlers import _reset_slot_session

    st = _state()
    st.sessions = MagicMock()

    async def _reset(_key: str) -> None:
        return None

    st.sessions.reset = _reset
    task = asyncio.ensure_future(
        st.request_question("ask-4", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if st._question_futures:
            break
        await asyncio.sleep(0)

    slot = MagicMock()
    slot.key = "chat-1"
    with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
        await _reset_slot_session(st, slot, "dashboard:chat-1")

    # Unblocked with no answer rather than left hanging until timeout.
    assert await task is None


# ── Owner-scoped broadcast (GPT HIGH, round 5) ──


@pytest.mark.asyncio
async def test_question_events_go_only_to_owner_sockets() -> None:
    """The card must not fan out to non-owner dashboard sockets.

    Owner-gating the HTTP endpoints buys nothing if the payload still reaches
    every socket: an allowed Slack user's `!dashboard` session registers as an
    ordinary WS client, so a plain broadcast would hand them the owner's question
    text, options, and ask_id.
    """
    st = _state()
    task = asyncio.ensure_future(
        st.request_question("ask-own", "chat-1", _questions(), timeout=30)
    )
    for _ in range(50):
        if st._question_futures:
            break
        await asyncio.sleep(0)

    assert [k for k, _ in st.broadcasts] == ["question_card"]
    # The all-clients channel must stay untouched for both events.
    assert st.broadcasts_all == []  # type: ignore[attr-defined]

    st.resolve_question("ask-own", None)
    assert await task is None
    assert [k for k, _ in st.broadcasts] == ["question_card", "question_card_resolved"]
    assert st.broadcasts_all == []  # type: ignore[attr-defined]


def test_broadcast_ws_owners_targets_the_owner_client_set() -> None:
    """The helper itself must send to _owner_ws_clients, not _ws_clients."""
    st = DashboardState.__new__(DashboardState)
    owner_ws = MagicMock()
    st._owner_ws_clients = {owner_ws}
    st._ws_clients = {owner_ws, MagicMock()}
    sent: list[str] = []
    st._send_ws_owners = lambda msg: sent.append(msg)  # type: ignore[assignment]
    st._send_ws_all = lambda msg: pytest.fail(  # type: ignore[assignment]
        "question payloads must never use the all-clients channel"
    )

    st.broadcast_ws_owners("question_card", {"ask_id": "x"})
    assert len(sent) == 1
    assert json.loads(sent[0]) == {"type": "question_card", "data": {"ask_id": "x"}}


# ── Round 7: watchdog-bounded window + post-redaction collision ──


def test_question_window_stays_under_the_tool_stall_watchdog() -> None:
    """The wait must end before ACP declares the turn dead.

    `acp/client.py::_TOOL_STALL_TIMEOUT` is armed once a tool call is dispatched,
    and a blocked ask_question emits no progress frames — so a window at or beyond
    that value lets the watchdog kill the turn, after which an answer has no turn
    left to return to. The ceiling was copied from the `wait` tool (1800s), which
    is a different mechanism; that was the bug.
    """
    from kiro_crew.acp.client import _TOOL_STALL_TIMEOUT
    from kiro_crew.validation import ASK_QUESTION_SCHEMA

    assert DashboardState._QUESTION_TIMEOUT_MAX < _TOOL_STALL_TIMEOUT
    assert DashboardState._QUESTION_TIMEOUT_DEFAULT <= DashboardState._QUESTION_TIMEOUT_MAX
    # The agent-facing schema must not advertise more than the server will honour,
    # or a caller asks for 1800s, gets silently clamped, and the turn dies anyway.
    spec = {f.name: f for f in ASK_QUESTION_SCHEMA.fields}["timeout_secs"]
    assert spec.max_val is not None
    assert spec.max_val <= DashboardState._QUESTION_TIMEOUT_MAX
    assert spec.max_val < _TOOL_STALL_TIMEOUT


@pytest.mark.asyncio
async def test_questions_identical_after_redaction_are_rejected() -> None:
    """Redaction is lossy, and the answer map is keyed by the REDACTED text.

    Two questions differing only inside a credential pass the pre-redaction
    duplicate check, then collapse to one key here — so one answer would
    overwrite the other and the agent would resume on incomplete input.
    """
    st = _state()
    colliding = _questions("Use LEAKY_CRED_SENTINEL now?") + [
        {
            "question": "Use LEAKY_CRED_SENTINEL2 now?",
            "header": "SCOPE",
            "options": [{"label": "Yes", "description": ""}],
        }
    ]

    with patch(
        "kiro_crew.dashboard.state.redact_credentials",
        side_effect=lambda s: (
            ("Use <cred> now?", 1) if "LEAKY_CRED_SENTINEL" in s else (s, 0)
        ),
    ), patch(
        "kiro_crew.dashboard.state.redact_exfiltration_urls",
        side_effect=lambda s: (s, 0),
    ):
        with pytest.raises(ValueError, match="after redaction"):
            await st.request_question("ask-collide", "chat-1", colliding, timeout=30)

    # And it must not leave an orphan future behind: nothing would ever resolve it.
    assert st._question_futures == {}
    assert st._pending_questions == {}
    assert st.broadcasts == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_option_labels_identical_after_redaction_are_rejected() -> None:
    """Distinct validated labels must remain distinct after lossy redaction."""
    st = _state()
    colliding = _questions()
    colliding[0]["options"] = [
        {"label": "Deploy LEAKY_CRED_SENTINEL", "description": "staging"},
        {"label": "Deploy LEAKY_CRED_SENTINEL2", "description": "production"},
    ]

    with patch(
        "kiro_crew.dashboard.state.redact_credentials",
        side_effect=lambda s: (
            ("Deploy <cred>", 1) if "LEAKY_CRED_SENTINEL" in s else (s, 0)
        ),
    ), patch(
        "kiro_crew.dashboard.state.redact_exfiltration_urls",
        side_effect=lambda s: (s, 0),
    ):
        with pytest.raises(ValueError, match="option labels.*after redaction"):
            await st.request_question("ask-option-collide", "chat-1", colliding, timeout=30)

    # Reject before registering or broadcasting; no unanswerable card may exist.
    assert st._question_futures == {}
    assert st._pending_questions == {}
    assert st.broadcasts == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_collision_surfaces_as_400_not_500() -> None:
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question

    st = _state()
    st._slots = {"chat-1": MagicMock()}
    request = MagicMock()
    request.app = {"state": st}
    _as_owner(request)

    async def _json() -> dict:
        return {"session_key": "dashboard:chat-1", "questions": _questions()}

    request.json = _json
    with patch.object(
        DashboardState, "request_question", side_effect=ValueError("collapse after redaction")
    ):
        resp = await api_ask_question(request)
    assert resp.status == 400
    assert "redaction" in json.loads(resp.text)["error"]
