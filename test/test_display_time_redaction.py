"""Regression tests for splitting redaction between load time and display time.

The startup restore used to redact BOTH `content` and `meta` on every message it
loaded, and that pass was silently covering for four emit sites that did not
redact. Removing it wholesale then leaked stored content through three further
paths that build model prompts (side-chat, orchestrator stage file, title model).

So the split is asymmetric, and measured:

  content -> still redacted ON LOAD. Only ~0.4s of the ~7s, but ~204 read sites,
             so a single chokepoint is the only holdable invariant.
  meta    -> redacted AT THE EMIT SITES. ~5.5s of the ~7s (tool_input payloads)
             is the real boot cost, and its 31 readers are tractable: outside the
             emit sites every one reads control fields only.

Each emit-site test here fails against the pre-change code for a DIFFERENT site,
so they cannot all be satisfied by re-adding a blanket load-time pass.

The `system` role is the crux of the emit-site half: the write path never redacted
it, the old load path did, and the emit paths did not — so `system` content
reached disk raw and was cleaned only in memory.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import (
    _build_history_prefix,
    _build_message_entry,
    _rehydrate_slot_from_history,
)
from kiro_crew.dashboard.chat_utils import _history_key_for, _prepare_messages

# A credential shape `redact_credentials` catches, kept in one place so a change
# to the detector surfaces as one failure rather than six.
SECRET = "AKIAIOSFODNN7EXAMPLE"


def _seed(state, slot_key: str, role: str, content: str) -> None:
    log = state.conversation_log
    assert log is not None
    log.append(_history_key_for(slot_key), role, content)


def _rewrite_last_meta(log, key: str, meta: dict) -> None:
    """Attach `meta` to the last stored record by editing the JSONL directly.

    ``ConversationLog.append()`` takes no ``meta=`` kwarg, and going through the
    normal write path would redact the payload before it landed — which is
    exactly what these tests need to NOT happen. Writing the file directly is the
    only way to stage bytes that a legacy or foreign writer could have left.
    """
    path = pathlib.Path(log._path(key))
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert lines, "precondition: something was written"
    rec = json.loads(lines[-1])
    rec["meta"] = meta
    lines[-1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")


# ── 1. emit path: _prepare_messages must cover `system` ──────────────────────


@pytest.mark.parametrize("role", ["assistant", "system", "tool"])
def test_prepare_messages_redacts_every_non_user_role(role: str) -> None:
    """`system` was excluded by the old gate, so stored secrets were emitted raw."""
    out = _prepare_messages([{"role": role, "content": f"key {SECRET}", "cls": "msg"}], False, live_child="")
    assert SECRET not in json.dumps(out), f"{role} content emitted unredacted"


def test_prepare_messages_leaves_user_content_alone() -> None:
    """User content is deliberately NOT redacted — the author is the only reader."""
    out = _prepare_messages(
        [{"role": "user", "content": f"key {SECRET}", "cls": "msg msg-u"}], False, live_child=""
    )
    assert SECRET in out[0]["content"]


def test_prepare_messages_redacts_stored_meta_not_just_cls_meta() -> None:
    """`dict(m)` is shallow, so stored meta reached the client by reference.

    The old code only overwrote `meta` when `parse_cls_meta(cls)` returned
    something; otherwise the loaded dict passed straight through. Load-time
    redaction was the only guard.
    """
    out = _prepare_messages(
        [{"role": "tool", "content": "ok", "cls": "msg", "meta": {"tool_input": SECRET}}],
        False,
        live_child="",
    )
    assert SECRET not in json.dumps(out), "stored meta emitted unredacted"


# ── 2. save path: _build_message_entry must cover `system` ───────────────────


@pytest.mark.parametrize("role", ["assistant", "system"])
def test_build_message_entry_redacts_every_non_user_role(role: str) -> None:
    """_save_slot_to_history rewrites the whole window, so this is a write-back gate.

    With load-time redaction gone, an unredacted `system` line from a legacy or
    foreign writer would otherwise survive every future rewrite.
    """
    entry = _build_message_entry({"role": role, "content": f"key {SECRET}", "cls": "msg"})
    assert entry is not None
    assert SECRET not in json.dumps(entry), f"{role} persisted unredacted"


# ── 3. ACP prompt path: _build_history_prefix must redact ───────────────────


def test_history_prefix_redacts_assistant_content(tmp_path, monkeypatch) -> None:
    """Its output is prepended to the ACP prompt, so kiro-cli persists it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    slot = state.get_or_create_slot("chat-1-prefix")
    slot.append("assistant", f"here is {SECRET}", "msg msg-a", broadcast=False)

    assert SECRET not in _build_history_prefix(slot)


