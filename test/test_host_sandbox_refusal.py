"""A chat the host cannot start for want of an OS sandbox: typed, and plain to a non-owner.

The machine owner keeps the whole diagnostic (the host's runbook). Every other reader
(a Shared Crew teammate, an app token) gets one plain line with the code, on the
websocket, the chat SSE stream and an HTTP read, whatever the redaction setting reads
(``dashboard.host_sandbox_refusal``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _drive, _runner_state, _slot
from chat_test_helpers import _make_app, _make_state

from kiro_crew import redaction_seams
from kiro_crew.dashboard import host_sandbox_refusal as hsr
from kiro_crew.dashboard import redaction_audience
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.shared_crew.teammate_access import TEAMMATE_WS_FLAG
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, finished

_SLOT = "host-sandbox-slot"
#: The host detail the owner's runbook names and no one else may read.
_HOST_DETAIL = ("unshare(CLONE_NEWNS)", "apparmor_restrict_unprivileged_userns", "config.json")
_DIAGNOSTIC = (
    "Sandbox backend unavailable and allow_unsandboxed_exec is not set. No OS-level "
    "sandbox backend is available on this host, and the agent subprocess cannot be "
    "safely isolated. Probe detail: unshare(CLONE_NEWNS) failed with errno 1 (EPERM). "
    "kernel.apparmor_restrict_unprivileged_userns=1, the default on Ubuntu 23.10+. Set "
    "agent.sandbox_allow_unsandboxed_exec=true in ~/.kiro/crew/config.json."
)


def _refusal(kind: str = "no_backend") -> SandboxUnavailableError:
    return SandboxUnavailableError(_DIAGNOSTIC, kind=kind, detail="unshare failed")


def _has_detail(text: str) -> bool:
    return any(part in text for part in _HOST_DETAIL)


@pytest.fixture(params=[True, False], ids=["off", "on"])
def off(request, monkeypatch):
    """The owner's redaction setting: neither value decides who reads the detail."""
    monkeypatch.setattr(redaction_seams, "_redaction_off", request.param)
    return request.param


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    return state


# ── the row ──


