"""``session_summary``: a peer session's cached intent summary over the dashboard MCP.

Three things are pinned. The verb is authorized by the SAME gate as
``session_read_message`` (``authorize_target``, operation ``read``), so the
digest can never be reachable where the transcript is not. It is a cache read:
nothing on its path generates a summary or calls a model. And the route is
wired as a strict internal-secret path, without which the tool is unreachable
in production while every handler test still passes.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state, move_transcript_past

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_summary
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner, _render_session_summary

_VERIFIED = "dashboard:chat-verified"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _pin_summaries(monkeypatch, enabled: bool) -> None:
    def _load():
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=enabled)
        return cfg

    monkeypatch.setattr(sc.KiroCrewConfig, "load", staticmethod(_load))


def _payload(title: str = "get the build green") -> dict:
    return {
        "intents": [
            {
                "title": title,
                "ranges": [[1, 2]],
                "status": "active",
                "verified": None,
                "state": "working",
                "last_touched_turn": 2,
                "progress": ["3 of 5 failing tests fixed"],
                "next_steps": [{"what": "fix the snapshot tests", "why": "", "expect": ""}],
            }
        ],
        "constraints": ["restart the worker after a config change"],
        "generated_at": 1_760_000_000.0,
        "user_turns": 2,
        "last_activity": "2026-09-29T21:40:12+00:00",
    }


def _pair(tmp_path, *, with_summary: bool = True, **target_kwargs):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2", **target_kwargs)
    hkey = slot_history_key(target)
    log = state.conversation_log
    log.append(hkey, "user", "hello")
    if with_summary:
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))
    return state, caller, target


def _read(state, caller, target: str = "chat-2") -> dict:
    return asyncio.run(
        sc.read_summary(state, caller_session_key=slot_history_key(caller), target=target)
    )


# ── Authorization is session_read_message's ──────────────────────────────────


def test_the_gate_is_authorize_target(tmp_path, monkeypatch):
    """The read gate itself; ``operation`` only labels the audit line."""
    _pin_summaries(monkeypatch, True)
    state, caller, target = _pair(tmp_path)
    seen: list[dict] = []
    real = sc.authorize_target

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _spy)
    _read(state, caller)
    assert [kw["operation"] for kw in seen] == ["summary", "summary"]
    assert [kw.get("skip_enabled_check", False) for kw in seen] == [False, True]
    assert seen[0]["caller_session_key"] == slot_history_key(caller)
    assert seen[0]["target"] == "chat-2"


def test_a_refusal_from_the_gate_reads_nothing(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)
    read = MagicMock()
    monkeypatch.setattr(chat_summary, "read_cached_intent_summary", read)

    def _deny(*_a, **_kw):
        raise sc.SessionControlError("no", status=403, code="not_creator")

    monkeypatch.setattr(sc, "authorize_target", _deny)
    with pytest.raises(sc.SessionControlError):
        _read(state, caller)
    read.assert_not_called()


def test_an_incognito_target_is_refused_like_a_read(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path, with_summary=False, memory_mode="incognito")
    with pytest.raises(sc.SessionControlError) as exc:
        _read(state, caller)
    assert "incognito" in exc.value.message


# ── It serves the cache and nothing more ─────────────────────────────────────


def test_serves_the_cached_summary_with_liveness(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)
    out = _read(state, caller)
    assert out["enabled"] is True
    assert out["stale"] is False
    assert out["running"] is False
    assert out["intents"][0]["title"] == "get the build green"
    assert out["constraints"] == ["restart the worker after a config change"]
    assert out["generated_at"] == 1_760_000_000.0


def test_a_summary_older_than_the_transcript_is_flagged_stale(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, caller, target = _pair(tmp_path)
    hkey = slot_history_key(target)
    log = state.conversation_log
    sig = log.session_mtime(hkey)
    log.append(hkey, "user", "a newer turn")
    move_transcript_past(log, hkey, sig)
    assert _read(state, caller)["stale"] is True


def test_switched_off_serves_no_sidecar(tmp_path, monkeypatch):
    """A sidecar written while the feature was on stops being served when it is off."""
    _pin_summaries(monkeypatch, False)
    state, caller, _target = _pair(tmp_path)
    out = _read(state, caller)
    assert out["enabled"] is False
    assert out["intents"] == []
    assert out["generated_at"] is None


def test_a_transcript_that_withholds_derivation_hides_its_sidecar(tmp_path, monkeypatch):
    """Same derivation gate as the panel GET: an on-disk restricted mode wins."""
    _pin_summaries(monkeypatch, True)
    state, caller, target = _pair(tmp_path)
    log = state.conversation_log
    asyncio.run(
        asyncio.to_thread(
            log.update_metadata, slot_history_key(target), {"memory_mode": "temporary"}
        )
    )
    assert _read(state, caller)["intents"] == []


def test_the_read_never_generates(tmp_path, monkeypatch):
    """Mutation guard: routing through ``generate_session_summary`` trips it."""
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path, with_summary=False)
    called: list[object] = []

    async def _fake(*a, **kw):
        called.append(a)
        return ""

    monkeypatch.setattr(chat_summary, "run_bg_oneliner", _fake)
    monkeypatch.setattr(chat_summary, "generate_session_summary", _fake)
    out = _read(state, caller)
    assert out["intents"] == []
    assert called == []


def test_the_payload_is_redacted_on_the_way_out(tmp_path, monkeypatch):
    """A scrubber rule added after the sidecar was written still covers it."""
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"

    async def _cached(_log, _slot):
        return {"intents": [{"title": f"key {secret}"}], "constraints": []}, False

    monkeypatch.setattr(chat_summary, "read_cached_intent_summary", _cached)
    out = _read(state, caller)
    assert secret not in json.dumps(out)


# ── The route ────────────────────────────────────────────────────────────────


def _request(state, *, internal: bool):
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/summary"
    request.method = "GET"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.query = {"target": "chat-2"}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )
    return request


def test_the_route_refuses_a_cookie_only_caller(tmp_path):
    state, _caller, _target = _pair(tmp_path)
    resp = asyncio.run(handlers_sc.api_session_control_summary(_request(state, internal=False)))
    assert resp.status == 403


def test_the_route_serves_an_internal_caller(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, _caller, _target = _pair(tmp_path)
    resp = asyncio.run(handlers_sc.api_session_control_summary(_request(state, internal=True)))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["target"] == "chat-2"
    assert body["intents"][0]["title"] == "get the build green"


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/summary" in _STRICT_INTERNAL_API_PATHS


# ── The MCP tool ─────────────────────────────────────────────────────────────


def test_the_tool_sends_the_verified_key_to_the_summary_route():
    body = {
        "target": "chat-2",
        "title": "w",
        "running": True,
        "enabled": True,
        "intents": [
            {
                "title": "get the build green",
                "state": "in-progress",
                "progress": ["3 of 5 failing tests fixed"],
                "next_steps": ["fix the snapshot tests"],
            }
        ],
    }
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._get", return_value=body) as get,
    ):
        out = _call_tool_inner("session_summary", {"target": "chat-2"})
    path, key = get.call_args.args
    assert path == "/api/session-control/summary?target=chat-2"
    assert key == _VERIFIED
    assert "still working" in out
    assert "[in-progress] get the build green" in out
    assert "next: fix the snapshot tests" in out


def test_the_tool_refuses_an_unverifiable_caller():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_dashboard._get") as get,
    ):
        out = _call_tool_inner("session_summary", {"target": "chat-2"})
    assert out.startswith("Error")
    get.assert_not_called()


def test_the_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._get", return_value={"error": "not yours"}),
    ):
        out = _call_tool_inner("session_summary", {"target": "chat-2"})
    assert out == "Error: could not read that session's summary: not yours"


def test_render_says_why_there_is_nothing():
    off = _render_session_summary({"target": "t", "enabled": False})
    assert "switched off" in off and "session_read_message" in off
    empty = _render_session_summary({"target": "t", "enabled": True, "intents": []})
    assert "No summary" in empty and "session_read_message" in empty


def test_render_flags_stale_and_reports_what_the_route_left_out():
    out = _render_session_summary(
        {
            "target": "t",
            "enabled": True,
            "stale": True,
            "intents": [
                {"title": "g", "status": "active", "progress_omitted": 3, "next_steps_omitted": 2}
            ],
            "intents_omitted": 4,
            "constraints": ["n"],
            "constraints_omitted": 1,
        }
    )
    assert "STALE" in out
    assert "3 earlier progress item(s) not shown" in out
    assert "2 more next step(s) not shown" in out
    assert "4 older intent(s) not shown" in out
    assert "1 more project note(s) not shown" in out


def test_render_prints_the_epoch_as_utc_iso():
    """The sidecar stores ``time.time()``; the agent reads a timestamp, not a float."""
    out = _render_session_summary(
        {"target": "t", "enabled": True, "generated_at": 0.0, "intents": [{"title": "g"}]}
    )
    assert "Summary written 1970-01-01T00:00:00Z." in out
    odd = _render_session_summary(
        {"target": "t", "enabled": True, "generated_at": "soon", "intents": [{"title": "g"}]}
    )
    assert "Summary written at an unknown time." in odd


# ── The response is bounded at the route ─────────────────────────────────────


def test_the_response_bounds_counts_and_strings(tmp_path, monkeypatch):
    """Counts and string lengths are cut in the response itself, with omission counts.

    Mutation guard: returning the stored payload verbatim fails every assertion.
    """
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)
    long = "x" * 5000
    many = [
        {
            "title": long,
            "status": "active",
            "progress": [f"p{n}" for n in range(8)],
            "next_steps": [{"what": f"s{n}", "why": long, "expect": long} for n in range(7)],
            "ranges": [[1, 2]],
        }
        for _ in range(sc.MAX_SUMMARY_INTENTS + 3)
    ]

    async def _cached(_log, _slot):
        return {"intents": many, "constraints": [long] * 12, "user_turns": 9}, False

    monkeypatch.setattr(chat_summary, "read_cached_intent_summary", _cached)
    out = _read(state, caller)
    assert len(out["intents"]) == sc.MAX_SUMMARY_INTENTS
    assert out["intents_omitted"] == 3
    first = out["intents"][0]
    assert first["progress"] == ["p3", "p4", "p5", "p6", "p7"]
    assert first["progress_omitted"] == 3
    assert first["next_steps"] == ["s0", "s1", "s2", "s3", "s4"]
    assert first["next_steps_omitted"] == 2
    assert len(first["title"]) <= sc.MAX_SUMMARY_CHARS + len(" …[truncated]")
    assert first["title"].endswith("…[truncated]")
    assert set(first) == {
        "title",
        "state",
        "progress",
        "progress_omitted",
        "next_steps",
        "next_steps_omitted",
    }
    assert len(out["constraints"]) == sc.MAX_SUMMARY_NOTES
    assert out["constraints_omitted"] == 2
    assert "user_turns" not in out and "last_activity" not in out
    assert len(json.dumps(out)) < 20_000


def test_a_completed_unverified_intent_reads_needs_you(tmp_path, monkeypatch):
    """The panel's derived state, not the raw ``status`` axis.

    Mutation guard: emitting ``status`` shows ``completed`` and hides the
    unverified finish.
    """
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)

    async def _cached(_log, _slot):
        intents = [
            {"title": "a", "status": "completed", "verified": False},
            {"title": "b", "status": "completed", "verified": True},
            {"title": "c", "status": "active", "state": "in-progress"},
        ]
        return {"intents": intents, "constraints": []}, False

    monkeypatch.setattr(chat_summary, "read_cached_intent_summary", _cached)
    out = _read(state, caller)
    assert [i["state"] for i in out["intents"]] == ["needs-you", "done", "in-progress"]


def test_authorization_is_rechecked_after_the_reads(tmp_path, monkeypatch):
    """A slot swapped while the sidecar is read is refused, not answered.

    Mutation guard: dropping the post-read ``authorize_target`` returns the digest.
    """
    _pin_summaries(monkeypatch, True)
    state, caller, target = _pair(tmp_path)
    real = sc.authorize_target
    calls: list[bool] = []

    def _gate(*args, **kwargs):
        calls.append(kwargs.get("skip_enabled_check", False))
        slot = real(*args, **kwargs)
        return slot if len(calls) == 1 else MagicMock()

    monkeypatch.setattr(sc, "authorize_target", _gate)
    with pytest.raises(sc.SessionControlError) as exc:
        _read(state, caller)
    assert exc.value.code == "target_replaced"
    assert calls == [False, True]


def test_a_refusal_on_the_recheck_propagates(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)
    real = sc.authorize_target
    calls: list[int] = []

    def _gate(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise sc.SessionControlError("linked now", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _gate)
    with pytest.raises(sc.SessionControlError) as exc:
        _read(state, caller)
    assert exc.value.code == "linked_session_target"


# ── The read direction of the channel mark, as for session_read_message ──────


@pytest.mark.parametrize("taint", ["channel_turn_seen", "channel_origin"])
def test_reading_a_marked_conversations_summary_marks_the_reader(tmp_path, monkeypatch, taint):
    """The summary's intents, constraints and title come from the target's
    transcript, so a reader of a marked conversation is marked too -- the
    rule ``read_messages`` applies -- or a clean slot could launder channel
    text into a later ``chat_folder_steering_set``."""
    _pin_summaries(monkeypatch, True)
    state, caller, target = _pair(tmp_path)
    if taint == "channel_turn_seen":
        target._channel_turn_seen = True
    else:
        target.channel_origin = True

    out = _read(state, caller)

    assert out["intents"]
    assert caller._channel_turn_seen is True
    assert caller.to_dict()["channel_turn_seen"] is True


def test_reading_a_clean_conversations_summary_leaves_the_reader_clean(tmp_path, monkeypatch):
    _pin_summaries(monkeypatch, True)
    state, caller, _target = _pair(tmp_path)

    _read(state, caller)

    assert caller._channel_turn_seen is False


def test_the_summary_handler_runs_the_off_loop_mirror_mark():
    """The async summary endpoint completes the read direction for a target
    reached only through a session-store mirror, as the read endpoint does."""
    import inspect

    assert "mark_reader_for_mirrored_target" in inspect.getsource(
        handlers_sc.api_session_control_summary
    )
