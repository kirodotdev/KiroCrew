"""``session_fork``: a new session that CARRIES a transcript, opened by an agent.

Three layers, three suites. The core (``session_control.fork_session``) is
exercised against real slot objects, as ``test_session_control.py`` does for the
other verbs, because every refusal reads production attributes off the slot and a
permissive double would let a dead guard look alive. The HTTP route is asserted
through the handler with the same request double the other routes use. The tool
layer is asserted on what it forwards and reports, with ``_post`` patched, the way
``test_mcp_dashboard_session_send.py`` does.

The refactor this verb rides on -- ``chat_fork.fork_slot`` split out of the human
fork handler -- is covered by the pre-existing fork suites, which run unchanged.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, _call_tool_inner
from kiro_crew.validation import SESSION_FORK_SCHEMA, ValidationError, validate_tool_args


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Default every test to the shipped (enabled) state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state (see the
    sibling fixture in ``test_session_control.py``); a fork spends the same budget."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture(autouse=True)
def _quiet_sel(monkeypatch):
    """The fork core writes SEL rows synchronously; keep them out of the test home."""
    monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())
    monkeypatch.setattr(sc, "sel", lambda: MagicMock())


def _key(slot) -> str:
    return slot_history_key(slot)


def _seed(state, name: str, turns: int = 2, **kwargs):
    """A live slot with ``turns`` user/assistant pairs persisted to disk, so the
    fork core reads a settled transcript (nothing dirty, boundary in place)."""
    slot = state.get_or_create_slot(name, **kwargs)
    for i in range(turns):
        slot.append("user", f"question {i}", "msg msg-u")
        slot.append("assistant", f"answer {i}", "msg msg-a")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return slot


def _folder(state, fid: str, name: str) -> str:
    state._folders.append({"id": fid, "name": name, "parent_id": ""})
    return fid


def _fork(state, caller, **kwargs):
    return asyncio.run(sc.fork_session(state, caller_session_key=_key(caller), **kwargs))


def _contents(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") in ("user", "assistant")]


# ── the core: what a fork carries ───────────────────────────────────────────────


def test_the_default_source_is_the_caller_itself(tmp_path):
    """The case the verb exists for: an agent splitting its OWN investigation."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")

    result = _fork(state, caller)

    child = state.get_slot(result["target"])
    assert child is not None and child is not caller
    assert result["source"] == caller.key
    assert result["messages"] == 4
    assert _contents(child) == _contents(caller), "the child carries the whole transcript"
    assert child.forked_from == sc.effective_session_key(caller)
    assert child.agent == caller.agent and child.workspace == caller.workspace
    assert child.title.endswith("Fork of Untitled") and child._titled


def test_a_child_starts_idle_and_is_addressable_by_its_creator(tmp_path):
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")

    result = _fork(state, caller)
    child = state.get_slot(result["target"])

    assert not child.running and not child._queue
    assert child._created_by == caller.key, "ownership is what lets the other verbs reach it"
    # `session_send` / `session_read_message` / `session_close` all resolve
    # through this gate, so the child must pass it for the caller that made it.
    assert (
        sc.authorize_target(
            state, caller_session_key=_key(caller), target=child.key, operation="read"
        )
        is child
    )


def test_an_explicit_fork_point_carries_the_head_only(tmp_path):
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1", turns=3)

    result = _fork(state, caller, at_message_index=2)

    child = state.get_slot(result["target"])
    assert result["messages"] == 3
    assert _contents(child) == ["question 0", "answer 0", "question 1"]