def test_history_prefix_keeps_user_text() -> None:
    """User turns are the prompt's own history; redacting them would corrupt it."""
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1-u")
    slot.append("user", "my own note", "msg msg-u", broadcast=False)
    assert "my own note" in _build_history_prefix(slot)


# ── 3b. the OTHER prompt paths, found by the CI GPT lane ─────────────────────
#
# My own egress audit covered client-facing routes thoroughly and under-covered
# prompt builders. The GPT review lane caught `side_context`; auditing that class
# properly then turned up `chat_orchestrator` as the same defect writing to disk.
# Both are pinned here so the class stays closed.


def test_side_chat_parent_snapshot_redacts_assistant_content() -> None:
    """The side-chat prompt embeds parent turns, so it is an egress path."""
    from kiro_crew.dashboard.side_context import _format_parent_snapshot
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1-side")
    slot.append("assistant", f"here is {SECRET}", "msg msg-a", broadcast=False)

    snapshot = _format_parent_snapshot(slot)
    assert snapshot, "precondition: a snapshot was produced"
    assert SECRET not in snapshot


def test_side_chat_parent_snapshot_keeps_user_text() -> None:
    """Same carve-out as the history prefix: the user's own words must survive."""
    from kiro_crew.dashboard.side_context import _format_parent_snapshot
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1-sideu")
    slot.append("user", "my own question", "msg msg-u", broadcast=False)
    assert "my own question" in _format_parent_snapshot(slot)


def test_stage_result_capture_redacts_before_writing_to_disk(tmp_path, monkeypatch) -> None:
    """_capture_stage_result writes assistant text to a NEW file on disk.

    A gateway restart mid-orchestration leaves restored (now unredacted) turns in
    the window, so without redaction here those bytes would be written out.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard.chat_orchestrator import _capture_stage_result
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1-stage")
    slot.append("assistant", f"result with {SECRET}", "msg msg-a", broadcast=False)

    path = _capture_stage_result(slot, 1)
    written = pathlib.Path(path).read_text()
    assert SECRET not in written, "stage result persisted an unredacted credential"


# ── 4. restore must not broadcast replayed history ───────────────────────────


def test_restore_recent_sessions_does_not_broadcast_either(tmp_path, monkeypatch) -> None:
    """The SECOND restore loop must hold the same invariant as the first.

    This invariant is load-bearing for the meta deferral, not just a nicety.
    `_broadcast_chat_message` merges a message's whole `meta` into its websocket
    payload and applies NO redaction to it (only the `cls`-derived branch is
    sanitised, via `parse_cls_meta`). Deferring meta redaction is therefore safe
    only because no disk-loaded message is ever appended with `broadcast=True`.
    Both restore loops must be pinned or the guarantee has a hole nobody notices.

    Deliberately NOT fixed by redacting inside `_broadcast_chat_message`: the LIVE
    oauth banner is appended there with a real `oauth_url`
    (`_emit_mcp_oauth_request`), and `_redact_meta_for_role` would blank a genuine
    Google/GitHub consent URL and break the user's ability to authorize. Also
    `chat_utils` imports from `state`, so that direction would be an import cycle.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-nb2", "assistant", f"key {SECRET}")

    state2 = _make_state(tmp_path / "sessions")
    seen: list[tuple[str, dict]] = []
    # Install the observer the way get_or_create_slot does, before the restore runs.
    # (Hooking state._on_message instead does NOT observe anything — an earlier
    # revision of this test did that and passed even with broadcast=True.)
    orig = state2.get_or_create_slot

    def _spy(*a, **kw):
        slot = orig(*a, **kw)
        slot._on_message = lambda key, msg: seen.append((key, msg))
        return slot

    monkeypatch.setattr(state2, "get_or_create_slot", _spy)

    from kiro_crew.dashboard.chat_persistence import restore_recent_sessions

    restored = restore_recent_sessions(state2, window_minutes=0)
    assert restored == 1, "precondition: the session was restored"
    slot = state2._slots.get("chat-1-nb2")
    assert slot is not None and slot.messages, "precondition: messages were replayed"
    assert seen == [], f"restore_recent_sessions broadcast {len(seen)} message(s)"


