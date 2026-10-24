"""Layer B, the kiro-cli context window a bundle carries so an imported session resumes.

Pins when the sending side reads it (in full, never while a turn is in flight), the v1 or
Layer-B-free downgrade for an older peer, validation of its wire shape, and on arrival the
envelope rewrite, the owner-only write locked down before its content, the join through
the live session manager and the rollback a refused or cancelled import owes its file
pair, plus when an imported tab is marked transcript-only. Bundle construction and the
import endpoint live in ``test_session_transfer.py``, the slot an import lands as in
``test_session_transfer_landing.py``, and the Layer B snapshot a send streams in
``test_session_transfer_outbound.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _identity_exchange,
    _isolate_import_tmp,
    _JsonReply,
    _make_request,
    _PostedBody,
    _run_import,
    _send,
    _size_refusing_peer,
    _slot,
    _state,
    _stub_state,
    _valid,
)

from kiro_crew.dashboard.session_transfer import _validate_bundle

# ── Layer B (kiro-cli context) ──────────────────────────────────────────


def _sessions(sid):
    """A stand-in for the live SessionManager's resume-lookup surface.

    Used with ``_resolve_layer_b_sid``, which runs ON THE LOOP -- ``resumable_sid``
    self-prunes the session map, so it must never be reached from a worker thread.
    """
    return SimpleNamespace(resumable_sid=lambda _k: sid)


def test_layer_b_bundle_carries_the_context_when_the_session_has_one(monkeypatch, tmp_path):
    """Layer B is what makes an imported session RESUME instead of replaying a
    lossy transcript prefix, so it must ride along when the session has one."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "11111111-2222-3333-4444-555555555555"
    (tmp_path / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "cwd": "/Users/src/proj"}), encoding="utf-8"
    )
    (tmp_path / f"{sid}.jsonl").write_text('{"kind":"Prompt"}\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    got = st._read_layer_b(sid)

    assert got is not None
    assert got["sid"] == sid
    assert got["envelope"]["session_id"] == sid
    assert "Prompt" in got["events"].path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "sid",
    [
        # A brand-new slot (or a pruned map entry) has no Layer B; the transfer must
        # degrade to transcript-only rather than fail.
        pytest.param("", id="layer_b_is_absent_when_the_session_has_no_kiro_context"),
        # A map entry can outlive its files; a missing pair is not an error.
        pytest.param("no-such-sid", id="layer_b_absent_when_the_files_were_pruned"),
    ],
)
def test_layer_b_is_absent(monkeypatch, tmp_path, sid):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    assert st._read_layer_b(sid) is None


def test_events_jsonl_loadable_accepts_valid_and_rejects_truncated():
    """Structural check only -- it must never rewrite what it inspects."""
    from kiro_crew.dashboard import session_transfer as st

    blob = (
        json.dumps({"kind": "Prompt", "data": {"content": "hi", "n": 1}})
        + "\n"
        + json.dumps({"kind": "AssistantMessage", "data": {"text": "ok"}})
        + "\n"
    )

    assert st._events_jsonl_is_loadable(blob) is True
    assert st._events_jsonl_is_loadable("not json at all\n") is False
    # One bad record poisons the blob even when others are fine.
    assert st._events_jsonl_is_loadable(json.dumps({"kind": "Prompt"}) + "\ntruncated {\n") is False
    # Empty and blank-only are structurally fine.
    assert st._events_jsonl_is_loadable("") is True
    assert st._events_jsonl_is_loadable("\n\n") is True
    # A last record with no trailing newline is still checked.
    assert st._events_jsonl_is_loadable('{"a": 1}\ntruncated {') is False
    assert st._events_jsonl_is_loadable('{"a": 1}\n{"b": 2}') is True


def test_events_jsonl_validation_holds_one_record_at_a_time():
    """Records inside the Layer B string are invisible to the parse-memory
    admission, so validating them must not materialise every record at once."""
    import tracemalloc

    from kiro_crew.dashboard import session_transfer as st

    records = 200_000
    blob = "{}\n" * records
    tracemalloc.start()
    try:
        assert st._events_jsonl_is_loadable(blob) is True
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A per-record list costs about 50 bytes a record (10 MB here); one live
    # record at a time stays far below the blob's own 600 KB.
    assert peak < len(blob), peak