def test_a_fork_point_past_the_transcript_is_refused_with_the_forks_own_code(tmp_path):
    """A refusal minted inside ``chat_fork`` arrives as a ``SessionControlError``
    carrying that module's code, not as an unlabelled 500."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    before = set(state._slots)

    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, at_message_index=99)

    assert exc.value.code == "value_out_of_range"
    assert exc.value.status == 400
    assert set(state._slots) == before, "a refused fork allocates no child"


def test_a_negative_fork_point_is_refused_before_anything_is_read(tmp_path):
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, at_message_index=-1)
    assert exc.value.code == "invalid_field_type"


def test_title_and_folder_are_applied_and_persisted(tmp_path):
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    folder_id = _folder(state, "fold00000001", "Gamma failures")

    result = _fork(state, caller, title="cluster 3: duid", folder_id=folder_id)

    child = state.get_slot(result["target"])
    assert child.title == "cluster 3: duid" and child._titled
    assert child.folder_id == folder_id
    assert result["title"] == "cluster 3: duid" and result["folder_id"] == folder_id
    # Title, folder and creator are stamped on the child BEFORE the fork's own
    # save, so that one write is what makes them survive a restart.
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("title") == "cluster 3: duid"
    assert written.get("folder_id") == folder_id
    assert written.get("created_by") == caller.key


def test_attribution_is_in_the_birth_save_not_a_second_write(tmp_path, monkeypatch):
    """No window in which a persisted, broadcast child exists unattributed.

    ``fork_slot`` saves the child once and then broadcasts it. If attribution
    were merged afterwards, a failure of that merge would leave a valid,
    visible, ownerless child behind and a retry would duplicate it. So the
    creator, title and folder must already be on the slot when the birth save
    runs, and ``update_metadata`` must not be called at all on the fork path.
    """
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    folder_id = _folder(state, "fold00000009", "Birth")
    seen: dict[str, Any] = {}
    real_save = chat_fork.save_slot_off_loop

    async def _spy_save(st, slot, *a, **kw):
        seen["created_by"] = getattr(slot, "_created_by", "")
        seen["title"] = slot.title
        seen["folder_id"] = slot.folder_id
        seen["lineage"] = getattr(slot, "_lineage_minted", False)
        return await real_save(st, slot, *a, **kw)

    monkeypatch.setattr(chat_fork, "save_slot_off_loop", _spy_save)
    merges: list[Any] = []
    monkeypatch.setattr(
        state.conversation_log,
        "update_metadata",
        lambda *a, **kw: merges.append(a),
    )

    result = _fork(state, caller, title="birth", folder_id=folder_id)

    assert seen == {
        "created_by": caller.key,
        "title": "birth",
        "folder_id": folder_id,
        "lineage": True,
    }, "attribution must be on the child when its birth save runs"
    assert merges == [], "the fork path performs no second metadata write"
    child = state.get_slot(result["target"])
    assert state.conversation_log.get_metadata(slot_history_key(child)).get("created_by") == (
        caller.key
    )


def test_a_source_moved_to_another_workspace_mid_fork_is_refused(tmp_path, monkeypatch):
    """The containment check is pinned to the workspace it ran against.

    ``authorize_target`` admits a peer in the caller's workspace; the child is
    then born in ``source.workspace`` read live. A concurrent owner switch of
    the source between those two points must refuse the copy, not carry the
    transcript into a workspace the check never saw.
    """
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    peer = _seed(state, "chat-2")
    peer._created_by = caller.key
    real_bind = chat_fork._bind_fork_execution

    def _move_then_bind(*a, **kw):
        # Runs inside fork_slot, after the source was authorized and frozen.
        peer.workspace = "elsewhere"
        return real_bind(*a, **kw)

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", _move_then_bind)
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, source=peer.key)
    # The containment re-check answers first, with read's own code; the frozen
    # fork identity (which now carries the workspace) is the backstop behind it.
    assert exc.value.code == "workspace_mismatch"
    assert set(state._slots) == before, "a refused fork leaves no child behind"
    assert chat_fork.ForkSource.__dataclass_fields__["identity"].type.count("str") == 6


def test_the_frozen_fork_identity_refuses_a_self_source_that_moved_workspace(tmp_path, monkeypatch):
    """No `authorize_target` runs for the caller's own transcript, so the frozen
    identity alone must catch a workspace move there."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    real_bind = chat_fork._bind_fork_execution

    def _move_then_bind(*a, **kw):
        caller.workspace = "elsewhere"
        return real_bind(*a, **kw)

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", _move_then_bind)
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller)
    assert exc.value.code == "store_unavailable"
    assert set(state._slots) == before