def test_rehydrate_does_not_broadcast_replayed_messages(tmp_path, monkeypatch) -> None:
    """Replayed history must not be broadcast even though content is now redacted.

    _broadcast_chat_message redacts non-user *content* (parity with
    _prepare_messages, #1713) but deliberately not *meta* — so replaying history
    through it would still push unredacted meta straight to connected clients.
    This helper also runs for on-demand cold-slot rehydrates, i.e. while clients
    are connected.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-bcast", "assistant", "hello from history")

    state2 = _make_state(tmp_path / "sessions")
    seen: list[tuple[str, dict]] = []
    # Install the observer the way get_or_create_slot does, before rehydrate runs.
    orig = state2.get_or_create_slot

    def _spy(*a, **kw):
        slot = orig(*a, **kw)
        slot._on_message = lambda key, msg: seen.append((key, msg))
        return slot

    monkeypatch.setattr(state2, "get_or_create_slot", _spy)

    slot = _rehydrate_slot_from_history(state2, "chat-1-bcast")
    assert slot is not None and slot.messages, "precondition: history was restored"
    assert seen == [], f"restore broadcast {len(seen)} message(s) it should not have"


# ── 5. the content/meta split, pinned on BOTH axes ───────────────────────────
#
# This is the crux of the change and it is deliberately asymmetric:
#
#   content -> redacted ON LOAD.  ~0.4s, but ~204 read sites, so "every reader
#              remembers to redact" is not holdable. Three egress paths (side-chat
#              prompt, orchestrator stage file, title-model prompt) each leaked
#              restored content before this. One chokepoint instead.
#   meta    -> redacted AT EMIT.  ~5.5s of the ~7s load (tool_input payloads) —
#              the actual boot cost — and only 31 readers, all of which read
#              control fields (`done`, `tool_call_id`) outside the emit sites.
#
# Both directions are pinned: flipping either one silently regresses something.


def test_load_redacts_content_restoring_the_chokepoint(tmp_path, monkeypatch) -> None:
    """Content is clean in memory, so all ~204 readers are safe by construction.

    A security assertion. The emit-site redaction remains as defence in depth,
    but this is what makes a NEW reader of `m["content"]` safe without having to
    know it must redact.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    assert log is not None
    # Write through the log directly so the STORED bytes keep the secret —
    # simulating a legacy/foreign writer whose bytes predate the write-path pass.
    log.append(_history_key_for("chat-1-raw"), "assistant", f"key {SECRET}")

    state2 = _make_state(tmp_path / "sessions")
    slot = _rehydrate_slot_from_history(state2, "chat-1-raw")
    assert slot is not None and slot.messages, "precondition: history was restored"
    loaded = " ".join(m.get("content", "") for m in slot.messages)
    assert SECRET not in loaded, "content must be clean in memory (the chokepoint)"
    # …and still clean on the way out.
    assert SECRET not in json.dumps(_prepare_messages(slot.messages, False, live_child=""))