@pytest.mark.asyncio
async def test_unparseable_layer_b_degrades_to_transcript_only(monkeypatch, tmp_path):
    """End-to-end on the send side: a crash-truncated source blob must produce a
    bundle with no Layer B rather than one the peer cannot load."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "eeeeeeee-1111-2222-3333-555555555555"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text('{"kind":"Prompt"}\ntruncated {\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    assert st._read_layer_b(sid) is None


@pytest.mark.asyncio
async def test_import_refuses_unparseable_layer_b_from_the_peer(monkeypatch, tmp_path):
    """The sender is not trusted: an unparseable blob must not be installed."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    assert st._write_layer_b_files({"envelope": {}, "events": "truncated {\n"}, "") is None
    assert not list(tmp_path.glob("*.json")), "nothing may be written for a refused blob"


def test_events_jsonl_handles_empty_and_blank():
    from kiro_crew.dashboard import session_transfer as st

    assert st._events_jsonl_is_loadable("") is True
    assert st._events_jsonl_is_loadable("\n\n") is True


# The shape of a REAL kiro-cli thinking block, which earlier revisions of this
# feature corrupted. ``signature`` is a cryptographic signature over the thinking
# content; the provider validates it when the conversation is replayed, so any
# rewrite of a covered byte makes the peer's NEXT turn fail -- long after the
# import reported success. Every Layer B test below uses this shape rather than an
# empty ``{}`` envelope, because an empty envelope cannot catch that class of bug.
_THINKING_ENVELOPE = {
    "session_id": "src-sid",
    "cwd": "/Users/someone/work/project",
    "session_state": {
        "version": "v1",
        "agent_name": "sender-agent",
        "permissions": {"filesystem": {"allowed_read_paths": ["/Users/someone/work"]}},
        "conversation_metadata": {
            "user_turn_metadatas": [
                {
                    "result": {
                        "Ok": {
                            "content": [
                                {
                                    "kind": "thinking",
                                    "data": {
                                        "modelId": "some-model",
                                        "text": "let me think about the plan",
                                        "redactedContent": None,
                                        # base64-shaped, like the real signature
                                        "signature": "Ci8KCEFTU0lTVEFOVBIhCgt0aGlua2luZ19zaWc",
                                    },
                                }
                            ]
                        }
                    }
                }
            ]
        },
    },
}


def test_layer_b_leaves_the_conversation_byte_exact_on_egress(monkeypatch, tmp_path):
    """Layer B ships verbatim. Redacting it and transplanting it cannot both hold:
    the thinking-block signature covers the content, so a scrub invalidates the
    conversation the transfer exists to carry."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    events = json.dumps({"kind": "AssistantMessage", "data": {"text": "ok"}}) + "\n"
    (tmp_path / f"{sid}.json").write_text(json.dumps(_THINKING_ENVELOPE), encoding="utf-8")
    # Bytes, not text: a text write turns "\n" into "\r\n" on Windows, and the
    # snapshot is byte-exact.
    (tmp_path / f"{sid}.jsonl").write_bytes(events.encode())
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    got = st._read_layer_b(sid)

    assert got is not None
    assert got["events"].path.read_bytes() == events.encode(), "the events blob was rewritten"
    assert got["envelope"] == _THINKING_ENVELOPE, "the envelope was rewritten"


@pytest.mark.asyncio
async def test_layer_b_is_skipped_while_a_turn_is_in_flight(monkeypatch):
    """Layer A records the prompt on submit; kiro-cli writes Layer B only when the
    turn persists. A mid-turn bundle would therefore pair a transcript that SHOWS
    the prompt with a context that lacks it, and the peer would resume the model
    behind its own visible transcript. Degrade to transcript-only instead."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    slot.running = True
    resolved: list[str] = []
    monkeypatch.setattr(
        st, "_resolve_layer_b_sid", lambda *a: (resolved.append("called"), "sid")[1]
    )

    bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")

    assert "layer_b" not in bundle
    assert bundle["bundle_version"] == 2
    assert resolved == [], "the sid must not even be resolved mid-turn"


@pytest.mark.asyncio
async def test_layer_b_travels_when_the_slot_is_idle(monkeypatch, tmp_path):
    """The positive case: an idle slot still carries its context."""
    from kiro_crew.dashboard import session_transfer as st

    sid = "cccccccc-1111-2222-3333-444444444444"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *a: sid)

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    slot = _slot(msgs)
    slot.running = False

    bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")

    assert bundle["layer_b"]["envelope"]["session_id"] == sid


@pytest.mark.asyncio
async def test_layer_b_eligibility_is_recomputed_on_snapshot_retry(monkeypatch):
    """Regression: a prompt starting DURING the threaded read forces a retry, and
    that retry re-reads Layer A with the new prompt. If eligibility were computed
    once up front, the retry would ship the new prompt alongside the pre-turn
    Layer B — the exact skew the mid-turn check exists to prevent, reintroduced
    through the retry path."""
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "persisted", "ts": ""}]
    slot = _slot(msgs)
    slot.running = False
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *a: "the-sid")

    calls = {"n": 0}
    real_to_thread = st.asyncio.to_thread

    async def _turn_starts_midway(fn, *args):
        result = fn(*args)
        calls["n"] += 1
        if calls["n"] == 1:
            # A prompt lands while we are off the loop: Layer A grows AND the slot
            # goes busy. Both must be seen by the retry.
            slot.messages = slot.messages + [{"role": "user", "content": "new prompt", "ts": ""}]
            slot.running = True
        return result

    st.asyncio.to_thread = _turn_starts_midway  # type: ignore[assignment]
    try:
        bundle = await st.build_transfer_bundle_async(_state(msgs), slot, origin="mac")
    finally:
        st.asyncio.to_thread = real_to_thread  # type: ignore[assignment]

    assert calls["n"] >= 2, "expected a retry once the slot changed"
    # The retry saw the running slot, so Layer B must be dropped even though the
    # first attempt was eligible.
    assert "layer_b" not in bundle
    assert [m["content"] for m in bundle["messages"]] == ["persisted", "new prompt"]


