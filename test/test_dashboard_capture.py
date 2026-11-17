"""``dashboard_capture``: a Bot reads its own live page from the person's open tab.

The gateway sends one owner-only ``dashboard_capture`` frame and takes the first valid
reply on the owner-only ``POST /api/members/{slug}/dashboard/capture``. These tests pin
each limit: own page only, request only, read only, the size cap, one answer from
several tabs, the no-tab answer, owner-only replies, redaction on the way to the Bot,
and the frame script that reads the page.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import FakeState, _fake_auth

from kiro_crew import dashboard_frame, mcp_panel, mcp_panel_capture
from kiro_crew.dashboard import dashboard_capture
from kiro_crew.dashboard.dashboard_capture import CaptureError, CaptureHub, capture_hub_for
from kiro_crew.dashboard.handlers import agent_panel, agent_panel_capture

OWNER = {"X-Test-Auth": "owner"}
AGENT = {"X-Test-Auth": "internal", "X-Session-Key": "member-atlas"}
SLUG = "atlas"


def _captured(payload: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = {
        "request_id": payload["request_id"],
        "slug": payload["slug"],
        "status": "captured",
        "text": "Queue 3\nBlocked: none",
        "fields": [{"label": "Note", "value": "typed by the person"}],
        "truncated": False,
    }
    body.update(over)
    return body


class TabsState(FakeState):
    """A gateway whose owner tabs each answer a capture frame with ``answers[i](frame)``.

    A tab whose answer is ``None`` stays silent, as a hidden tab or one showing
    another page does.
    """

    def __init__(self, *answers: Any) -> None:
        super().__init__()
        self.client: Any = None
        self.answers = answers
        self.replies: list[tuple[int, Any]] = []
        self.mutations = 0

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        for answer in self.answers:
            body = answer(payload) if answer is not None else None
            if body is None:
                continue
            r = await self.client.post(
                f"/api/members/{SLUG}/dashboard/capture", json=body, headers=OWNER
            )
            self.replies.append((r.status, await r.json()))
        return len(self.answers)


@pytest.fixture
def caller(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """The caller gate, answering as Bot ``Atlas``; a test may replace the answer."""
    who = [(SLUG, "Atlas", "member-atlas")]

    async def gate(_request: web.Request, operation: str) -> Any:
        assert operation == "dashboard_capture"
        return who[0], None

    monkeypatch.setattr(agent_panel, "_resolve_dashboard_caller", gate)
    return who


def _run(state: FakeState, fn: Any) -> Any:
    async def main() -> Any:
        app = web.Application(middlewares=[_fake_auth])
        app["state"] = state
        app.router.add_get(
            "/api/agent-panel/dashboard/capture", agent_panel_capture.api_dashboard_capture
        )
        app.router.add_post(
            "/api/members/{slug}/dashboard/capture",
            agent_panel_capture.api_member_dashboard_capture,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            if hasattr(state, "client"):
                state.client = client
            return await fn(client)
        finally:
            await client.close()

    return asyncio.run(main())


def _ask(state: FakeState, query: str = "") -> tuple[int, Any]:
    async def go(c: TestClient) -> tuple[int, Any]:
        r = await c.get(f"/api/agent-panel/dashboard/capture{query}", headers=AGENT)
        return r.status, await r.json()

    return _run(state, go)


# ── the agent's half ──


def test_a_visible_tab_on_the_bots_page_answers_with_its_text_and_values(caller) -> None:
    state = TabsState(_captured)
    status, body = _ask(state)
    assert status == 200, body
    assert body["capture"] == {
        "status": "captured",
        "text": "Queue 3\nBlocked: none",
        "fields": [{"label": "Note", "value": "typed by the person"}],
        "truncated": False,
    }
    ((kind, frame),) = state.frames
    # The frame names the caller's own slug and a request id, and nothing else.
    assert kind == "dashboard_capture" and set(frame) == {"request_id", "slug"}
    assert frame["slug"] == SLUG
    assert capture_hub_for(state).pending_count() == 0


def test_with_no_tab_open_nothing_is_captured(caller) -> None:
    state = FakeState()

    async def none_open(kind: str, payload: dict[str, Any]) -> int:
        return 0

    state.deliver_ws_owners = none_open  # type: ignore[method-assign]
    assert _ask(state) == (200, {"capture": {"status": "not_captured", "reason": "no_tab"}})


def test_when_only_hidden_tabs_are_open_the_wait_ends_with_no_tab(caller, monkeypatch) -> None:
    # No tab can answer here, so the wait always runs out: its length decides no outcome.
    monkeypatch.setattr(
        dashboard_capture, "CAPTURE_WAIT_SECONDS", 0.05
    )  # flake-ok: nothing answers
    state = TabsState(None, None)
    assert _ask(state) == (200, {"capture": {"status": "not_captured", "reason": "no_tab"}})
    assert capture_hub_for(state).pending_count() == 0


def test_of_several_tabs_the_first_answer_wins(caller) -> None:
    second = lambda p: _captured(p, text="another tab")  # noqa: E731
    status, body = _ask(TabsState(_captured, second))
    assert body["capture"]["text"] == "Queue 3\nBlocked: none"


def test_a_second_tabs_answer_to_the_same_request_is_refused_not_merged() -> None:
    async def go() -> tuple[str, dict[str, Any]]:
        hub = CaptureHub()
        p = hub.begin(SLUG)
        hub.deliver(SLUG, _captured({"request_id": p.request_id, "slug": SLUG}))
        with pytest.raises(CaptureError) as exc:
            hub.deliver(SLUG, _captured({"request_id": p.request_id, "slug": SLUG}, text="other"))
        return exc.value.code, p.future.result()

    code, first = asyncio.run(go())
    assert code == "capture_answered" and first["text"] == "Queue 3\nBlocked: none"


class SlowSocketState(TabsState):
    """One tab answers at once while another owner socket's send has not finished."""

    def __init__(self) -> None:
        super().__init__(_captured)
        self.drained = asyncio.Event()

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        delivered = await super().deliver_ws_owners(kind, payload)
        await self.drained.wait()
        return delivered