def test_load_leaves_user_content_raw(tmp_path, monkeypatch) -> None:
    """The load-time pass is gated `role != "user"`, not `not in ("user","system")`.

    Regression test for a real slip: the content pass was first written
    unconditionally, which silently redacted the user's OWN words on restore. The
    author is the only reader of their own text, so mangling it is a correctness
    bug — and no existing test caught it, because none restored a user message
    containing a credential shape.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    assert log is not None
    key = _history_key_for("chat-1-userraw")
    log.append(key, "user", f"my key is {SECRET}")
    log.append(key, "assistant", f"noted {SECRET}")

    state2 = _make_state(tmp_path / "sessions")
    slot = _rehydrate_slot_from_history(state2, "chat-1-userraw")
    assert slot is not None and len(slot.messages) >= 2
    by_role = {m.get("role"): m.get("content", "") for m in slot.messages}
    assert SECRET in by_role["user"], "user's own text was redacted on load"
    assert SECRET not in by_role["assistant"], "assistant text was not redacted"


def test_load_redacts_system_role(tmp_path, monkeypatch) -> None:
    """`system` is on the redacted side of the gate, and must stay there.

    The write path excludes `system`, so system content reaches disk raw. A gate
    of `not in ("user", "system")` would therefore leave it raw in memory too.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    assert log is not None
    log.append(_history_key_for("chat-1-sysload"), "system", f"key {SECRET}")

    state2 = _make_state(tmp_path / "sessions")
    slot = _rehydrate_slot_from_history(state2, "chat-1-sysload")
    assert slot is not None and slot.messages
    loaded = " ".join(m.get("content", "") for m in slot.messages)
    assert SECRET not in loaded, "system content left raw in memory"