def test_without_a_folder_the_child_inherits_the_sources_folder(tmp_path):
    """The human fork files the child next to its parent; this path keeps that."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    folder_id = _folder(state, "fold00000002", "Investigation")
    caller.folder_id = folder_id

    result = _fork(state, caller)

    assert state.get_slot(result["target"]).folder_id == folder_id


def test_an_unknown_folder_refuses_the_whole_fork(tmp_path):
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, folder_id="fld-does-not-exist")
    assert exc.value.code == "folder_not_found"
    assert set(state._slots) == before


def test_the_child_takes_the_callers_posture_and_nothing_narrower(tmp_path):
    """Same two fields ``create_session`` carries, same two it withholds."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    caller._trust = True
    caller._trust_reads = True
    caller._trusted_patterns = {"npm test"}
    caller._trust_scope = "scope-1"

    child = state.get_slot(_fork(state, caller)["target"])

    assert child._trust is True and child._trust_reads is True
    assert not getattr(child, "_trusted_patterns", set())
    assert not getattr(child, "_trust_scope", "")


# ── the core: naming another session ────────────────────────────────────────────


def test_a_peer_can_be_forked_by_key(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    peer = _seed(state, "chat-2", turns=1)

    result = _fork(state, caller, source="chat-2")

    child = state.get_slot(result["target"])
    assert result["source"] == peer.key
    assert _contents(child) == ["question 0", "answer 0"]
    assert child._created_by == caller.key, "the CALLER owns the child, not the peer"


def test_a_peer_can_be_forked_by_its_unique_title(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    peer = _seed(state, "chat-2", turns=1)
    peer.title = "Gamma ToD review"

    result = _fork(state, caller, source="gamma tod review")

    assert result["source"] == peer.key


def test_naming_yourself_is_the_default_case_not_a_self_target_refusal(tmp_path):
    """``authorize_target`` refuses ``self_target`` for stop/send/read, and rightly;
    copying your OWN transcript crosses no boundary, so the same key spelled out
    must behave exactly like omitting ``source``."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    result = _fork(state, caller, source=caller.key)
    assert result["source"] == caller.key


def test_an_ambiguous_title_is_refused_not_guessed(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    for name in ("chat-2", "chat-3"):
        _seed(state, name, turns=1).title = "Shared Title"
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, source="Shared Title")
    assert exc.value.code == "ambiguous_target"
    assert exc.value.status == 409


def test_an_unknown_source_is_404(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, source="chat-nope")
    assert exc.value.code == "target_not_found"
    assert exc.value.status == 404


@pytest.mark.parametrize(
    "shape, code",
    [
        ("incognito", "ephemeral_target"),
        ("workspace", "workspace_mismatch"),
        ("linked", "linked_session_target"),
    ],
)
def test_a_source_the_caller_may_not_read_may_not_be_forked(tmp_path, shape, code):
    """Forking copies the transcript, so it IS a read: the refusal is the one
    ``read_messages`` gives for the same target, code for code."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    peer = _seed(state, "chat-2", turns=1)
    if shape == "incognito":
        peer.memory_mode = "incognito"
    elif shape == "workspace":
        peer.workspace = "elsewhere"
    else:
        peer.linked_session_key = "channel:1786300000.000100"

    with pytest.raises(sc.SessionControlError) as fork_exc:
        _fork(state, caller, source="chat-2")
    with pytest.raises(sc.SessionControlError) as read_exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert fork_exc.value.code == code
    assert fork_exc.value.code == read_exc.value.code
    assert fork_exc.value.status == read_exc.value.status


def test_a_source_that_gains_a_channel_mirror_mid_fork_is_refused(tmp_path, monkeypatch):
    """Containment is re-asserted at the copy, not only at admission.

    ``authorize_target`` admitted the peer before ``fork_slot`` suspended for
    the transcript read and the memory bind. A channel link attached in that
    window would otherwise be copied into an ordinary child, outside the fence
    the link exists to keep it behind.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    peer = _seed(state, "chat-2", turns=1)
    real_bind = chat_fork._bind_fork_execution

    def _link_then_bind(*a, **kw):
        peer.linked_session_key = "channel:1786300000.000100"
        return real_bind(*a, **kw)

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", _link_then_bind)
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, source="chat-2")
    assert exc.value.code == "linked_session_target"
    assert set(state._slots) == before, "the empty child is withdrawn"


def test_a_folder_deleted_mid_fork_is_refused_not_dangled(tmp_path, monkeypatch):
    """The folder was confirmed before ``fork_slot`` suspended; a delete landing
    in that window must refuse the fork rather than file the child under an id
    that is absent from the committed folder list."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    folder_id = _folder(state, "fold0000gone", "Ephemeral")
    real_bind = chat_fork._bind_fork_execution

    def _delete_then_bind(*a, **kw):
        state._folders[:] = [f for f in state._folders if f.get("id") != folder_id]
        return real_bind(*a, **kw)

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", _delete_then_bind)
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller, folder_id=folder_id)
    assert exc.value.code == "folder_not_found"
    assert set(state._slots) == before


def test_a_fenced_caller_can_fork_only_what_it_created(tmp_path):
    """A cron caller reaches the sessions it dispatched and nothing else; that
    fence bounds the fork exactly as it bounds a read."""
    state = _make_state(tmp_path)
    cron = state.get_or_create_slot("cron-abc123")
    # Ownership is read from the JOB, and an unfindable one fails closed, so the
    # fence is only reached once the registry can produce a non-app owner.
    state.crons.list_jobs.return_value = [SimpleNamespace(id="abc123", created_by="U0123ABCD")]
    _seed(state, "chat-2", turns=1)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, cron, source="chat-2")
    assert exc.value.code == "not_creator"