def test_an_answer_is_returned_while_another_socket_is_still_sending(caller) -> None:
    state = SlowSocketState()

    async def go(c: TestClient) -> tuple[int, Any]:
        ask = c.get("/api/agent-panel/dashboard/capture", headers=AGENT)
        try:
            r = await asyncio.wait_for(ask, timeout=dashboard_capture.CAPTURE_WAIT_SECONDS * 4)
            return r.status, await r.json()
        finally:
            state.drained.set()

    status, body = _run(state, go)
    assert status == 200 and body["capture"]["status"] == "captured"


def test_a_tab_that_cannot_read_a_static_page_says_so(caller) -> None:
    def static(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": p["request_id"],
            "slug": SLUG,
            "status": "unavailable",
            "reason": "static_page",
        }

    status, body = _ask(TabsState(static))
    assert body["capture"] == {"status": "unavailable", "reason": "static_page"}


def test_a_root_session_has_no_page_to_capture_and_no_tab_is_asked(caller) -> None:
    caller[0] = ("dashboard-chat-1", "", "chat-1")
    state = TabsState(_captured)
    status, body = _ask(state)
    assert body["capture"]["status"] == "not_captured"
    assert state.frames == []


def test_the_tool_takes_no_arguments(caller) -> None:
    state = TabsState(_captured)
    status, body = _ask(state, "?slug=someone-else")
    assert (status, body["code"]) == (400, "validation_error")
    assert state.frames == []


def test_one_capture_of_a_page_runs_at_a_time() -> None:
    async def go() -> str:
        hub = CaptureHub()
        hub.begin(SLUG)
        with pytest.raises(CaptureError) as exc:
            hub.begin(SLUG)
        return exc.value.code

    assert asyncio.run(go()) == "busy"


# ── the tab's half: own page only, request only, size cap ──


