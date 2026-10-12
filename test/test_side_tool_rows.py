"""Side Chat tool rows: a side turn draws its tool calls where they happened.

Split from ``test_side.py`` (file line cap). The rows, the ``settle`` frame and
the whole-text redaction passes are specified in
``docs/system-specs/modules/side.md``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from side_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _capture_broadcasts,
    _published_readonly_spec_fixture,
)

from kiro_crew.dashboard.handlers.side import _SIDE_TOOL_PREVIEW_CHARS, _run_side_turn
from kiro_crew.dashboard.side_state import SideState


def _tool_event(**fields):
    """An ``EVENT_TOOL_CALL`` / permission event as ``stream_and_collect`` hands it on."""
    from kiro_crew.acp.types import AcpEvent

    base = dict(
        kind="tool_call",
        title="Running: cat /etc/hosts",
        tool_call_id="call-1",
        tool_kind="execute",
        tool_purpose="read the hosts file",
        tool_input="cat /etc/hosts",
    )
    base.update(fields)
    return AcpEvent(**base)


def _client_rows(frames: list[dict]) -> list[tuple[str, str]]:
    """The rows the side reducer draws for these frames (``store/chat/side.ts``):
    a tool frame is a row of its own; a plain assistant frame is a delta that
    grows the assistant row at the tail or starts a new row under a tool row; a
    ``settle`` or terminal frame replaces the tail row's text (removes the row
    when it is empty) and starts no row when empty."""
    rows: list[tuple[str, str]] = []
    for d in frames:
        if d["role"] == "tool":
            rows.append(("tool", d["content"]))
            continue
        whole = d.get("settle") or d.get("final")
        if rows and rows[-1][0] == "assistant":
            if not whole:
                rows[-1] = ("assistant", rows[-1][1] + d["content"])
            elif d["content"]:
                rows[-1] = ("assistant", d["content"])
            else:
                rows.pop()
            continue
        if whole and not d["content"]:
            continue
        rows.append(("assistant", d["content"]))
    return rows


@pytest.mark.asyncio
async def test_side_turn_draws_tool_rows_where_they_happened(tmp_path, monkeypatch):
    """A tool attempt is a ``tool`` frame between the text frames, shaped as the
    main chat's row (``🔧 <title>`` + the call's meta), and the terminal frame
    carries only the text written AFTER the last tool row — the client has one
    answer row per segment, and a frame carrying the whole reply would land
    under the last of them a second time."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("Let me check. ")
        on_tool_call(_tool_event(), True)
        on_chunk("The file lists ")
        on_chunk("two hosts.")
        return "Let me check. The file lists two hosts."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    roles = [(d["role"], d["content"]) for d in frames]
    tool_at = next(i for i, (r, _c) in enumerate(roles) if r == "tool")
    # Text before the call, the call, text after it, then the terminal frame.
    assert "".join(c for r, c in roles[:tool_at] if r == "assistant") == "Let me check. "
    assert roles[tool_at] == ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts")
    assert frames[tool_at]["meta"] == {
        "tool_call_id": "call-1",
        "kind": "execute",
        "purpose": "read the hosts file",
        "input_preview": "cat /etc/hosts",
    }
    assert "final" not in frames[tool_at]
    final = [d for d in frames if d.get("final")]
    assert len(final) == 1
    assert final[0]["role"] == "assistant"
    assert final[0]["content"] == "The file lists two hosts."
    # The live deltas after the call are a prefix of the terminal frame (the
    # redactor withholds a trailing run until the frame proves it safe), which
    # is what lets the client replace that one row rather than append to it.
    streamed_after = "".join(d["content"] for d in frames[tool_at + 1 :] if not d.get("final"))
    assert streamed_after and final[0]["content"].startswith(streamed_after)
    # The sidecar's own history keeps the whole reply: it feeds a cold-start
    # recovery, which has no rows to split.
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert stored[-1]["content"] == "Let me check. The file lists two hosts."
    # A panel that joined the run part-way never drew the rows above, so the
    # terminal frame also carries the whole answer, as stored, under the
    # stored row's id (``sideRestore`` waits on it).
    assert final[0]["reply"] == stored[-1]["content"]
    assert final[0]["message_id"] == stored[-1]["id"]