def test_load_does_not_redact_meta_keeping_boot_fast(tmp_path, monkeypatch) -> None:
    """Meta is NOT scanned on load — that deferral IS the performance win.

    Not a security hole: meta payload egress is confined to the emit sites (see
    the meta tests above), and every other reader touches only control fields.
    This exists so re-adding the meta pass fails loudly instead of quietly
    handing back the ~5.5s of boot time this change removed.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    assert log is not None
    key = _history_key_for("chat-1-rawmeta")
    log.append(key, "tool", "ran a tool")
    # Put the secret in the stored meta payload, where the real cost lives.
    _rewrite_last_meta(log, key, {"tool_input": {"cmd": f"aws --key {SECRET}"}})

    state2 = _make_state(tmp_path / "sessions")
    slot = _rehydrate_slot_from_history(state2, "chat-1-rawmeta")
    assert slot is not None and slot.messages, "precondition: history was restored"
    loaded_meta = json.dumps([m.get("meta") for m in slot.messages])
    assert SECRET in loaded_meta, "meta was scanned on load — the ~5.5s cost is back"
    # …and the emit path still cleans it on the way out.
    assert SECRET not in json.dumps(_prepare_messages(slot.messages, False, live_child=""))


# ── 6. the META side: a non-emit reader that RE-EMITS the dict ────────────────
#
# The meta deferral rests on the claim that outside the emit sites, meta readers
# are harmless. The first pass at verifying that checked which FIELDS each reader
# inspects, saw only control fields (`done`, `tool_call_id`, `server_name`), and
# concluded they were safe. That was the wrong question.
# `_mark_mcp_oauth_completed` inspects only control fields for its matching logic
# — and then copies the whole stored dict into a `chat_message_update` broadcast
# that bypasses _prepare_messages. What matters is not which fields a reader
# INSPECTS but whether it RE-EMITS what it read.


def test_oauth_completion_redacts_restored_meta_before_broadcast() -> None:
    """A legacy or tampered oauth banner's meta must not reach the dashboard raw.

    Scope, stated honestly: the SAVE path already redacts meta
    (`_build_message_entry`) and `ConversationLog.append` takes no `meta`, so meta
    THIS version wrote to disk comes back clean. The reachable payload is a line
    this version did not write — a legacy line, a tampered session file, or a
    verbatim-preserved foreign byte range. This test stages that by appending the
    banner directly rather than round-tripping through disk, because a real save
    would have redacted it; the point is the re-emit, not the disk state.
    """
    from kiro_crew.dashboard.chat_runner import _mark_mcp_oauth_completed
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1-oauth")
    slot.append(
        "mcp_oauth",
        "authorize please",
        "msg",
        broadcast=False,
        meta={"server_name": "acme", "oauth_url": f"https://ex.com/cb?tok={SECRET}"},
    )

    sent: list[dict] = []

    class _FakeState:
        def broadcast_ws(self, kind, payload):  # noqa: ANN001, ANN202
            sent.append({"kind": kind, "payload": payload})

    _mark_mcp_oauth_completed(_FakeState(), slot, "acme", success=True)

    assert sent, "precondition: a completion update was broadcast"
    assert SECRET not in json.dumps(sent), "restored oauth meta broadcast unredacted"
    # Redaction must not break the flow: the banner still reaches a terminal state.
    assert sent[0]["payload"]["meta"].get("completed") is True


def test_oauth_url_corpus_survives_the_emit_path(monkeypatch) -> None:
    """Every real provider's consent URL must survive `_prepare_messages`.

    This is the test whose absence let a real regression through. The repo already
    had `test/oauth_url_corpus.py` pinning that the OAuth banner gate (now
    `security.oauth_url_contains_credential`)
    never rejects a real provider URL — but nothing routed that corpus through the
    DISPLAY gate, and this PR newly called `_redact_meta_for_role` from
    `_prepare_messages`.

    The consequence was not cosmetic. `_prepare_messages` serves the slot-detail
    endpoint, which the frontend refetches on `chat_done`, on WS reconnect, and on
    switchSlot. With the exfil heuristic still in the gate, 8 of the 9 corpus
    providers (GitHub, Google, Microsoft, Slack, …) had `oauth_url` blanked, and
    `renderMcpOAuthMessage` returns null when the URL is empty and the banner is
    neither completed nor failed — so the Authorize banner silently disappeared and
    the MCP server could never be authorized.
    """
    from oauth_url_corpus import LEGIT_OAUTH_URLS

    assert LEGIT_OAUTH_URLS, "precondition: the corpus is non-empty"
    blanked = []
    for provider, url in LEGIT_OAUTH_URLS:
        out = _prepare_messages(
            [
                {
                    "role": "mcp_oauth",
                    "content": "authorize",
                    "cls": "msg msg-info",
                    # Stamped with the child the caller resolves as LIVE, which is
                    # what a banner the user can still act on always carries:
                    # `_emit_mcp_oauth_request` is the only producer of these rows
                    # and it always stamps. An unstamped row means a dead flow and
                    # is withdrawn on purpose (issues #7654, #8149) -- pinned by
                    # the next test, so this one keeps measuring what it was
                    # written to measure: the redaction gate.
                    "meta": {
                        "server_name": "acme",
                        "oauth_url": url,
                        "child": "live-child",
                    },
                }
            ],
            False,
            live_child="live-child",
        )
        if out[0]["meta"].get("oauth_url") != url:
            blanked.append(provider)
    assert not blanked, (
        "the emit path blanked a legitimate consent URL — the Authorize banner "
        f"would silently vanish for: {blanked}"
    )


def test_a_legitimate_url_from_a_dead_child_is_withdrawn() -> None:
    """The other side of the corpus test: a real URL is no longer a live one.

    A banner carrying no child stamp was persisted by an earlier build, so the
    process that owned its loopback listener and PKCE verifier is gone. The URL is
    still a perfectly well-formed provider URL — that is exactly why the scheme and
    credential gates cannot catch it, and why the liveness gate has to (issues
    #7654, #8149).
    """
    from oauth_url_corpus import LEGIT_OAUTH_URLS

    _, url = LEGIT_OAUTH_URLS[0]
    out = _prepare_messages(
        [
            {
                "role": "mcp_oauth",
                "content": "authorize",
                "cls": "msg msg-info",
                "meta": {"server_name": "acme", "oauth_url": url},
            }
        ],
        False,
        live_child="live-child",
    )
    assert out[0]["meta"]["expired"] is True
    assert not out[0]["meta"].get("oauth_url"), "a dead flow still offered its link"


def test_oauth_url_gate_still_blocks_a_tampered_url() -> None:
    """Dropping the exfil heuristic must not open the two gates that matter."""
    from kiro_crew.dashboard.chat_utils import _redact_meta_for_role

    # 1. Non-http(s) scheme: must never reach an <a href>.
    out = _redact_meta_for_role(
        "mcp_oauth", {"oauth_url": "javascript:alert(document.cookie)"}
    )
    assert out["oauth_url"] == "", "javascript: URL survived the scheme gate"

    # 2. An embedded real credential still blanks it.
    out = _redact_meta_for_role(
        "mcp_oauth", {"oauth_url": f"https://ex.com/cb?tok={SECRET}"}
    )
    assert out["oauth_url"] == "", "credential-bearing URL survived the cred gate"


def test_oauth_completion_preserves_a_legitimate_url() -> None:
    """A real consent URL must survive the completion path too — one gate everywhere.

    An earlier revision of this test asserted the OPPOSITE: that blanking a real URL
    here was acceptable "because oauth_url is dead data once the banner is terminal".
    The premise was true for THIS call site and the conclusion was still wrong,
    because `_redact_meta_for_role` has another caller — `_prepare_messages` — where
    the banner is PRE-TERMINAL and the URL is rendered. Pinning the blanking as
    acceptable had frozen the exact behaviour that silently deleted the Authorize
    banner for 8 of the 9 providers in test/oauth_url_corpus.py.

    The lesson worth keeping: a shared helper cannot be judged safe from one call
    site. The gate now matches `security.oauth_url_contains_credential` for every caller.
    """
    from kiro_crew.dashboard.chat_runner import _mark_mcp_oauth_completed
    from kiro_crew.dashboard.state import _ChatSlot

    legit = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        "client_id=1234567890-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A5476%2Fcallback&response_type=code"
        "&scope=openid%20email%20profile&state=Ab3dEf6hIj9lMn2pQr5tUv8xYz1cD4fG"
    )
    slot = _ChatSlot("chat-1-oauthlegit")
    slot.append(
        "mcp_oauth",
        "authorize please",
        "msg",
        broadcast=False,
        meta={"server_name": "acme", "oauth_url": legit},
    )

    sent: list[dict] = []

    class _FakeState:
        def broadcast_ws(self, kind, payload):  # noqa: ANN001, ANN202
            sent.append({"kind": kind, "payload": payload})

    _mark_mcp_oauth_completed(_FakeState(), slot, "acme", success=True)

    assert sent, "precondition: a completion update was broadcast"
    meta = sent[0]["payload"]["meta"]
    assert meta.get("oauth_url") == legit, "a legitimate consent URL was blanked"
    assert meta.get("completed") is True


# ── 7. WS broadcast redaction parity with the HTTP history path (#1713) ──────
#
# _prepare_messages (HTTP history) redacts non-user content at display time;
# _broadcast_chat_message (live WS push) used to ship the same row verbatim, so
# one chat row left the backend in two different byte forms depending on which
# consumer received it. These pin the parity on both sides of the role gate.


def test_ws_broadcast_redacts_assistant_content(tmp_path, monkeypatch) -> None:
    """An assistant row carrying a credential comes out redacted on the WS path."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    sent: list[dict] = []
    monkeypatch.setattr(state, "_broadcast", lambda payload: sent.append(payload))

    state._broadcast_chat_message(
        "chat-1-wsred", {"role": "assistant", "content": f"key {SECRET}", "ts": "1"}
    )

    assert len(sent) == 1, "precondition: exactly one payload was broadcast"
    assert SECRET not in sent[0]["content"], "WS payload leaked an unredacted credential"
    assert sent[0]["role"] == "assistant"


def test_ws_broadcast_leaves_user_content_raw(tmp_path, monkeypatch) -> None:
    """A user row is left alone — the same carve-out as _prepare_messages.

    The user typed it and is the only one who sees it back; redacting it here
    would diverge from the HTTP path in the other direction.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    sent: list[dict] = []
    monkeypatch.setattr(state, "_broadcast", lambda payload: sent.append(payload))

    text = f"my note contains {SECRET}"
    state._broadcast_chat_message("chat-1-wsraw", {"role": "user", "content": text, "ts": "1"})

    assert len(sent) == 1, "precondition: exactly one payload was broadcast"
    assert sent[0]["content"] == text, "user-authored content must survive verbatim"