@pytest.mark.asyncio
async def test_imported_tab_is_marked_when_it_arrived_transcript_only(monkeypatch):
    """The sender's row is ephemeral (component state in a menu that closes), but
    the consequence is felt later on the RECEIVING machine. The tab title is the
    surface that persists and sits where the loss will be discovered."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    slot = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), return_slot=True
    )

    assert slot.title.endswith("— transcript only"), slot.title


@pytest.mark.asyncio
async def test_imported_tab_is_not_marked_when_layer_b_landed(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    slot = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), return_slot=True
    )

    assert "transcript only" not in slot.title


@pytest.mark.asyncio
async def test_a_v1_import_is_not_marked_transcript_only(monkeypatch):
    """A v1 bundle carries no context by construction — the copy is exactly what
    was sent, so flagging it would cry wolf."""
    from kiro_crew.dashboard import session_transfer as st

    slot = await _run_import(st, monkeypatch, _valid(bundle_version=1), return_slot=True)

    assert "transcript only" not in slot.title


def test_read_layer_b_takes_a_sid_not_the_live_manager():
    """Regression guard: ``resumable_sid`` -> ``SessionMap.get`` SELF-PRUNES, so it
    is a map mutation and must stay on the event loop (same contract as the
    join). The threaded reader therefore takes an immutable sid and has no way to
    reach the map."""
    import inspect

    from kiro_crew.dashboard import session_transfer as st

    params = inspect.signature(st._read_layer_b).parameters
    assert list(params) == ["sid"]
    assert "sessions" not in params
    # ...and the assemble step the thread runs carries only the sid too.
    assert "sessions" not in inspect.signature(st._read_and_assemble).parameters


def test_resolve_layer_b_sid_is_the_loop_side_lookup():
    from kiro_crew.dashboard import session_transfer as st

    assert st._resolve_layer_b_sid(_sessions("the-sid"), "dashboard:slot-1") == "the-sid"
    assert st._resolve_layer_b_sid(_sessions(None), "dashboard:slot-1") == ""
    assert st._resolve_layer_b_sid(None, "dashboard:slot-1") == ""


def test_resolve_layer_b_sid_swallows_lookup_failure():
    from kiro_crew.dashboard import session_transfer as st

    def _boom(_k):
        raise RuntimeError("map unreadable")

    assert st._resolve_layer_b_sid(SimpleNamespace(resumable_sid=_boom), "k") == ""


def test_layer_b_envelope_rewrite_neutralises_the_source_host():
    """The conversation state (what makes resume work) is kept verbatim; only the
    fields that reference the SOURCE host are rewritten."""
    from kiro_crew.dashboard import session_transfer as st

    env = {
        "session_id": "old-sid",
        "cwd": "/Volumes/workplace/only-on-my-mac",
        "title": "leaky title",
        "session_state": {
            "conversation_metadata": {"user_turn_metadatas": [{"keep": "me"}]},
            "agent_name": "source-agent",
            "permissions": {
                "filesystem": {
                    "allowed_read_paths": ["/Volumes/workplace/only-on-my-mac"],
                    "allowed_write_paths": ["/Volumes/workplace/only-on-my-mac"],
                }
            },
        },
    }

    out = st._rewrite_layer_b_envelope(env, "fresh-sid", "target-agent")

    # Fresh identity keeps copy-never-move: a repeat send cannot collide.
    assert out["session_id"] == "fresh-sid"
    # Unscoped on arrival, matching the deliberate decision to drop `project`.
    assert out["cwd"] == ""
    assert out["session_state"]["permissions"]["filesystem"]["allowed_read_paths"] == []
    assert out["session_state"]["permissions"]["filesystem"]["allowed_write_paths"] == []
    assert out["session_state"]["agent_name"] == "target-agent"
    assert out["title"] is None
    # The resumable context itself survives untouched — that is the whole point.
    assert out["session_state"]["conversation_metadata"] == {
        "user_turn_metadatas": [{"keep": "me"}]
    }
    # The source path must not survive anywhere in the envelope.
    assert "only-on-my-mac" not in json.dumps(out)
    # The caller's dict is not mutated.
    assert env["session_id"] == "old-sid"


def test_layer_b_rewrite_tolerates_a_minimal_envelope():
    """A peer on a different kiro-cli build may omit whole subtrees."""
    from kiro_crew.dashboard import session_transfer as st

    out = st._rewrite_layer_b_envelope({}, "fresh-sid", "")

    assert out["session_id"] == "fresh-sid"
    assert out["session_state"]["agent_name"] is None


def test_write_layer_b_files_writes_a_fresh_sid_and_never_touches_the_map(monkeypatch, tmp_path):
    """File writes run in a worker thread, so they must NOT touch the session
    map: ``SessionMap.set`` mutates a shared dict and serialises the whole file,
    which races the event loop's own map writes."""
    import inspect

    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "peer-sid", "cwd": "/peer/path"}, "events": '{"k":1}\n'},
        "my-agent",
    )

    # Fresh sid, NOT the peer's — copy semantics.
    assert new_sid and new_sid != "peer-sid"
    written = json.loads((tmp_path / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert written["session_id"] == new_sid
    assert written["cwd"] == ""
    assert written["session_state"]["agent_name"] == "my-agent"
    # Events are re-serialised per record by the redactor, so compare PARSED
    # content, not bytes: JSON spacing is normalised (`{"k":1}` -> `{"k": 1}`),
    # which is semantically identical for a consumer that parses it.
    written_events = (tmp_path / f"{new_sid}.jsonl").read_text(encoding="utf-8")
    assert [json.loads(ln) for ln in written_events.split("\n") if ln.strip()] == [{"k": 1}]
    # No map access from the thread half — asserted structurally: the function
    # takes no ``sessions`` handle, so it cannot reach the live map at all.
    # (A source-text scan is wrong here: the docstring names the hazard on
    # purpose.)
    assert "sessions" not in inspect.signature(st._write_layer_b_files).parameters


def test_write_layer_b_files_returns_none_on_failure(monkeypatch, tmp_path):
    from kiro_crew.dashboard import session_transfer as st

    def _boom():
        raise OSError("no dir")

    monkeypatch.setattr(st, "kiro_sessions_dir", _boom)

    assert st._write_layer_b_files({"envelope": {}, "events": ""}, "") is None


def test_join_layer_b_goes_through_the_live_manager():
    """Regression guard for the review finding: constructing ``SessionMap()``
    writes a correct file that the live map then clobbers, because its ``_data``
    is a startup snapshot and every ``set`` rewrites the whole file.

    Asserted by absence of the IMPORT — if the name is not bound in the module it
    cannot be constructed, and the body's own comments legitimately mention the
    hazard they prevent.
    """
    from kiro_crew.dashboard import session_transfer as st

    assert not hasattr(st, "SessionMap"), "session_transfer must not import SessionMap"
    recorded: dict = {}
    live = SimpleNamespace(
        seed_conversation=lambda key, sid, *, provider="", cwd="": recorded.update(
            {"key": key, "sid": sid, "provider": provider}
        )
    )

    assert st._join_layer_b(live, "dashboard:imported-1", "new-sid") is True
    assert recorded == {"key": "dashboard:imported-1", "sid": "new-sid", "provider": "acp"}


def test_join_layer_b_without_a_live_manager_is_a_clean_no():
    from kiro_crew.dashboard import session_transfer as st

    assert st._join_layer_b(None, "k", "sid") is False


def test_join_layer_b_is_best_effort():
    """A join failure must NOT fail an already-persisted import — the session
    still opens as the transcript-only copy."""
    from kiro_crew.dashboard import session_transfer as st

    def _boom(*_a, **_k):
        raise OSError("map write failed")

    assert st._join_layer_b(SimpleNamespace(seed_conversation=_boom), "k", "sid") is False


def test_bundle_includes_layer_b_when_present():
    from kiro_crew.dashboard import session_transfer as st

    msgs = [{"role": "user", "content": "hi", "ts": ""}]
    bundle = st._assemble_bundle(
        msgs, "t", "", "mac", {"sid": "s", "envelope": {"session_id": "s"}, "events": "e"}
    )

    assert bundle["bundle_version"] == 2
    assert bundle["layer_b"]["envelope"]["session_id"] == "s"
    # The sid is NOT sent: the importer allocates its own.
    assert "sid" not in bundle["layer_b"]


def test_bundle_omits_layer_b_when_the_session_has_none():
    from kiro_crew.dashboard import session_transfer as st

    bundle = st._assemble_bundle(
        [{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", None
    )

    assert "layer_b" not in bundle


def test_validate_accepts_a_v1_bundle_without_layer_b():
    """A v1 sender (transcript-only) must still be able to send us a copy."""
    bundle, err = _validate_bundle(_valid(bundle_version=1))

    assert err is None
    assert "layer_b" not in bundle


def test_validate_accepts_a_v2_bundle_with_layer_b():
    bundle, err = _validate_bundle(
        _valid(layer_b={"envelope": {"session_id": "s"}, "events": '{"k":1}\n'})
    )

    assert err is None
    assert bundle["layer_b"]["events"] == '{"k":1}\n'


@pytest.mark.parametrize(
    "layer_b,code",
    [
        ("not a dict", "transfer_layer_b_not_object"),
        ({"envelope": "nope", "events": ""}, "transfer_layer_b_bad_envelope"),
        ({"envelope": {}, "events": 5}, "transfer_layer_b_bad_events"),
    ],
)
def test_validate_rejects_a_malformed_layer_b(layer_b, code):
    """Layer B is untrusted peer input and is checked for SHAPE before anything is
    written. It is not size-capped: the whole body was bounded by the
    stream-to-disk (and the operator's optional ceiling) before this validator."""
    _, err = _validate_bundle(_valid(layer_b=layer_b))

    assert err is not None
    assert json.loads(err.body)["code"] == code


@pytest.mark.asyncio
async def test_import_materialises_layer_b_so_the_session_resumes(monkeypatch):
    """End-to-end on the receiving side: a v2 bundle must land a joined Layer B."""
    from kiro_crew.dashboard import session_transfer as st

    calls: dict = {}

    def _fake(layer_b, agent):
        calls.update({"layer_b": layer_b, "agent": agent})
        return "new-sid"

    monkeypatch.setattr(st, "_write_layer_b_files", _fake)
    monkeypatch.setattr(
        st, "_join_layer_b", lambda _s, k, _sid: calls.update({"sm_key": k}) or True
    )
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {"session_id": "s"}, "events": "e"})
    )

    assert resp.status == 200
    assert calls["layer_b"]["events"] == "e"
    # Joined under the SESSION key (what turns run on), which is what the resume
    # path reads — not the raw slot key.
    assert calls["sm_key"]


@pytest.mark.asyncio
async def test_import_still_succeeds_when_layer_b_cannot_be_materialised(monkeypatch):
    """Best-effort: the transcript copy already persisted, so a Layer B failure
    must not turn a landed import into an error."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    resp = await _run_import(st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}))

    assert resp.status == 200
    assert json.loads(resp.body)["ok"] is True


@pytest.mark.asyncio
async def test_import_of_a_v1_bundle_does_not_touch_layer_b(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    called = {"n": 0}

    def _count(*_a, **_k):
        called["n"] += 1
        return True

    monkeypatch.setattr(st, "_write_layer_b_files", _count)
    resp = await _run_import(st, monkeypatch, _valid(bundle_version=1))

    assert resp.status == 200
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_import_reports_session_load_when_layer_b_landed(monkeypatch):
    """The sender must be able to tell a full copy from a degraded one — without
    this the feature's own failure mode (a silently lossy copy) shows the same
    green "Sent" as a successful resume."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    resp = await _run_import(st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}))

    assert json.loads(resp.body)["resume_mode"] == "session_load"


@pytest.mark.asyncio
async def test_a_carried_bundle_resumes_and_its_tab_is_not_marked_transcript_only(monkeypatch):
    """The import-side invariant the file export now depends on.

    A file export that carries Layer B produces a v2 bundle WITH ``layer_b`` and
    WITHOUT ``layer_b_skipped``. When that context materialises, the tab resumes
    through ``session/load`` and must NOT wear the "transcript only" suffix -- the
    suffix is for a degraded copy, and a carried export is not degraded.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)

    bundle = _valid(layer_b={"envelope": {}, "events": "e"})
    assert "layer_b_skipped" not in bundle, "a carried export never sets the skip flag"

    slot = await _run_import(st, monkeypatch, bundle, return_slot=True)

    assert "transcript only" not in slot.title, slot.title


@pytest.mark.asyncio
async def test_import_reports_prefix_when_layer_b_failed(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: None)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: False)
    resp = await _run_import(st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}))

    assert json.loads(resp.body)["resume_mode"] == "prefix"


@pytest.mark.asyncio
async def test_import_reports_prefix_for_a_v1_bundle(monkeypatch):
    """A v1 bundle carries no context by construction, so "prefix" is the honest
    answer rather than an error."""
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, _valid(bundle_version=1))

    assert json.loads(resp.body)["resume_mode"] == "prefix"


@pytest.mark.asyncio
async def test_send_bundle_downgrades_to_v1_when_the_peer_refuses_v2():
    """Gaining Layer B must not REMOVE the ability to send to a not-yet-upgraded
    peer: an older peer refuses bundle_version 2 outright, so retry once with the
    transcript-only v1 shape it has always accepted."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "tok"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )

    seen: list[dict] = []

    class _Resp(_JsonReply):
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
            seen.append(dict(json))
            if json.get("bundle_version") == 2:
                return _Resp(400, {"code": "transfer_version_unsupported"})
            return _Resp(200, {"key": "remote-1", "resume_mode": "prefix"})

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await _send(
            mgr,
            "peer",
            {"bundle_version": 2, "messages": [], "layer_b": {"envelope": {}, "events": "e"}},
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert len(seen) == 2
    # The retry drops Layer B and re-tags as v1 — same conversation, v1 fidelity.
    assert seen[0]["bundle_version"] == 2 and "layer_b" in seen[0]
    assert seen[1]["bundle_version"] == 1 and "layer_b" not in seen[1]


@pytest.mark.asyncio
async def test_send_bundle_downgrades_only_once():
    """A peer that refuses BOTH versions must surface an error, not spin."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "tok"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    posts = {"n": 0}

    class _Resp(_JsonReply):
        status = 400

        async def json(self):
            return {"code": "transfer_version_unsupported"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
            posts["n"] += 1
            return _Resp()

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await _send(
            mgr, "peer", {"bundle_version": 2, "layer_b": {"envelope": {}, "events": "e"}}
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is False
    assert payload["code"] == "transfer_version_unsupported"
    assert posts["n"] == 2, "one downgrade retry, then stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["transfer_layer_b_too_large", "transfer_bundle_too_large"])
async def test_send_bundle_drops_layer_b_when_an_older_peer_refuses_the_size(code):
    """This side sends Layer B whatever its size, but a peer on an older release
    enforces a ceiling and refuses the whole bundle. That peer accepts the
    session transcript-only, so resend it that way rather than fail it."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    mgr, seen, session_factory = _size_refusing_peer(code)
    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = session_factory  # type: ignore[assignment]
    try:
        ok, payload = await _send(
            mgr,
            "peer",
            {"bundle_version": 2, "messages": [], "layer_b": {"envelope": {}, "events": "e"}},
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert payload["resume_mode"] == "prefix"
    assert len(seen) == 2
    # Same version, no Layer B: a v2 importer takes a context-free v2 bundle.
    assert seen[1]["bundle_version"] == 2 and "layer_b" not in seen[1]


@pytest.mark.asyncio
async def test_send_bundle_surfaces_a_size_refusal_without_layer_b():
    """A bundle that has no Layer B to drop is refused as it was: the fallback
    retries once, only when there is something to take out."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    mgr, seen, _ = _size_refusing_peer("transfer_bundle_too_large")

    class _Resp(_JsonReply):
        status = 400

        async def json(self):
            return {"code": "transfer_bundle_too_large"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
            seen.append(dict(json))
            return _Resp()

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await _send(mgr, "peer", {"bundle_version": 2, "messages": []})
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is False
    assert payload["code"] == "transfer_bundle_too_large"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_layer_b_lands_before_the_transcript_is_persisted(monkeypatch):
    """Ordering guard: the slot is registered in ``state._slots`` synchronously
    and handlers enumerate that dict directly, so it is GET-reachable before the
    awaits finish. A prompt landing while the join is missing cold-starts a FRESH
    context and the later join binds to nothing — the silent context loss this
    feature exists to prevent. So the join must be written FIRST.
    """
    from kiro_crew.dashboard import session_transfer as st

    order: list[str] = []

    def _mat(*_a, **_k):
        order.append("layer_b")
        return "new-sid"

    async def _save(*_a, **_k):
        order.append("save")
        return True

    monkeypatch.setattr(st, "_write_layer_b_files", _mat)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), save=_save
    )

    assert resp.status == 200
    assert order == ["layer_b", "save"], order


@pytest.mark.asyncio
async def test_failed_save_rolls_back_the_layer_b_join(monkeypatch):
    """Because the join now precedes the save, a rollback has to undo it — else
    the map keeps an entry for a tab that does not exist and the
    ``<sid>.{json,jsonl}`` pair lingers until a prune sweeps it."""
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "new-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, key: (forgotten.append(key), "")[1])

    async def _boom(*_a, **_k):
        raise OSError("disk full")

    resp = await _run_import(
        st, monkeypatch, _valid(layer_b={"envelope": {}, "events": "e"}), save=_boom
    )

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "transfer_import_save_failed"
    assert forgotten, "the join must be dropped when the import is refused"


def test_unlink_layer_b_files_removes_the_pair(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    sid = "dddddddd-eeee-ffff-0000-111111111111"
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    (tmp_path / f"{sid}.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text("", encoding="utf-8")

    st._unlink_layer_b_files(sid)

    assert not (tmp_path / f"{sid}.json").exists()
    assert not (tmp_path / f"{sid}.jsonl").exists()


def test_forget_layer_b_join_returns_the_sid_for_file_cleanup():
    from kiro_crew.dashboard import session_transfer as st

    dropped: list[str] = []
    live = SimpleNamespace(forget_conversation=lambda key: (dropped.append(key), "old-sid")[1])

    assert st._forget_layer_b_join(live, "dashboard:imported-1") == "old-sid"
    assert dropped == ["dashboard:imported-1"]


def test_forget_layer_b_join_is_silent_without_a_live_manager():
    from kiro_crew.dashboard import session_transfer as st

    assert st._forget_layer_b_join(None, "k") == ""  # must not raise


@pytest.mark.asyncio
async def test_send_bundle_downgrades_a_v2_bundle_that_has_no_layer_b():
    """A session with no kiro-cli context ships v2 with NO ``layer_b`` key. Gating
    the downgrade on Layer B presence would skip exactly those transfers and fail
    them against a v1 peer, so the gate is the VERSION."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    # The generation map: this carrier re-reads the forward it resolved
    # before it spends the credential, and the generation is half of that
    # reading, so a manager assembled without `__init__` must name it.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "tok"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    seen: list[dict] = []

    class _Resp(_JsonReply):
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, _url, data=None, headers=None):
            return _PostedBody(lambda body: self._respond(_url, body, headers), data)

        def _respond(self, _url, json=None, headers=None):
            seen.append(dict(json))
            if json.get("bundle_version") == 2:
                return _Resp(400, {"code": "transfer_version_unsupported"})
            return _Resp(200, {"key": "remote-1"})

    import kiro_crew.instances.ssh_tunnel_manager as mod

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        # No "layer_b" key at all — the context-free case.
        ok, payload = await _send(
            mgr, "peer", {"bundle_version": 2, "messages": [{"role": "user", "content": "hi"}]}
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert len(seen) == 2, "a context-free v2 bundle must still downgrade"
    assert seen[1]["bundle_version"] == 1


# ── Layer B resource + permission bounds ─────────────────────────────────


def test_layer_b_is_read_in_full_without_a_size_cap(monkeypatch, tmp_path):
    """Export carries the FULL Layer B — there is no size cap on the read.

    The old degradation ("Layer B too large -> transcript-only") is gone: a large
    context window is copied, not silently dropped, so a resumable session stays
    resumable however big its context is.
    """
    from kiro_crew.dashboard import session_transfer as st

    sid = "big"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    big_events = "".join('{"kind":"Prompt"}\n' for _ in range(50_000))
    (tmp_path / f"{sid}.jsonl").write_text(big_events, encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    layer_b = st._read_layer_b(sid)
    assert layer_b is not None
    # A snapshot of the log, not the log's text: it streams on egress.
    assert isinstance(layer_b["events"], st.LayerBEvents)
    assert layer_b["events"].path.read_text(encoding="utf-8") == big_events


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses ACLs")
def test_imported_layer_b_is_owner_only(monkeypatch, tmp_path):
    """Layer B is the model's whole context window -- every user turn and tool
    result. Default umask 022 would publish the imported copy at 0644 for any
    other local user to read.
    """
    from kiro_crew.dashboard import session_transfer as st

    d = tmp_path / "cli"  # absent, so this call is the one that creates it
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: d)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert new_sid
    for suffix in (".json", ".jsonl"):
        mode = (d / f"{new_sid}{suffix}").stat().st_mode & 0o777
        assert mode == 0o600, f"{suffix} landed at {oct(mode)}, not owner-only"
    assert d.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses ACLs")
def test_import_does_not_repermission_kiro_clis_existing_dir(monkeypatch, tmp_path):
    """Hardening is scoped to the directory this code creates.

    This is kiro-cli's own sessions dir; silently tightening a pre-existing one
    would mutate posture on a directory the feature does not own. The FILES are
    owner-only either way, which is what actually contains the context.
    """
    from kiro_crew.dashboard import session_transfer as st

    d = tmp_path / "cli"
    d.mkdir()
    # A deliberately GROUP/OTHER-READABLE fixture: the whole point is to hand the
    # code a directory laxer than what it would choose, so "did not re-permission"
    # is observable. Asserting on an already-0700 tmp dir would pass even if the
    # code did chmod it. Test-only fixture state, never a shipped permission.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- see above  # noqa: E501
    os.chmod(d, 0o755)  # separate from mkdir: mkdir's mode is umask-masked
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: d)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert new_sid
    assert d.stat().st_mode & 0o777 == 0o755, "pre-existing dir was re-permissioned"
    assert (d / f"{new_sid}.jsonl").stat().st_mode & 0o777 == 0o600


def test_layer_b_write_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    """The shared helper allocates its temp via ``mkstemp`` rather than a
    deterministic ``<name>.tmp``, which is what made concurrent writers race to
    ENOENT. Pin that only the two real files remain.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    new_sid = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert new_sid
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"{new_sid}.json",
        f"{new_sid}.jsonl",
    ]


def test_validate_carries_the_skipped_flag_through():
    """The marker branches on this flag, so it must survive normalisation.

    ``_validate_bundle`` builds a fresh dict, so a field it does not copy reads
    as absent downstream no matter what the sender put on the wire.
    """
    bundle, err = _validate_bundle(_valid(layer_b_skipped=True))
    assert err is None
    assert bundle["layer_b_skipped"] is True
    # Coerced, not passed through: it arrives from an untrusted peer.
    bundle, _ = _validate_bundle(_valid(layer_b_skipped="yes"))
    assert bundle["layer_b_skipped"] is True
    bundle, _ = _validate_bundle(_valid())
    assert bundle["layer_b_skipped"] is False


def test_mid_turn_bundle_announces_that_it_withheld_context():
    """An absent ``layer_b`` is ambiguous on the wire; the sender disambiguates."""
    from kiro_crew.dashboard import session_transfer as st

    skipped = st._assemble_bundle([], "t", "", "mac", None, True)
    never_had = st._assemble_bundle([], "t", "", "mac", None, False)

    assert skipped["layer_b_skipped"] is True
    assert "layer_b" not in skipped
    assert "layer_b_skipped" not in never_had


@pytest.mark.asyncio
async def test_imported_tab_is_marked_when_the_sender_withheld_layer_b(monkeypatch):
    """The gap: a mid-turn source ships NO ``layer_b``, so gating the marker on
    that key silenced the tab in exactly the case the sender's own row was
    reporting as transcript-only.

    The two negatives are covered elsewhere: a v1 bundle by
    ``test_a_v1_import_is_not_marked_transcript_only``, and a v2 session that
    never had a context by ``test_import_creates_a_new_slot_with_no_project``
    (plain ``_valid()``, no flag -> no suffix).
    """
    from kiro_crew.dashboard import session_transfer as st

    bundle = _valid(layer_b_skipped=True)
    bundle.pop("layer_b", None)

    slot = await _run_import(st, monkeypatch, bundle, return_slot=True)

    assert slot.title.endswith("— transcript only"), slot.title


def test_a_mapped_but_unreadable_layer_b_is_reported_as_withheld(monkeypatch):
    """Absence and LOSS are different wires.

    A mapped sid whose files will not read (pruned, over the cap, unparseable)
    is context the session had and is giving up, so the peer must hear about it
    -- otherwise the imported tab looks like a complete copy with no resumable
    context behind it. An empty sid stays silent: there was nothing to carry.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_read_chained_history", lambda _state, key: ([], (key,)))
    monkeypatch.setattr(st, "_read_layer_b", lambda _sid: None)

    lost = st._read_and_assemble(
        None, "k", [{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", "mapped-sid"
    )
    never_had = st._read_and_assemble(
        None, "k", [{"role": "user", "content": "hi", "ts": ""}], "t", "", "mac", ""
    )

    assert lost["layer_b_skipped"] is True
    assert "layer_b_skipped" not in never_had


def test_layer_b_is_discarded_when_owner_lockdown_fails(monkeypatch, tmp_path):
    """Fail CLOSED, and leave nothing behind.

    On Windows the POSIX mode bits are a no-op, so the owner-only DACL applied by
    ``atomic_write(restrict_to_owner=True)`` is the only thing making the file
    owner-only -- if it fails, the context would be readable by other local
    accounts on a shared machine. Refusing costs only resume fidelity (the import
    lands transcript-only), so it is the cheaper side of the trade. Both files
    must go: the pair is useless alone and the ``.json`` carries context too.

    The lockdown seam lives inside ``atomic_write`` now (it locks the temp file
    down BEFORE any content reaches it), so the failure is injected
    at ``platform_compat.restrict_to_owner`` -- the module-level function the
    helper calls -- not at a name in this module.
    """
    from kiro_crew import platform_compat
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)

    def _refuse(_path):
        raise OSError("cannot resolve the invoking user's SID")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _refuse)

    got = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert got is None, "returned a sid for context it could not protect"
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"left unprotected context on disk: {leftovers}"


def test_layer_b_lockdown_precedes_content(monkeypatch, tmp_path):
    """The context window must never exist in a file that has not been locked
    down yet.

    On Windows the POSIX mode bits are a no-op, so the owner-only DACL is the
    only protection; applying it after the rename left Layer B readable under
    the inherited ACL for the write window. Asserted by measuring
    each file's SIZE at the moment its lockdown is applied — zero means no
    payload byte existed yet. A post-write stat passes on the buggy ordering
    too, so it would not be a regression test.
    """
    from kiro_crew import platform_compat
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    sizes: list[int] = []
    real_restrict = platform_compat.restrict_to_owner

    def _measuring_restrict(target):
        sizes.append(os.stat(target).st_size)
        return real_restrict(target)

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _measuring_restrict)

    got = st._write_layer_b_files(
        {"envelope": {"session_id": "old"}, "events": '{"kind":"Prompt"}\n'}, "agent"
    )

    assert got is not None
    assert len(sizes) == 2, f"expected one lockdown per file of the pair: {sizes}"
    assert sizes == [0, 0], f"a file already held payload bytes when it was locked down: {sizes}"


def test_import_preserves_the_thinking_signature_verbatim(monkeypatch, tmp_path):
    """THE regression test for this feature's worst failure mode.

    An earlier revision redacted Layer B on both boundaries. Measured against 704
    real sessions on a developer machine, that rewrote a thinking-block
    ``signature`` in 41% of them -- and the provider validates that signature when
    it replays the conversation, so the peer's ``session/load`` succeeded and its
    very NEXT turn was rejected. Every test in the suite passed, because they all
    used ``{"envelope": {}}``.

    Asserts on the byte-level file content, not on the in-memory dict, because the
    file is what kiro-cli reads.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    sig = _THINKING_ENVELOPE["session_state"]["conversation_metadata"]["user_turn_metadatas"][0][
        "result"
    ]["Ok"]["content"][0]["data"]["signature"]

    new_sid = st._write_layer_b_files(
        {"envelope": _THINKING_ENVELOPE, "events": '{"kind":"Prompt"}\n'}, "target-agent"
    )

    assert new_sid
    written = json.loads((tmp_path / f"{new_sid}.json").read_text(encoding="utf-8"))
    landed = written["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]
    block = landed["result"]["Ok"]["content"][0]
    assert block["data"]["signature"] == sig, "the thinking signature was rewritten"
    assert block["data"]["text"] == "let me think about the plan"
    # The events blob lands byte-for-byte.
    assert (tmp_path / f"{new_sid}.jsonl").read_text(encoding="utf-8") == '{"kind":"Prompt"}\n'
    # Host-naming fields ARE still neutralised -- byte-exact applies to the
    # conversation, not to the fields that point at the sender's machine.
    assert written["session_id"] == new_sid
    assert written["cwd"] == ""
    assert written["session_state"]["agent_name"] == "target-agent"
    assert written["session_state"]["permissions"]["filesystem"]["allowed_read_paths"] == []


# ── failure-path hygiene found by review on ff205cba1 ────────────────────


def test_layer_b_write_leaves_no_half_pair_when_the_second_write_fails(monkeypatch, tmp_path):
    """The pair is written one file at a time. A failure on the SECOND write left
    the first behind: an orphan no join references, that ``_read_layer_b`` will not
    load (it needs both), and that nothing else cleans up."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    real_write = st.atomic_write
    calls = {"n": 0}

    def _fail_on_second(path, text, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space left on device")
        return real_write(path, text, **kw)

    monkeypatch.setattr(st, "atomic_write", _fail_on_second)

    got = st._write_layer_b_files(
        {"envelope": _THINKING_ENVELOPE, "events": '{"kind":"Prompt"}\n'}, ""
    )

    assert got is None
    assert calls["n"] == 2, "expected the second write to be the failing one"
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == [], f"half-pair left on disk: {leftovers}"


@pytest.mark.asyncio
async def test_import_rolls_back_layer_b_on_cancellation(monkeypatch):
    """``CancelledError`` is a BaseException, so the ordinary ``except Exception``
    never saw it -- a shutdown after the join left an orphaned map entry and files
    behind a slot that never publishes."""
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    async def _cancel(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(st, "save_slot_off_loop", _cancel)
    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(
        st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "lb-sid")[1]
    )
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch, save=_cancel)
    with pytest.raises(asyncio.CancelledError):
        await st.api_chat_slot_import(
            _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
        )

    assert forgotten, "the join was not rolled back on cancellation"
    assert unlinked == ["lb-sid"], f"files were not removed: {unlinked}"
    assert state._slots == {}
    assert state._slots_under_construction == set()


@pytest.mark.asyncio
async def test_a_refusal_cleans_layer_b_when_the_delete_left_no_replacement(monkeypatch):
    """ABSENT is not REPLACED, and ``dict.get`` answers ``None`` for both.

    The ordinary permanent delete pops by key and puts nothing back, so this
    import's own Layer B pair is nobody else's -- and the delete does not unwind
    these module-local helpers. Folding the absent case into the replaced one
    orphans a ``.json``, a ``.jsonl`` and a join for a session with no tab, and
    nothing re-cleans it: this refusal return is terminal and re-arms nothing.

    Mutation-checked: collapsing the two cases back into one ``else`` leaves
    ``unlinked`` empty here while the replacement test below still passes.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    # The join this import wrote, so the map names THIS import's sid. Load-bearing:
    # the forget is scoped by equality against it.
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)

    def _deleted_with_no_replacement(_state, slot):
        # What the permanent delete does: pop by key, put nothing back.
        state._slots.pop(slot.key, None)
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_with_no_replacement)
    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 409, resp.body
    assert forgotten, "the join must be dropped -- with no replacement it is this import's own"
    assert unlinked == ["lb-sid"], (
        "this import's own Layer B pair must be removed; nothing else will, so "
        f"leaving it orphans files in the shared store: {unlinked}"
    )


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_replacement_s_layer_b_alone(monkeypatch):
    """The other half, and the reason the guard exists at all.

    A DIFFERENT object holding the key is a session somebody else created. Its
    slot, its join and its files are its own writer's, so a refusal touches none
    of them -- the case that makes a bare pop-by-key wrong.

    Mutation-checked: widening the cleanup to fire whenever the key is not this
    object unlinks the replacement's files here, while the absent test above
    still passes.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)
    replacement = SimpleNamespace(key="", folder_id="")

    def _deleted_then_recreated(_state, slot):
        replacement.key = slot.key
        state._slots[slot.key] = replacement
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_then_recreated)
    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 409, resp.body
    assert unlinked == [], f"a replacement's files must survive: {unlinked}"
    assert forgotten == [], "a replacement's join mapping must survive"
    assert (
        state._slots.get(replacement.key) is replacement
    ), "the replacement's own slot must still be registered"


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_foreign_join_mapping_alone(monkeypatch):
    """An ABSENT key does not make the mapping at that key this import's.

    A replacement can land at the same key, register its OWN join, and be popped
    again before the object read -- which leaves ``current is None`` while the
    mapping belongs to that replacement. Forgetting by key alone would drop its
    mapping and its continuable mark, and nothing re-arms: this refusal return is
    terminal. The object comparison cannot separate this from a plain delete, so
    the forget is scoped by equality against this import's own sid instead.

    Mutation-checked: dropping the guard, or weakening it from an equality to a
    presence test, forgets the foreign mapping here while the own-sid test above
    still passes.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_write_layer_b_files", lambda *_a, **_k: "lb-sid")
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)
    # A replacement's join occupies the key: present, and NOT this import's sid.
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "replacement-sid")
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)

    def _deleted_then_replaced_then_popped(_state, slot):
        state._slots.pop(slot.key, None)
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_then_replaced_then_popped)
    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 409, resp.body
    assert forgotten == [], (
        "a mapping naming another session's sid must survive -- forgetting it "
        f"drops that session's resume context with nothing to re-arm: {forgotten}"
    )
    # The other half, in the same call: scoping the forget must not disable the
    # cleanup this import genuinely owes for its own files.
    assert unlinked == [
        "lb-sid"
    ], f"this import's own Layer B pair must still be removed: {unlinked}"


@pytest.mark.asyncio
async def test_a_refusal_without_layer_b_forgets_no_mapping(monkeypatch):
    """Holding no Layer B of its own, this import has no mapping to drop.

    The bundle carries no Layer B, so there is no join this import wrote. Any
    mapping sitting at the key is therefore somebody else's by construction, and
    a forget here could only ever take a foreign one.

    Mutation-checked: dropping the guard forgets the squatting mapping here.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    unlinked: list[str] = []

    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "squatter-sid")
    monkeypatch.setattr(st, "_forget_layer_b_join", lambda _s, k: (forgotten.append(k), "")[1])
    monkeypatch.setattr(st, "_unlink_layer_b_files", lambda sid: unlinked.append(sid))

    state = _stub_state(st, monkeypatch)

    def _deleted_with_no_replacement(_state, slot):
        state._slots.pop(slot.key, None)
        return True

    monkeypatch.setattr(st, "session_was_deleted", _deleted_with_no_replacement)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 409, resp.body
    assert forgotten == [], f"an import holding no Layer B must drop no mapping: {forgotten}"
    assert unlinked == [], f"and must unlink no files: {unlinked}"