@pytest.mark.asyncio
async def test_side_turn_with_no_tool_row_sends_no_reply_field(tmp_path, monkeypatch):
    """With no tool row the terminal frame's ``content`` is already the whole
    answer, so it carries no separate ``reply``."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("Two hosts.")
        return "Two hosts."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    final = [d for t, d in events if t == "chat.side_result" and d.get("final")]
    assert len(final) == 1
    assert final[0]["content"] == "Two hosts."
    assert "reply" not in final[0]


@pytest.mark.asyncio
async def test_side_turn_marks_a_refused_tool_and_redacts_its_fields(tmp_path, monkeypatch):
    """A refused attempt draws its ``🔧`` row plus the hidden ``🚫`` sibling the
    shared transcript reads as "blocked", and every LLM-authored field on the
    row — title, input preview, meta — goes through the frame redaction."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    raw_cred = "AKIAIOSFODNN7EXAMPLE"

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_tool_call(
            _tool_event(
                kind="permission_request",
                title=f"Running: echo {raw_cred} > creds",
                tool_call_id="call-9",
                tool_input=f"echo {raw_cred} > creds",
                tool_purpose=f"write {raw_cred}",
            ),
            False,
        )
        on_chunk("I can't write files here.")
        return "I can't write files here."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    tool_frames = [d for t, d in events if t == "chat.side_result" and d["role"] == "tool"]
    assert [d["content"][:1] for d in tool_frames] == ["🔧", "🚫"]
    assert (
        tool_frames[0]["meta"]["tool_call_id"] == tool_frames[1]["meta"]["tool_call_id"] == "call-9"
    )
    for frame in tool_frames:
        assert raw_cred not in frame["content"]
        assert all(raw_cred not in v for v in frame["meta"].values())
    assert "[REDACTED" in tool_frames[0]["content"]
    assert "[REDACTED" in tool_frames[0]["meta"]["input_preview"]
    # No text preceded the call, so the terminal frame is the whole reply.
    final = [d for t, d in events if t == "chat.side_result" and d.get("final")]
    assert final[-1]["content"] == "I can't write files here."


@pytest.mark.asyncio
async def test_side_turn_carries_the_withheld_tail_below_the_tool_row(tmp_path, monkeypatch):
    """The stream redactor withholds a trailing credential-shaped run until more
    text proves it safe. A tool call ends the row, but the frame that settles the
    row ABOVE the tool carries only what was committed: the held run may still
    become a credential with the text written after the call, and a frame cannot
    be taken back. The run is drawn under the tool row instead, once the text
    after the call has committed it."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    digest = "9f86d081884c7d659a2feaa0c55ad015"

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk(f"Commit {digest}")
        on_tool_call(_tool_event(), True)
        on_chunk("is the one.")
        return f"Commit {digest}is the one."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    tool_at = next(i for i, d in enumerate(frames) if d["role"] == "tool")
    # Above the tool: the committed prefix only, and no settle frame (the
    # whole-text pass changes nothing about it). The held digest is in no frame
    # above the tool row.
    above = [d for d in frames[:tool_at]]
    assert [d["content"] for d in above] == ["Commit "]
    assert not any(d.get("settle") for d in above)
    # Below it: the digest heads the first delta, drawn once.
    after = [d for d in frames[tool_at + 1 :] if d["role"] == "assistant"]
    assert after[0]["content"].startswith(digest)
    assert after[-1].get("final") is True
    assert _client_rows(frames) == [
        ("assistant", "Commit "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", f"{digest}is the one."),
    ]
    streamed_after = "".join(d["content"] for d in after if not d.get("final"))
    assert streamed_after and after[-1]["content"].startswith(streamed_after)


@pytest.mark.asyncio
async def test_side_turn_redacts_a_credential_the_model_split_around_a_tool_call(
    tmp_path, monkeypatch
):
    """``Authorization: `` | read the token | ``Bearer <token>``: the model writes
    the header name, fetches its value with a tool, and writes the value after
    the call. The batch pattern needs header and token in ONE string, so the
    stream redactor's held run (the header name) is kept across the tool row
    rather than flushed, and the token reaches no frame -- not a delta, not the
    frames that settle the rows on either side of the call. The header name goes
    where the pass over the whole reply puts it: under the tool row, folded into
    the tag with its value."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    token = "opaque" + "x7Qz" * 10  # no ``eyJ`` header: only the anchor can catch it

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("Set the header Authorization: ")
        on_tool_call(
            _tool_event(title="Read token.txt", tool_input="token.txt", tool_kind="read"), True
        )
        on_chunk(f"Bearer {token} and retry.")
        return f"Set the header Authorization: Bearer {token} and retry."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    for frame in frames:
        assert token not in frame["content"], frame
        assert all(token not in v for v in (frame.get("meta") or {}).values())
    assert _client_rows(frames) == [
        ("assistant", "Set the header "),
        ("tool", "🔧 Read token.txt\ntoken.txt"),
        ("assistant", "[REDACTED: credential] and retry."),
    ]
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert token not in stored[-1]["content"]