def _reply_alone(
    state: FakeState, slug: str, body: dict[str, Any], headers=OWNER
) -> tuple[int, Any]:
    async def go(c: TestClient) -> tuple[int, Any]:
        r = await c.post(f"/api/members/{slug}/dashboard/capture", json=body, headers=headers)
        return r.status, await r.json()

    return _run(state, go)


def test_a_reply_nobody_asked_for_is_refused() -> None:
    state = FakeState()
    body = {
        "request_id": "c_made_up",
        "slug": SLUG,
        "status": "captured",
        "text": "x",
        "fields": [],
        "truncated": False,
    }
    status, out = _reply_alone(state, SLUG, body)
    assert (status, out["code"]) == (409, "capture_expired")


def _pending_reply(slug: str, mutate: Any) -> tuple[int, Any, bool]:
    """Open one request for SLUG, post *mutate*(reply) to *slug*'s route, report."""
    state = FakeState()

    async def go(c: TestClient) -> tuple[int, Any, bool]:
        p = capture_hub_for(state).begin(SLUG)
        body = mutate(_captured({"request_id": p.request_id, "slug": SLUG}))
        r = await c.post(f"/api/members/{slug}/dashboard/capture", json=body, headers=OWNER)
        return r.status, await r.json(), p.future.done()

    return _run(state, go)


def test_a_reply_for_another_bots_page_is_refused() -> None:
    status, out, done = _pending_reply("scout", lambda b: {**b, "slug": "scout"})
    assert (status, out["code"], done) == (409, "capture_wrong_page", False)


@pytest.mark.parametrize(
    "mutate, code",
    [
        (
            lambda b: {**b, "text": "x" * (dashboard_capture.TEXT_MAX_CHARS + 1)},
            "capture_too_large",
        ),
        (
            lambda b: {
                **b,
                "fields": [{"label": "a", "value": "v"}] * (dashboard_capture.FIELDS_MAX + 1),
            },
            "capture_too_large",
        ),
        (
            lambda b: {
                **b,
                "fields": [{"label": "a", "value": "v" * (dashboard_capture.FIELD_VALUE_MAX + 1)}],
            },
            "capture_too_large",
        ),
        (lambda b: {**b, "html": "<p>x</p>"}, "invalid_capture"),
        (
            lambda b: {**b, "fields": [{"label": "a", "value": "v", "type": "password"}]},
            "invalid_capture",
        ),
        (lambda b: {**b, "truncated": "no"}, "invalid_capture"),
        (lambda b: {**b, "status": "unavailable", "reason": "anything"}, "invalid_capture"),
    ],
)
def test_an_oversized_or_misshapen_reply_is_refused_whole(mutate: Any, code: str) -> None:
    status, out, done = _pending_reply(SLUG, mutate)
    assert out["code"] == code and done is False


def test_a_text_exactly_at_the_cap_is_accepted() -> None:
    status, out, done = _pending_reply(
        SLUG, lambda b: {**b, "text": "x" * dashboard_capture.TEXT_MAX_CHARS, "truncated": True}
    )
    assert (status, done) == (200, True)


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        # An app token is turned away by the members surface; a caller with no owner
        # session by the owner gate. Each gate answers as itself.
        ({"X-Test-Auth": "app"}, 404, "not_found"),
        ({}, 403, "owner_only"),
    ],
)
def test_only_the_owner_can_answer(headers: dict[str, str], status: int, code: str) -> None:
    got, out = _reply_alone(FakeState(), SLUG, {"request_id": "c_x"}, headers=headers)
    assert (got, out["code"]) == (status, code)


def test_a_capture_writes_nothing_to_the_gateway(caller) -> None:
    """Read only: a whole round trip leaves the data home as it found it."""
    from kiro_crew.config.paths import data_home

    def listing() -> dict[str, tuple[int, int]]:
        return {
            str(p): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(data_home().rglob("*"))
            if p.is_file()
        }

    before = listing()
    _status, body = _ask(TabsState(_captured))
    assert body["capture"]["status"] == "captured"
    assert listing() == before