# ── the core: who may fork at all ───────────────────────────────────────────────


def test_fork_refuses_the_caller_classes_create_refuses(tmp_path):
    """A fork manufactures a session the caller owns, so an ineligible CREATOR is
    refused with the same codes ``create_session`` gives -- even for its own
    transcript, where no target-side check would fire."""
    state = _make_state(tmp_path)

    app_caller = _seed(state, "chat-app")
    app_caller._app = "some-app"
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, app_caller)
    assert exc.value.code == "app_scoped_caller"

    ghost = _seed(state, "chat-ghost")
    ghost.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, ghost)
    assert exc.value.code == "ephemeral_caller"

    linked = _seed(state, "chat-linked")
    linked.linked_session_key = "channel:1786300000.000100"
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, linked)
    assert exc.value.code == "linked_session_caller"


def test_the_config_switch_refuses_a_fork(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller)
    assert exc.value.code == "session_control_disabled"


def test_the_slot_cap_refuses_a_fork_before_the_copy(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", state.live_slot_count())
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller)
    assert exc.value.code == "slot_cap_reached"
    assert exc.value.status == 429
    assert set(state._slots) == before


def test_the_ceilings_are_re_read_at_the_mint(tmp_path, monkeypatch):
    """Two forks in flight both pass the entry check; the one that mints second
    must see the first's child in the count. Simulated by filling the cap while
    ``fork_slot`` is suspended in the transcript read, before the mint."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    real_resolve = chat_fork.resolve_fork_source

    async def _fill_then_resolve(*a, **kw):
        source = await real_resolve(*a, **kw)
        # Entry checks have passed; the cap now closes under the coroutine.
        monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", state.live_slot_count())
        return source

    monkeypatch.setattr(sc, "resolve_fork_source", _fill_then_resolve)
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller)
    assert exc.value.code == "slot_cap_reached"
    assert set(state._slots) == before, "no child is minted over the ceiling"


def test_trust_revoked_during_preparation_is_not_inherited(tmp_path, monkeypatch):
    """The posture is read when the child is stamped, not when the fork was
    admitted: an operator switching the caller to normal mode while the
    transcript is read off disk must not see the child born auto-approving."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    caller._trust = True
    caller._trust_reads = True
    real_bind = chat_fork._bind_fork_execution

    def _revoke_then_bind(*a, **kw):
        caller._trust = False
        caller._trust_reads = False
        return real_bind(*a, **kw)

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", _revoke_then_bind)
    child = state.get_slot(_fork(state, caller)["target"])
    assert child._trust is False and child._trust_reads is False


def test_a_fork_spends_the_session_create_budget(tmp_path, monkeypatch):
    """A fork is a session the caller manufactured, whatever it starts with, so
    it draws on the same per-caller window a create does."""
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    monkeypatch.setattr(sc, "allow_create", lambda verb, key: False)
    with pytest.raises(sc.SessionControlError) as exc:
        _fork(state, caller)
    assert exc.value.code == "create_rate_limited"