@pytest.mark.asyncio
async def test_side_turn_keeps_a_uri_password_split_around_a_tool_call_out_of_every_frame(
    tmp_path, monkeypatch
):
    """``postgres://user:pass`` | tool | ``@host``: the URI-userinfo pattern needs
    the ``@`` to recognise the password, so a whole-text pass over the text
    written BEFORE the call returns it unchanged. The frame that settles the row
    above the tool must therefore leave the redactor's held run out -- it is
    exactly the text that is a credential only once the ``@`` arrives -- and
    the run is redacted where the call's continuation completes it."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    password = "s3cretPASSw0rd"

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk(f"Use the DSN postgres://app:{password}")
        on_tool_call(_tool_event(title="Read db.env", tool_input="db.env", tool_kind="read"), True)
        on_chunk("@db.example.com and retry.")
        return f"Use the DSN postgres://app:{password}@db.example.com and retry."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    for frame in frames:
        assert password not in frame["content"], frame
        assert all(password not in v for v in (frame.get("meta") or {}).values())
    tool_at = next(i for i, d in enumerate(frames) if d["role"] == "tool")
    assert [d["content"] for d in frames[:tool_at]] == ["Use the DSN "]
    assert _client_rows(frames) == [
        ("assistant", "Use the DSN "),
        ("tool", "🔧 Read db.env\ndb.env"),
        ("assistant", "[REDACTED: credential]db.example.com and retry."),
    ]
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert password not in stored[-1]["content"]


@pytest.mark.asyncio
async def test_side_turn_settles_the_row_above_a_tool_call_with_a_whole_text_pass(
    tmp_path, monkeypatch
):
    """What the side panel draws is the whole-text pass over the reply, never the
    stream redactor's own output. The stream redactor commits a private-key block
    line by line, and a body line that is not credential-shaped on its own would
    stream as text; the pass over everything committed sees the block whole and
    redacts it as one, so the deltas carry the tag and never the body. With the
    live view already right, the row above the tool call needs no settle frame."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    # The runtime value is a real key header, which is what the redactor has to
    # recognise; only the source spelling is split, so the fork content scan
    # does not read this test as a committed key. Do not join these back up.
    _dashes = "-" * 5
    _kind = "RSA PRIVATE KEY"
    begin, end = f"{_dashes}BEGIN {_kind}{_dashes}", f"{_dashes}END {_kind}{_dashes}"
    key_body = "FAKEKEYMATERIALLINE1/abcdefghijklmnop"  # fake, asserted absent
    block = f"{begin}\n{key_body}\nMORELINES==\n{end}\n"

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk(f"key: {begin}\n{key_body}\n")
        on_chunk(f"MORELINES==\n{end}\nthen ")
        on_tool_call(_tool_event(), True)
        on_chunk("ok.")
        return f"key: {block}then ok."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    assert all(key_body not in d["content"] for d in frames)
    assert all("MORELINES" not in d["content"] for d in frames)
    tool_at = next(i for i, d in enumerate(frames) if d["role"] == "tool")
    above = [d["content"] for d in frames[:tool_at]]
    assert "".join(above) == "key: [REDACTED: credential]\nthen "
    assert not any(d.get("settle") for d in frames[:tool_at])
    assert _client_rows(frames) == [
        ("assistant", "key: [REDACTED: credential]\nthen "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", "ok."),
    ]


@pytest.mark.asyncio
async def test_side_turn_redacts_a_key_body_written_under_its_header_after_a_tool_call(
    tmp_path, monkeypatch
):
    """``-----BEGIN ... KEY-----⏎`` | tool | ``<base64 body>``: the header's newline
    would commit it alone, and the body is a run the stream redactor judges with
    nothing in front of it; a body line shorter than the bare-secret floor is
    plain text to it. The private-key pattern needs header and body in one
    text, so a key block the reply has opened is held back from its header
    until the footer closes it, and the whole block is drawn as one tag where
    the footer lands: under the tool row. The body reaches no frame: not a
    delta, not the frame that settles the row above, not the terminal frame."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    # Split spelling of the key header: see the whole-text-pass test above.
    _dashes = "-" * 5
    _kind = "RSA PRIVATE KEY"
    begin, end = f"{_dashes}BEGIN {_kind}{_dashes}", f"{_dashes}END {_kind}{_dashes}"
    body = "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFE"  # 35 chars: under the bare-secret floor

    for shape, after in {
        "body ends the reply": f"{body}",
        "body then footer": f"{body}\n{end}\nthen",
        "body in pieces": None,
    }.items():
        events.clear()
        parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
        parent._side.append_user("q")
        parent._side.last_run_id = "run-1"
        parent._side.is_complete = False

        async def _fake_stream(
            provider, message, *, on_chunk=None, on_tool_call=None, _after=after, **kwargs
        ):
            on_chunk(f"key: {begin}\n")
            on_tool_call(_tool_event(), True)
            if _after is None:
                on_chunk(body[:12])
                on_chunk(body[12:] + "\n")
                on_chunk(f"{end}\nthen")
                return f"key: {begin}\n{body}\n{end}\nthen"
            on_chunk(_after)
            return f"key: {begin}\n{_after}"

        monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

        await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

        frames = [d for t, d in events if t == "chat.side_result"]
        for frame in frames:
            assert body[:12] not in frame["content"], (shape, frame)
            assert body[12:] not in frame["content"], (shape, frame)
            assert begin not in frame["content"], (shape, frame)
        rows = _client_rows(frames)
        assert rows[0] == ("assistant", "key: "), shape
        assert rows[1][0] == "tool", shape
        tail = [r for r in rows[2:]]
        if shape == "body ends the reply":
            # No footer ever comes: the pass over the whole reply judges the
            # open block by its header and body line (the truncated-block form).
            assert tail == [("assistant", "[REDACTED: credential]")], shape
        else:
            assert tail == [("assistant", "[REDACTED: credential]\nthen")], shape
        stored = [m for m in parent._side.messages if m["role"] == "assistant"]
        assert body not in stored[-1]["content"], shape


@pytest.mark.asyncio
async def test_side_turn_redacts_a_tool_preview_before_cutting_it(tmp_path, monkeypatch):
    """A tool row's ``input_preview`` and ``purpose`` are cut to
    ``_SIDE_TOOL_PREVIEW_CHARS``. A cut inside a credential leaves a prefix no
    pattern matches (``postgres://user:pass`` without its ``@host``), so each
    field is redacted whole first and the redacted text is what gets cut."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    password = "s3cretPASSw0rd"
    # The cut lands between the ``:pass`` and the ``@host`` of the DSN.
    dsn = f"postgres://app:{password}@db.internal/x"
    pad = "x" * (_SIDE_TOOL_PREVIEW_CHARS - len("psql ") - len(f"postgres://app:{password}"))
    tool_input = f"psql {pad}{dsn}"
    assert tool_input[:_SIDE_TOOL_PREVIEW_CHARS].endswith(password)
    purpose = f"connect with {dsn}"  # short: the whole redacted field survives the cut

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_tool_call(
            _tool_event(title="Running: psql", tool_input=tool_input, tool_purpose=purpose), True
        )
        on_chunk("Connected.")
        return "Connected."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    tool = next(d for d in frames if d["role"] == "tool")
    assert password not in tool["content"]
    assert all(password not in v for v in tool["meta"].values())
    assert tool["meta"]["purpose"] == "connect with [REDACTED: credential]db.internal/x"
    # The long input is cut AFTER redaction: the password is gone and the cut
    # falls inside the tag that replaced it, never inside the password.
    preview = tool["meta"]["input_preview"]
    assert len(preview) == _SIDE_TOOL_PREVIEW_CHARS
    assert preview.startswith(f"psql {pad}") and "[REDACTED" in preview[len(pad) :]


@pytest.mark.asyncio
async def test_side_turn_live_view_follows_the_whole_text_pass_without_a_tool_row(
    tmp_path, monkeypatch
):
    """The same holds for a reply with no tool row: a key body line the stream
    redactor would commit as plain text (it has no header in its buffer) is not
    drawn, because every delta is the whole-text pass over what was committed so
    far. The terminal frame then extends the deltas instead of contradicting them."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    _dashes = "-" * 5
    _kind = "RSA PRIVATE KEY"
    begin, end = f"{_dashes}BEGIN {_kind}{_dashes}", f"{_dashes}END {_kind}{_dashes}"
    body = "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFE"

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk(f"key: {begin}\n")
        on_chunk(f"{body}\n{end}\n")
        on_chunk("then done")
        return f"key: {begin}\n{body}\n{end}\nthen done"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    assert all(body not in d["content"] for d in frames)
    deltas = "".join(d["content"] for d in frames if not d.get("final"))
    final = [d for d in frames if d.get("final")][-1]
    # ``done`` is held (a trailing run) until the terminal frame; the body and
    # the footer never appear, only the tag the pass over the whole put there.
    assert deltas == "key: [REDACTED: credential]\nthen "
    assert final["content"] == "key: [REDACTED: credential]\nthen done"
    assert final["content"].startswith(deltas)


@pytest.mark.asyncio
async def test_side_turn_draws_a_refusal_that_follows_its_tool_call_event_once(
    tmp_path, monkeypatch
):
    """kiro-cli reports the attempt as a tool-call event and THEN asks permission
    for it, so a refusal arrives for a row already drawn: it adds the hidden
    ``🚫`` sibling alone, not a second pill."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("Let me look. ")
        on_tool_call(_tool_event(tool_call_id="t1"), True)
        on_tool_call(_tool_event(kind="permission_request", tool_call_id="t1"), False)
        on_chunk("I can't run that here.")
        return "Let me look. I can't run that here."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    tool_frames = [d for d in frames if d["role"] == "tool"]
    assert [d["content"][:1] for d in tool_frames] == ["🔧", "🚫"]
    assert all(d["meta"]["tool_call_id"] == "t1" for d in tool_frames)
    assert _client_rows(frames) == [
        ("assistant", "Let me look. "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("tool", "🚫 Running: cat /etc/hosts"),
        ("assistant", "I can't run that here."),
    ]


@pytest.mark.asyncio
async def test_side_turn_closes_the_text_row_at_a_refusal_sibling_too(tmp_path, monkeypatch):
    """A delta that lands between a tool row and its ``🚫`` sibling is a row of
    its own on the client (every tool row starts a new one), so the server
    closes its row there too; otherwise the terminal frame, which carries the
    text after the server's last row, would draw that delta a second time."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("Let me look. ")
        on_tool_call(_tool_event(tool_call_id="t1"), True)
        on_chunk("Checking. ")
        on_tool_call(_tool_event(kind="permission_request", tool_call_id="t1"), False)
        on_chunk("I can't run that here.")
        return "Let me look. Checking. I can't run that here."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    assert frames[-1]["content"] == "I can't run that here." and frames[-1]["final"] is True
    assert _client_rows(frames) == [
        ("assistant", "Let me look. "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", "Checking. "),
        ("tool", "🚫 Running: cat /etc/hosts"),
        ("assistant", "I can't run that here."),
    ]


@pytest.mark.asyncio
async def test_side_turn_holds_a_key_block_the_pattern_cannot_judge_before_its_footer(
    tmp_path, monkeypatch
):
    """An indented key block (YAML ``key: |``): the truncated-block form of the
    private-key pattern knows only body lines that start right after the
    newline, so the pass over the text written before the call leaves this body
    line as text, and the pass over the whole reply (header, body, footer)
    removes it. The row above the tool cannot be taken back, so the block is
    held from its header until the footer closes it, and is drawn as one tag
    where the footer lands. Same for a JSON-escaped block."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    # Split spelling of the key header: see the whole-text-pass test above.
    _dashes = "-" * 5
    _kind = "RSA PRIVATE KEY"
    begin, end = f"{_dashes}BEGIN {_kind}{_dashes}", f"{_dashes}END {_kind}{_dashes}"
    body = "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFE"  # 35 chars: under the bare-secret floor

    for shape, before, after, rows in [
        (
            "yaml",
            f"key: |\n    {begin}\n    {body}\n",
            f"    {end}\ndone",
            [("assistant", "key: |\n    "), ("assistant", "[REDACTED: credential]\ndone")],
        ),
        (
            "json",
            '{"key": "' + begin + "\\n" + body + "\\n",
            end + '"}',
            [("assistant", '{"key": "'), ("assistant", '[REDACTED: credential]"}')],
        ),
    ]:
        events.clear()
        parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
        parent._side.append_user("q")
        parent._side.last_run_id = "run-1"
        parent._side.is_complete = False

        async def _fake_stream(
            provider, message, *, on_chunk=None, on_tool_call=None, _b=before, _a=after, **kw
        ):
            on_chunk(_b)
            on_tool_call(_tool_event(), True)
            on_chunk(_a)
            return _b + _a

        monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

        await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

        frames = [d for t, d in events if t == "chat.side_result"]
        for frame in frames:
            assert body not in frame["content"], (shape, frame)
            assert begin not in frame["content"], (shape, frame)
        got = _client_rows(frames)
        assert got[1][0] == "tool", shape
        assert [got[0], got[2]] == rows and len(got) == 3, (shape, got)
        stored = [m for m in parent._side.messages if m["role"] == "assistant"]
        assert body not in stored[-1]["content"], shape


@pytest.mark.asyncio
async def test_side_turn_holds_a_uri_password_the_stream_redactor_would_commit(
    tmp_path, monkeypatch
):
    """``postgres://app:pa!ss`` | tool | ``@db/x``: ``!`` is outside the stream
    redactor's credential class, so it commits ``postgres://app:pa!`` as text,
    and the URI pattern cannot judge it without the ``@``. The open URL is held
    from its scheme until the whitespace that ends it; nothing of the URI is
    above the tool row, and the password reaches no frame."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    password = "pa!ss"

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk(f"Use the DSN postgres://app:{password}")
        on_tool_call(_tool_event(title="Read db.env", tool_input="db.env", tool_kind="read"), True)
        on_chunk("@db/x now.")
        return f"Use the DSN postgres://app:{password}@db/x now."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    for frame in frames:
        assert password not in frame["content"] and "postgres" not in frame["content"], frame
    assert _client_rows(frames) == [
        ("assistant", "Use the DSN "),
        ("tool", "🔧 Read db.env\ndb.env"),
        ("assistant", "[REDACTED: credential]db/x now."),
    ]


@pytest.mark.asyncio
async def test_side_turn_settles_a_row_whose_drawn_text_a_later_pass_rewrote(tmp_path, monkeypatch):
    """The live view draws what the whole-text pass produces; a later pass can
    still rewrite text a row already shows when the batch redactor judges the
    earlier text by what follows it. The delta that would contradict the row is
    withheld, and the frame that closes the row (``settle``) carries the row as
    the pass has it, replacing the streamed text. No pattern of the real
    redactor does this once the open key block and URL are held, so the
    mechanism is pinned with a redactor whose judgment of ``alpha`` waits for
    ``omega``."""
    from kiro_crew.dashboard import side_segment_redactor as side_module

    real_redact = side_module.redact

    def _late_judgment(text: str) -> str:
        out = real_redact(text)
        return out.replace("alpha", "[REDACTED: credential]") if "omega" in text else out

    monkeypatch.setattr(side_module, "redact", _late_judgment)
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("say alpha ")
        on_chunk("omega ")
        on_tool_call(_tool_event(), True)
        on_chunk("done")
        return "say alpha omega done"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    tool_at = next(i for i, d in enumerate(frames) if d["role"] == "tool")
    settle = frames[tool_at - 1]
    assert settle.get("settle") is True and "final" not in settle
    assert settle["content"] == "say [REDACTED: credential] omega "
    # The rewriting delta was withheld: the deltas drawn are a prefix of the row.
    assert [d["content"] for d in frames[: tool_at - 1]] == ["say alpha "]
    assert _client_rows(frames) == [
        ("assistant", "say [REDACTED: credential] omega "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", "done"),
    ]


@pytest.mark.asyncio
async def test_side_turn_draws_a_carried_run_once_under_the_tool_row(tmp_path, monkeypatch):
    """The run the redactor still holds at the call opens the next segment's
    buffer. Nothing of it is in the row above (the settle frame carries only the
    committed text), and the next segment's commits draw it once, under the
    tool row, redacted in the context the later text gives it."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk("Header: Authorization: Be")  # the redactor holds a possible ``Bearer``
        on_tool_call(_tool_event(), True)
        on_chunk("x")  # ``Bex`` is no Bearer: the stream redactor commits the header
        on_chunk(" is fine.")  # then ``Bex is ``; the open label held both until here
        return "Header: Authorization: Bex is fine."

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    tool_at = next(i for i, d in enumerate(frames) if d["role"] == "tool")
    assert [d["content"] for d in frames[:tool_at]] == ["Header: "]
    deltas_after = [d["content"] for d in frames[tool_at + 1 :] if not d.get("final")]
    assert deltas_after == ["Authorization: Bex is "]
    assert _client_rows(frames) == [
        ("assistant", "Header: "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", "Authorization: Bex is fine."),
    ]


@pytest.mark.asyncio
async def test_side_turn_settles_a_segment_opened_inside_a_dropped_value_from_the_stream(
    tmp_path, monkeypatch
):
    """An over-long credential makes the stream redactor emit its tag and DROP the
    rest of the value (``StreamRedactor.discarding``). A tool call inside that
    value leaves the text under it opening on the value's anchorless remainder,
    which no pass over that text alone could recognise. The pass over the whole
    reply sees the value with its anchor and masks it as one, so neither row
    shows a byte of it."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    head, rest = "A" * 4200, "B" * 40  # past the redactor's hold-back cap

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_chunk(f"Authorization: Bearer {head}")
        on_tool_call(_tool_event(), True)
        on_chunk(f"{rest} done")
        return f"Authorization: Bearer {head}{rest} done"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    for frame in frames:
        assert rest not in frame["content"] and head[:64] not in frame["content"], frame
    tool_at = next(i for i, d in enumerate(frames) if d["role"] == "tool")
    assert [d["content"] for d in frames[:tool_at]] == ["[REDACTED: credential]"]
    assert _client_rows(frames) == [
        ("assistant", "[REDACTED: credential]"),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", " done"),
    ]
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert rest not in stored[-1]["content"] and head[:64] not in stored[-1]["content"]


@pytest.mark.asyncio
async def test_side_turn_keeps_a_value_the_stream_redactor_dropped_out_of_every_frame(
    tmp_path, monkeypatch
):
    """A JWT whose claims run past the stream redactor's hold-back ceiling before
    its signature: the stream redactor tags it once and drops the rest, holding
    nothing, and the batch patterns never recognise it (a signed JWT has two
    dots), so a pass over the reply as written would draw the claims. The passes
    see the dropped value as its tag instead: no live delta, settling frame,
    terminal frame or stored reply carries a byte of the claims, with or without
    a tool call inside the value, and the tag stands where the value was."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    claims = "".join("A1b2C3d4E5f6G7h8-_"[(i * 5) % 18] for i in range(4200))
    text = f"The token is eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.{claims} and that is all."
    chunks = [text[i : i + 64] for i in range(0, len(text), 64)]
    call_after = 40  # chunk index: inside the claims, before the drop arms

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        for i, chunk in enumerate(chunks):
            if i == call_after:
                on_tool_call(_tool_event(), True)
            on_chunk(chunk)
        return text

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    stored = [m for m in parent._side.messages if m["role"] == "assistant"][-1]["content"]
    for i in range(0, len(claims) - 11, 6):
        window = claims[i : i + 12]
        assert window not in stored, window
        for frame in frames:
            assert window not in frame["content"], (window, frame)
    assert stored == "The token is [REDACTED: credential] and that is all."
    assert _client_rows(frames) == [
        ("assistant", "The token is "),
        ("tool", "🔧 Running: cat /etc/hosts\ncat /etc/hosts"),
        ("assistant", "[REDACTED: credential] and that is all."),
    ]


@pytest.mark.asyncio
async def test_side_turn_that_ran_tools_but_wrote_nothing_keeps_the_fallback(tmp_path, monkeypatch):
    """A text-less turn still gets the read-only fallback sentence as its terminal
    frame, below the tool rows, rather than an empty frame."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_tool_call=None, **kwargs):
        on_tool_call(_tool_event(kind="permission_request"), False)
        return ""

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = [d for t, d in events if t == "chat.side_result"]
    assert [d["role"] for d in frames] == ["tool", "tool", "assistant"]
    assert frames[-1].get("final") is True
    assert frames[-1]["content"].startswith("Side Chat is read-only")