# ── the Bot's half: the reply is redacted ──


def test_the_capture_reaches_the_bot_through_the_redaction_pass(monkeypatch) -> None:
    seen: list[str] = []

    def fake_get(route: str, **_kw: Any) -> dict[str, Any]:
        assert route == mcp_panel_capture.ROUTE
        return {
            "capture": {
                "status": "captured",
                "text": "token sk-live-123",
                "fields": [],
                "truncated": True,
            }
        }

    def fake_redact(text: str) -> str:
        seen.append(text)
        return text.replace("sk-live-123", "[REDACTED]")

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("member-atlas", ""))
    monkeypatch.setattr(mcp_panel, "_get", fake_get)
    monkeypatch.setattr(mcp_panel, "redact", fake_redact)
    out = mcp_panel._call_tool_inner("dashboard_capture", {})
    assert "sk-live-123" not in out and "[REDACTED]" in out
    assert seen and "Cut at the size limit" in out


def test_no_tab_tells_the_bot_the_person_must_have_the_page_on_screen() -> None:
    out = mcp_panel_capture.render_capture(
        {"capture": {"status": "not_captured", "reason": "no_tab"}}
    )
    assert out.startswith("Not captured:") and "on screen" in out


# ── the frame's half: the script that reads the page ──


def test_the_frame_reads_the_page_only_when_its_parent_asks() -> None:
    doc = dashboard_frame.compose("<p>hi</p>", dashboard_frame.read_payload({}))
    js = dashboard_frame._CAPTURE_JS
    assert js in doc
    # Before the refill probe, so its listener is not read as the page handling refills.
    assert doc.index(js) < doc.index(dashboard_frame._REFILL_PROBE_JS)
    assert "event.source !== parent" in js
    assert (
        dashboard_frame.CAPTURE_MESSAGE_TYPE in js and dashboard_frame.CAPTURED_MESSAGE_TYPE in js
    )
    # The caps are the gateway's own, and secrets in form controls are never read.
    assert str(dashboard_capture.TEXT_MAX_CHARS) in js
    for skipped in ("'password'", "'hidden'", "'file'"):
        assert skipped in js