def test_an_unidentifiable_caller_is_refused(tmp_path):
    state = _make_state(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.fork_session(state, caller_session_key=""))
    assert exc.value.code == "caller_unidentified"


def test_the_fork_is_audited_under_its_own_operation(tmp_path, monkeypatch):
    """The human Fork button audits ``chat.slot_fork``; an agent's fork must be
    told apart from it in the SEL, on both the fork core's rows and this
    module's own completion row."""
    core_ops: list[str] = []
    core_sel = MagicMock()
    core_sel.log_api_access.side_effect = lambda **kw: core_ops.append(kw["operation"])
    monkeypatch.setattr(chat_fork, "sel", lambda: core_sel)
    audited: list[dict] = []
    monkeypatch.setattr(
        sc, "_audit", lambda **kw: audited.append(kw)
    )  # the off-loop SEL write, captured at the call
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")

    result = _fork(state, caller)

    assert core_ops and set(core_ops) == {"session_control.fork"}
    assert audited[-1]["operation"] == "fork"
    assert audited[-1]["slot_key"] == result["target"]
    assert audited[-1]["detail"]["source"] == caller.key


# ── the refactor: the human route is byte-for-byte the same contract ────────────


def test_the_human_fork_route_still_reports_its_full_body(tmp_path, monkeypatch):
    """The wrapper builds the response the frontend has always read: the split
    must not drop a field the tab renders from (``memory_mode``, ``direction``)."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _seed(state, "forkable", turns=1)

    async def _go():
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/forkable/fork", json={"prompt": "go on"})
            return resp.status, await resp.json()

    status, body = asyncio.run(_go())
    assert status == 200
    assert set(body) == {
        "ok",
        "key",
        "title",
        "messages",
        "prompt",
        "folder_id",
        "direction",
        "memory_mode",
    }
    assert body["prompt"] == "go on" and body["messages"] == 2 and body["direction"] == "head"


def test_only_the_human_fork_opts_into_the_session_count(tmp_path, monkeypatch):
    """``count_user_session`` marks a human request-layer session for the pulse
    survey; an agent's fork is not one. Pinned behaviourally here because the
    structural sweep in ``test_session_pulse_session_count`` can only see the
    literal at the one call site the two paths now share."""
    increments: list[int] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.state.increment_user_session_count_off_loop",
        lambda: increments.append(1),
    )
    state = _make_state(tmp_path)
    caller = _seed(state, "chat-1")
    _fork(state, caller)
    assert increments == []


# ── the route ───────────────────────────────────────────────────────────────────


class TestTheRoute:
    def _request(self, tmp_path, *, internal: bool, body: dict):
        state = _make_state(tmp_path)
        caller = _seed(state, "chat-1")
        request = MagicMock()
        request.app = {"state": state}
        request.path = "/api/session-control/fork"
        request.method = "POST"
        request.headers = {"X-Session-Key": _key(caller)}
        request.get = lambda key, default=None: (
            True if (key in ("internal_auth", "peer_verified") and internal) else default
        )

        async def _json():
            return body

        request.json = _json
        return state, caller, request

    @staticmethod
    def _body(response) -> dict:
        return json.loads(response.body.decode())

    def test_without_the_secret_it_is_forbidden(self, tmp_path):
        _, _, req = self._request(tmp_path, internal=False, body={})
        resp = asyncio.run(handlers_sc.api_session_control_fork(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_it_returns_a_session_carrying_the_transcript(self, tmp_path):
        state, caller, req = self._request(tmp_path, internal=True, body={"title": "split"})
        resp = asyncio.run(handlers_sc.api_session_control_fork(req))
        assert resp.status == 200
        body = self._body(resp)
        assert body["ok"] is True and body["source"] == caller.key
        child = state.get_slot(body["target"])
        assert child is not None and len(_contents(child)) == 4
        assert child.title == "split"

    def test_a_boolean_fork_point_is_refused_not_read_as_one(self, tmp_path):
        _, _, req = self._request(tmp_path, internal=True, body={"at_message_index": True})
        resp = asyncio.run(handlers_sc.api_session_control_fork(req))
        assert resp.status == 400
        assert self._body(resp)["code"] == "invalid_field_type"

    def test_a_non_string_source_is_refused(self, tmp_path):
        _, _, req = self._request(tmp_path, internal=True, body={"source": 7})
        resp = asyncio.run(handlers_sc.api_session_control_fork(req))
        assert resp.status == 400
        assert self._body(resp)["code"] == "invalid_field_type"

    def test_the_route_is_registered_and_strict_internal(self):
        """The create route once shipped dead for exactly this omission."""
        from kiro_crew.dashboard import server as server_mod

        source = open(server_mod.__file__, encoding="utf-8").read()
        assert '_deferred("session_control", "api_session_control_fork")' in source
        assert '"/api/session-control/fork",' in source.split("_STRICT_INTERNAL_API_PATHS")[1]


# ── the tool layer ──────────────────────────────────────────────────────────────


@pytest.fixture
def _caller():
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


def _tool(args: dict, resp: dict):
    with patch("kiro_crew.mcp_dashboard._post", return_value=resp) as mock_post:
        out = _call_tool_inner("session_fork", args)
    return out, mock_post


_OK = {
    "ok": True,
    "target": "chat-9",
    "title": "↳ Fork of Gamma review",
    "source": "chat-1",
    "messages": 12,
    "folder_id": None,
}


class TestTheTool:
    def test_it_is_session_control_so_it_is_identity_gated(self):
        assert "session_fork" in SESSION_CONTROL_TOOLS

    def test_a_subagent_without_a_strict_key_is_refused_before_any_request(self):
        with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""):
            with patch("kiro_crew.mcp_dashboard._post") as mock_post:
                out = _call_tool_inner("session_fork", {})
        assert out.startswith("Error:") and "identified" in out
        mock_post.assert_not_called()

    def test_an_empty_call_forks_the_caller_under_its_own_key(self, _caller):
        _, mock_post = _tool({}, _OK)
        path, body = mock_post.call_args.args
        assert path == "/api/session-control/fork"
        assert body == {"source": "", "title": ""}
        assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-1-100"

    def test_source_title_and_fork_point_are_forwarded(self, _caller):
        _, mock_post = _tool({"source": "chat-2", "title": "cluster 3", "at_message_index": 7}, _OK)
        assert mock_post.call_args.args[1] == {
            "source": "chat-2",
            "title": "cluster 3",
            "at_message_index": 7,
        }

    def test_the_report_names_source_child_and_what_was_carried(self, _caller):
        out, _ = _tool({}, _OK)
        assert "chat-1" in out and "chat-9" in out and "12 message(s)" in out
        assert "idle" in out and "session_send" in out

    def test_a_refusal_is_reported_with_the_apis_words(self, _caller):
        out, _ = _tool({}, {"error": "slot cap reached (500)", "code": "slot_cap_reached"})
        assert out.startswith("Error: could not fork the session: slot cap reached")

    def test_a_folder_path_goes_through_the_tree_shaping_gate(self, _caller):
        """Filing at creation creates missing segments, which is tree shaping, so
        it must run the same gate ``session_create`` runs -- not a second path."""
        with (
            patch(
                "kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable",
                return_value=("", "", "Error: gate refused"),
            ) as gate,
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("session_fork", {"folder": "Gamma/cluster 3"})
        assert out == "Error: gate refused"
        gate.assert_called_once()
        mock_post.assert_not_called()


class TestTheSchema:
    def test_nothing_is_required(self):
        assert validate_tool_args({}, SESSION_FORK_SCHEMA) == {
            "source": "",
            "title": "",
            "folder": "",
        }

    def test_a_boolean_fork_point_is_refused(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"at_message_index": True}, SESSION_FORK_SCHEMA)

    def test_a_negative_fork_point_is_refused(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"at_message_index": -1}, SESSION_FORK_SCHEMA)

    def test_there_is_no_agent_model_or_mode_override(self):
        """The child inherits the source's, as the human fork does; an override
        here would be a memory-boundary crossing the fork core cannot honour."""
        for field in ("agent", "model", "mode", "direction"):
            with pytest.raises(ValidationError):
                validate_tool_args({field: "x"}, SESSION_FORK_SCHEMA)