@pytest.mark.asyncio
async def test_a_turn_the_host_cannot_sandbox_leaves_a_typed_row_with_the_whole_diagnostic(
    tmp_path,
) -> None:
    state, _client = _runner_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(side_effect=_refusal())
    slot = _slot()
    await _drive(state, slot)

    error = next(row for row in slot.messages if row["role"] == "error")
    assert error["content"] == f"{hsr.HOST_SANDBOX_UNAVAILABLE}: {_DIAGNOSTIC}"
    assert error["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE
    assert datetime.fromisoformat(error["meta"]["at"]).tzinfo is not None
    assert "retryable" not in error["meta"]


@pytest.mark.asyncio
async def test_a_sandbox_launcher_that_fails_at_start_leaves_the_same_typed_row(tmp_path) -> None:
    from kiro_crew.acp.transport_errors import AcpSandboxInitFailed

    state, _client = _runner_state(tmp_path)
    failed = AcpSandboxInitFailed(
        layer="kirocrew", detail="PermissionError: '/proc/1/setgroups'", corroborated=True
    )
    state.sessions.get_or_create = AsyncMock(side_effect=failed)
    slot = _slot()
    await _drive(state, slot)

    error = next(row for row in slot.messages if row["role"] == "error")
    assert error["content"].startswith(f"{hsr.HOST_SANDBOX_UNAVAILABLE}: ")
    assert "setgroups" in error["content"], "the owner keeps the diagnostic"
    assert error["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE
    shown = hsr.for_non_owner(error)
    assert shown["content"] == hsr.NON_OWNER_TEXT
    assert "setgroups" not in json.dumps(shown)


def test_a_momentary_refusal_is_retryable_and_a_wrapped_one_is_found() -> None:
    assert hsr.row_meta(_refusal("transient"))["retryable"] is True
    try:
        try:
            raise _refusal()
        except SandboxUnavailableError as inner:
            raise RuntimeError("session start failed") from inner
    except RuntimeError as outer:
        assert isinstance(hsr.host_sandbox_refusal(outer), SandboxUnavailableError)
    assert hsr.host_sandbox_refusal(RuntimeError("something else")) is None


def test_the_background_start_stop_row_is_typed_and_loses_its_detail_for_a_non_owner() -> None:
    from kiro_crew.dashboard.chat_turn.eager_spawn import (
        _EAGER_SPAWN_FAILURE_CAP,
        _note_eager_spawn_failure,
    )

    slot = _slot("eager-host-sandbox")
    for _ in range(_EAGER_SPAWN_FAILURE_CAP):
        _note_eager_spawn_failure(slot, _refusal())
    row = next(row for row in slot.messages if row["role"] == "error")
    assert row["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE
    assert row["meta"]["notice"] == "eager_spawn_stopped"
    assert _has_detail(json.dumps(row))

    shown = hsr.for_non_owner(row)
    assert shown["content"] == hsr.NON_OWNER_TEXT
    assert "notice" not in shown["meta"] and "notice_args" not in shown["meta"]
    assert shown["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE
    assert not _has_detail(json.dumps(shown))


def test_the_background_start_stop_row_is_typed_by_its_content_when_the_meta_is_gone() -> None:
    from kiro_crew.dashboard.chat_turn.eager_spawn import (
        _EAGER_SPAWN_FAILURE_CAP,
        _note_eager_spawn_failure,
    )

    slot = _slot("eager-host-sandbox-no-meta")
    for _ in range(_EAGER_SPAWN_FAILURE_CAP):
        _note_eager_spawn_failure(slot, _refusal())
    row = next(row for row in slot.messages if row["role"] == "error")
    assert row["content"].startswith(f"{hsr.HOST_SANDBOX_UNAVAILABLE}: ")
    assert _has_detail(row["content"]), "the owner keeps the diagnostic"

    bare = {key: value for key, value in row.items() if key != "meta"}
    shown = hsr.for_non_owner(bare)
    assert shown["content"] == hsr.NON_OWNER_TEXT
    assert not _has_detail(json.dumps(shown))
    text = hsr.text_for_non_owner(json.dumps({"type": "chat_message", "data": bare}))
    assert json.loads(text)["data"]["content"] == hsr.NON_OWNER_TEXT
    assert not _has_detail(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["capability_error", "capability_startup_error"])
async def test_a_refusal_wrapped_in_a_capability_error_is_still_typed(tmp_path, wrapper) -> None:
    from kiro_crew.agent_capabilities import CapabilityError
    from kiro_crew.session_capabilities import CapabilityStartupError

    # Codes the capability arms of ``_on_error`` would otherwise claim.
    outer: Exception = (
        CapabilityError("materialization_changed")
        if wrapper == "capability_error"
        else CapabilityStartupError("capability_harness_unsupported")
    )
    outer.__cause__ = _refusal()
    state, _client = _runner_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(side_effect=outer)
    slot = _slot()
    await _drive(state, slot)

    error = next(row for row in slot.messages if row["role"] == "error")
    assert error["content"] == f"{hsr.HOST_SANDBOX_UNAVAILABLE}: {_DIAGNOSTIC}"
    assert error["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE
    shown = hsr.for_non_owner(error)
    assert shown["content"] == hsr.NON_OWNER_TEXT
    assert not _has_detail(json.dumps(shown))


#: A sandbox-init row as a build without the typed prefix stored it: the card's glyph,
#: the launcher's stderr (the host path) and the owner's switch, and no ``meta.code``.
_UNTYPED_INIT_ROW = {
    "role": "error",
    "content": (
        "\u274c the kirocrew sandbox failed to initialize, so the agent process could not "
        "start -- PermissionError: [Errno 1] setgroups File "
        '"/srv/crew-host/run/kirocrew_sandbox_launcher.py", line 3040. Retrying '
        "cannot fix this: run `kirocrew config set agent.sandbox off`"
    ),
    "meta": {"mid": "m-1"},
}
_UNTYPED_DETAIL = ("/srv/crew-host", "kirocrew config set agent.sandbox off", "setgroups")


@pytest.mark.parametrize("framing", ["json", "lines", "socket"])
def test_a_stored_untyped_sandbox_init_row_loses_its_detail_for_a_non_owner(framing) -> None:
    rows = [{"role": "user", "content": "Run echo hi"}, _UNTYPED_INIT_ROW]
    if framing == "socket":
        out = hsr.text_for_non_owner(json.dumps({"type": "chat_message", "data": rows[1]}))
    elif framing == "lines":
        body = "\n".join(json.dumps(r) for r in rows).encode()
        out = (hsr.body_for_non_owner(body) or b"").decode()
    else:
        out = (hsr.body_for_non_owner(json.dumps({"messages": rows}).encode()) or b"").decode()
    assert hsr.NON_OWNER_TEXT in out
    assert not any(part in out for part in _UNTYPED_DETAIL)
    # Typed for the dashboard's host card, as a row stored with the code is.
    assert f'"code": "{hsr.HOST_SANDBOX_UNAVAILABLE}"' in out
    assert "Run echo hi" in out or framing == "socket"


#: Rows a build without the typed prefix stored, as it wrote them: a no-backend refusal
#: as the whole row, a launcher failure behind the ``Error:`` lead, and the
#: background-start stop notice whose last error (in its content and its
#: ``notice_args``) is each raiser's refusal. None carries ``meta.code``.
_INIT_FAILED = (
    "the kirocrew sandbox failed to initialize, so the agent process could not start -- "
    "PermissionError: [Errno 1] setgroups File "
    '"/srv/crew-host/run/kirocrew_sandbox_launcher.py". Retrying cannot fix this: run '
    "`kirocrew config set agent.sandbox off`"
)
_STOP_TEXT = (
    "This chat's agent failed to start 3 times in a row, so it will not be started again "
    "in the background. Send a message to try again. Last error: {detail}"
)
_LEGACY_ROWS = {
    "no_backend": (_DIAGNOSTIC, None),
    "init_failed_after_a_lead": (f"Error: {_INIT_FAILED}", None),
    **{
        f"background_stop_{name}": (
            _STOP_TEXT.format(detail=detail),
            {"notice": "eager_spawn_stopped", "notice_args": {"failures": 3, "detail": detail}},
        )
        for name, detail in (("no_backend", _DIAGNOSTIC), ("init_failed", _INIT_FAILED))
    },
}


def _legacy_detail(text: str) -> bool:
    return _has_detail(text) or any(part in text for part in _UNTYPED_DETAIL)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_LEGACY_ROWS))
@pytest.mark.parametrize("user", ["local-app", "teammate-7"], ids=["owner", "teammate"])
async def test_a_legacy_refusal_row_reaches_a_teammate_as_the_plain_line(
    state, off, user, shape
) -> None:
    content, meta = _LEGACY_ROWS[shape]
    state.get_slot(_SLOT).append("error", content, "msg msg-err", meta=meta)
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    app.middlewares.insert(2, redaction_audience.redaction_audience_middleware)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/chat/slots/{_SLOT}")
        assert response.status == 200
        body = await response.text()
    rows = [m for m in json.loads(body)["messages"] if m.get("role") == "error"]
    assert len(rows) == 1
    if user == "local-app":
        # The owner reads the row as it was stored.
        assert rows[0]["content"] == content
        assert (rows[0].get("meta") or {}).get("notice_args") == (meta or {}).get("notice_args")
        return
    assert rows[0]["content"] == hsr.NON_OWNER_TEXT
    assert rows[0]["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE
    assert "notice" not in rows[0]["meta"] and "notice_args" not in rows[0]["meta"]
    assert not _legacy_detail(body)


@pytest.mark.parametrize("shape", sorted(_LEGACY_ROWS))
def test_a_legacy_refusal_row_on_a_teammates_socket_holds_no_detail(shape) -> None:
    content, meta = _LEGACY_ROWS[shape]
    row = {"role": "error", "content": content, **({"meta": meta} if meta else {})}
    text = hsr.text_for_non_owner(json.dumps({"type": "chat_message", "data": row}))
    assert json.loads(text)["data"]["content"] == hsr.NON_OWNER_TEXT
    assert not _legacy_detail(text)


@pytest.mark.parametrize("stored", ["typed", "untyped"])
def test_session_search_holds_no_excerpt_of_a_refusal_row(tmp_path, stored) -> None:
    """A search excerpt carries no row type to redact by, so the row is not searched."""
    from kiro_crew.history import ConversationLog

    refusal = (
        {"role": "error", "content": hsr.row_text(_DIAGNOSTIC), "meta": hsr.row_meta(_refusal())}
        if stored == "typed"
        else _UNTYPED_INIT_ROW
    )
    lines = [
        {"_type": "metadata", "title": "shell check"},
        {"role": "user", "content": "Run echo hi before the unshare probe"},
        refusal,
    ]
    (tmp_path / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    rows = ConversationLog(tmp_path).search_sessions("unshare")
    assert [r["key"] for r in rows] == ["s1"]
    snippet = rows[0].get("snippet", "")
    assert "probe" in snippet
    assert not _has_detail(snippet) and not any(part in snippet for part in _UNTYPED_DETAIL)
    assert ConversationLog(tmp_path).search_sessions("apparmor_restrict_unprivileged_userns") == []


@pytest.mark.parametrize("stored", ["typed", "untyped"])
def test_the_session_list_preview_never_quotes_a_refusal_row(tmp_path, stored) -> None:
    """`GET /api/sessions?preview=1` hands a teammate a bare string with no row type
    to redact by, so a chat ending on a refusal previews the message before it."""
    from kiro_crew.history import ConversationLog

    refusal = (
        {"role": "error", "content": hsr.row_text(_DIAGNOSTIC), "meta": hsr.row_meta(_refusal())}
        if stored == "typed"
        else _UNTYPED_INIT_ROW
    )
    lines = [
        {"_type": "metadata", "title": "shell check"},
        {"role": "user", "content": "Run echo hi"},
        refusal,
    ]
    (tmp_path / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    preview = ConversationLog(tmp_path).last_message_preview("s1")
    assert preview == "Run echo hi"
    assert not _legacy_detail(preview)


def test_the_untyped_wording_outside_an_error_row_is_left_alone() -> None:
    said = {"role": "user", "content": _UNTYPED_INIT_ROW["content"]}
    assert hsr.for_non_owner(said) is said


def test_no_other_row_changes() -> None:
    rows = [
        {"role": "error", "content": "something else broke", "meta": {"code": "other"}},
        {"role": "user", "content": f"{hsr.HOST_SANDBOX_UNAVAILABLE}: typed by a person"},
    ]
    assert hsr.for_non_owner(rows) is rows
    assert hsr.body_for_non_owner(json.dumps(rows).encode()) is None


# ── each edge ──


class _Socket:
    def __init__(self, **items) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self.got = asyncio.Event()
        self._items: dict = {"_is_dashboard_user": True, **items}

    def get(self, key, default=None):
        return self._items.get(key, default)

    def __setitem__(self, key, value) -> None:
        self._items[key] = value

    def pop(self, key, default=None):
        return self._items.pop(key, default)

    async def send_str(self, msg: str) -> None:
        self.sent.append(json.loads(msg))
        self.got.set()


@pytest.mark.asyncio
async def test_a_teammates_socket_gets_the_plain_line_and_the_owners_the_diagnostic(
    state, off
) -> None:
    owner, teammate = _Socket(), _Socket(**{TEAMMATE_WS_FLAG: True})
    state._ws_clients.extend([owner, teammate])
    state._owner_ws_clients.update({owner, teammate})
    row = {
        "slot": _SLOT,
        "role": "error",
        "content": hsr.row_text(_DIAGNOSTIC),
        "meta": hsr.row_meta(_refusal()),
    }
    state.broadcast_ws("chat_message", row)
    await finished(asyncio.gather(owner.got.wait(), teammate.got.wait()), "both sends")

    assert _has_detail(json.dumps(owner.sent))
    assert not _has_detail(json.dumps(teammate.sent))
    shown = teammate.sent[0]["data"]
    assert shown["content"] == hsr.NON_OWNER_TEXT
    assert shown["meta"]["code"] == hsr.HOST_SANDBOX_UNAVAILABLE


def _as_caller(user: str):
    from aiohttp import web

    @web.middleware
    async def caller(request, handler):
        request["user"] = user
        request["app"] = ""
        return await handler(request)

    return caller


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["local-app", "teammate-7"], ids=["owner", "teammate"])
async def test_a_history_read_holds_the_diagnostic_only_for_the_owner(state, off, user) -> None:
    state.get_slot(_SLOT).append(
        "error", hsr.row_text(_DIAGNOSTIC), "msg msg-err", meta=hsr.row_meta(_refusal())
    )
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    app.middlewares.insert(2, redaction_audience.redaction_audience_middleware)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/chat/slots/{_SLOT}")
        assert response.status == 200
        body = await response.text()
    assert hsr.HOST_SANDBOX_UNAVAILABLE in body
    assert _has_detail(body) is (user == "local-app")


def test_an_archived_transcript_body_loses_the_detail_whatever_its_content_type() -> None:
    """``GET /api/session/archive/{name}`` answers JSON lines as
    ``application/x-ndjson``: each row line is read on its own."""
    from aiohttp import web

    rows = [
        {"role": "user", "content": "hi"},
        {"role": "error", "content": hsr.row_text(_DIAGNOSTIC), "meta": hsr.row_meta(_refusal())},
    ]
    text = "\n".join(json.dumps(r) for r in rows) + "\n"
    response = web.Response(text=text, content_type="application/x-ndjson")
    shown = redaction_audience._without_host_detail(response)
    body = bytes(shown.body).decode()
    lines = [json.loads(line) for line in body.splitlines() if line]
    assert lines[0] == rows[0]
    assert lines[1]["content"] == hsr.NON_OWNER_TEXT
    assert not _has_detail(body)


def test_a_body_that_only_names_the_code_goes_out_as_it_is() -> None:
    """A line that is not JSON is no row: a source file naming the code is untouched."""
    from aiohttp import web

    text = f'CODE = "{hsr.HOST_SANDBOX_UNAVAILABLE}"\n'
    response = web.Response(text=text, content_type="text/plain")
    assert bytes(redaction_audience._without_host_detail(response).body).decode() == text


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["local-app", "teammate-7"], ids=["owner", "teammate"])
async def test_a_gzipped_history_read_holds_the_diagnostic_only_for_the_owner(
    state, off, user
) -> None:
    from kiro_crew.dashboard.conditional_get import GZIP_MIN_BYTES

    slot = state.get_slot(_SLOT)
    # Past the size the history read gzips at, so the scrub meets compressed bytes.
    for i in range(GZIP_MIN_BYTES // 64 + 1):
        slot.append("user", f"padding message {i} " + "x" * 64, "msg msg-user")
    slot.append("error", hsr.row_text(_DIAGNOSTIC), "msg msg-err", meta=hsr.row_meta(_refusal()))
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    app.middlewares.insert(2, redaction_audience.redaction_audience_middleware)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/chat/slots/{_SLOT}", headers={"Accept-Encoding": "gzip"})
        assert response.status == 200
        assert response.headers.get("Content-Encoding") == "gzip"
        body = await response.text()
    assert hsr.HOST_SANDBOX_UNAVAILABLE in body
    assert _has_detail(body) is (user == "local-app")


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["local-app", "teammate-7"], ids=["owner", "teammate"])
async def test_a_chat_sse_stream_holds_the_diagnostic_only_for_the_owner(
    state, off, monkeypatch, user
) -> None:
    async def reply(
        st,
        slot,
        message,
        *,
        _directive_user_origin,
        _directive_owner_origin=False,
        _stop_hold_stamp="",
    ):
        slot.append(
            "error", hsr.row_text(_DIAGNOSTIC), "msg msg-err", meta=hsr.row_meta(_refusal())
        )
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", reply)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/chat", json={"slot": _SLOT, "message": "go on"})
        assert response.status == 200
        body = await finished(response.text(), "response.text", timeout=FALLBACK_TIMEOUT_SECS)
    errors = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]" and '"error"' in line
    ]
    assert len(errors) == 1
    assert errors[0]["content"].startswith(f"{hsr.HOST_SANDBOX_UNAVAILABLE}:")
    assert _has_detail(errors[0]["content"]) is (user == "local-app")