#: A DOM just big enough for ``_CAPTURE_JS``: a body whose ``innerText`` leaves out whatever
#: is ``display: none`` (as a browser's does), a textarea and a contenteditable region
#: holding what a person typed, and one parent.
_NODE_HARNESS = r"""
const listeners = [];
const posted = [];
const parent = { postMessage: (m) => posted.push(m) };
const inlineStyle = () => {
  const m = {};
  return {
    getPropertyValue: (k) => (m[k] ? m[k].v : ''),
    getPropertyPriority: (k) => (m[k] ? m[k].p : ''),
    setProperty: (k, v, p) => { m[k] = { v, p: p || '' }; },
    removeProperty: (k) => { delete m[k]; },
  };
};
const control = (tag, type, value, label, extra) => Object.assign({
  tagName: tag, value, checked: false, id: '', labels: [], style: inlineStyle(),
  getAttribute: (k) => (k === 'type' ? type : k === 'aria-label' ? label : null),
}, extra || {});
const area = control('TEXTAREA', '', 'TYPED-AREA ' + 'y'.repeat(900), 'Long');
const edit = { tagName: 'DIV', isContentEditable: true, style: inlineStyle() };
edit.style.setProperty('display', 'block', 'important');
const controls = [
  control('INPUT', 'text', 'TYPED-NOTE', 'Note'),
  control('INPUT', 'password', 'hunter2', 'Password'),
  control('INPUT', 'hidden', 'csrf', 'token'),
  control('INPUT', 'search', 'TYPED-SEARCH', 'Search'),
  area,
  control('INPUT', 'checkbox', 'on', 'Agree', { checked: true }),
  control('INPUT', 'radio', 'b', 'Pick B', { checked: false }),
  control('INPUT', 'submit', 'Save it', 'Save'),
  control('SELECT', '', '', 'Size', { options: [{ selected: false, text: 'Small' }, { selected: true, text: 'Large' }] }),
  // A label the person can edit, and one that is not drawn: neither is read for its text.
  control('INPUT', 'checkbox', 'on', null, { id: 'edited-label', labels: [{ isContentEditable: true, getClientRects: () => [{}], innerText: 'TYPED-IN-LABEL' }] }),
  control('INPUT', 'checkbox', 'on', null, { id: 'hidden-label', labels: [{ isContentEditable: false, getClientRects: () => [], innerText: 'TYPED-IN-HIDDEN-LABEL' }] }),
  control('INPUT', 'checkbox', 'on', null, { id: 'plain', labels: [{ isContentEditable: false, getClientRects: () => [{}], innerText: 'Archive' }] }),
];
const shown = (el) => el.style.getPropertyValue('display') !== 'none';
const body = {
  get innerText() {
    return 'Page words' + (shown(area) ? ' TYPED-IN-AREA' : '') + (shown(edit) ? ' TYPED-IN-EDITABLE' : '')
      + 'x'.repeat(%(over)d);
  },
};
const document = {
  body, designMode: 'off',
  querySelectorAll: (sel) => (sel.includes('contenteditable') ? [area, edit] : controls),
};
const addEventListener = (type, fn) => { if (type === 'message') listeners.push(fn); };
%(script)s
const stranger = {};
for (const fn of listeners) fn({ source: stranger, data: { type: %(ask)s, nonce: 'n0' } });
for (const fn of listeners) fn({ source: parent, data: { type: %(ask)s, nonce: 'n1' } });
document.designMode = 'on';
for (const fn of listeners) fn({ source: parent, data: { type: %(ask)s, nonce: 'n2' } });
process.stdout.write(JSON.stringify({
  posted,
  values: controls.map((c) => c.value),
  editDisplay: [edit.style.getPropertyValue('display'), edit.style.getPropertyPriority('display')],
  areaDisplay: area.style.getPropertyValue('display'),
}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the frame script")
def test_the_frame_script_never_reads_typed_text_and_cuts_to_the_cap() -> None:
    import json
    import subprocess

    source = _NODE_HARNESS % {
        "over": dashboard_capture.TEXT_MAX_CHARS + 10,
        "script": dashboard_frame._CAPTURE_JS,
        "ask": json.dumps(dashboard_frame.CAPTURE_MESSAGE_TYPE),
    }
    run = subprocess.run(
        ["node", "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,  # flake-ok: a bound on a hung node, never the pass condition
        check=True,
    )
    out = json.loads(run.stdout)
    # Only the parent's request is answered, with its own nonce; the page in design mode
    # is wholly editable, so its text is not sent at all.
    answer, designing = out["posted"][0], out["posted"][1]
    assert len(out["posted"]) == 2
    assert answer["type"] == dashboard_frame.CAPTURED_MESSAGE_TYPE and answer["nonce"] == "n1"
    assert designing["nonce"] == "n2" and designing["text"] == ""
    assert answer["text"].startswith("Page words")
    assert len(answer["text"]) == dashboard_capture.TEXT_MAX_CHARS and answer["truncated"] is True
    # Nothing a person typed reaches the text or the fields.
    assert "TYPED" not in json.dumps(answer) and "hunter2" not in json.dumps(answer)
    unread = dashboard_capture.UNREAD_VALUE
    assert {f["label"]: f["value"] for f in answer["fields"]} == {
        "Note": unread,
        "Search": unread,
        "Long": unread,
        "Agree": "checked",
        "Pick B": "unchecked",
        "Save": "Save it",
        "Size": "Large",
        "edited-label": "unchecked",
        "hidden-label": "unchecked",
        "Archive": "unchecked",
    }
    # Read only: every control keeps its value, and a region hidden for the read gets its
    # own inline display back, priority included, or none if it had none.
    assert out["values"][0] == "TYPED-NOTE" and out["values"][2] == "csrf"
    assert out["editDisplay"] == ["block", "important"] and out["areaDisplay"] == ""
