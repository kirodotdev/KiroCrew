"""The sending side's privacy gate and the staged upload that carries a bundle to its peer.

Pins the transcript file's own privacy line withholding a bundle (at the read, after it,
and again before the request), a forward replaced during serialisation getting nothing,
the streaming writer matching ``json.dumps`` byte for byte, the Layer B snapshot and its
release, the upload's no-progress deadline, the peer's reply read under a cap, and the
once-per-process sweep of both staging directories. Bundle construction and the delivery
hop's refusals live in ``test_session_transfer.py``; the receiving side's body lives in
``test_session_transfer_import_body.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from types import SimpleNamespace

import pytest
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _async_value,
    _ChunkedContent,
    _FakeLog,
    _isolate_import_tmp,
    _JsonReply,
    _OwnerReq,
    _PersistentLine,
    _send,
    _serialise,
    _size_refusing_peer,
    _slot,
    _state,
)

# --------------------------------------------------------------------------- #
# the file's own privacy contract gates the bundle, not only the live slot
# --------------------------------------------------------------------------- #

MSGS = [
    {"role": "user", "content": "PRIVATE-1", "ts": ""},
    {"role": "assistant", "content": "PRIVATE-2", "ts": ""},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("line_mode", ["incognito", "temporary", "Incognito"])
async def test_a_restricted_on_disk_line_withholds_the_bundle(line_mode):
    """A persistent slot over a restricted line: the rows come from disk, so the
    line on disk decides. A same-key recreation of a closed restricted tab, or a
    writer tightening the line while this slot still reads persistent, both
    reach here with a slot the callers' own gate lets through."""
    from kiro_crew.dashboard import session_transfer as st

    state = _state(MSGS)
    state.conversation_log.metadata = {"memory_mode": line_mode}
    slot = _slot(MSGS)
    with pytest.raises(st.TranscriptWithheld):
        await st.build_transfer_bundle_async(state, slot)


@pytest.mark.asyncio
async def test_an_unreadable_on_disk_line_withholds_the_bundle():
    """Fail closed: a builder that cannot see the contract ships nothing."""
    from kiro_crew.dashboard import session_transfer as st

    state = _state(MSGS)
    state.conversation_log.readable = False
    slot = _slot(MSGS)
    with pytest.raises(st.TranscriptWithheld):
        await st.build_transfer_bundle_async(state, slot)


@pytest.mark.asyncio
async def test_a_line_tightened_during_the_read_withholds_the_bundle():
    """The gate is asked again AFTER the read, so a tightening in between is caught."""
    from kiro_crew.dashboard import session_transfer as st

    state = _state(MSGS)
    log = state.conversation_log
    real_derive = log.derive_messages_chained

    def _tighten_then_derive(key):
        # The tightening writer takes the transcript lock the seam holds, so it
        # lands before the seam's hold (modelled here) or after it -- never inside.
        log.metadata = {"memory_mode": "incognito"}
        return real_derive(key)

    log.derive_messages_chained = _tighten_then_derive
    slot = _slot(MSGS)
    with pytest.raises(st.TranscriptWithheld):
        await st.build_transfer_bundle_async(state, slot)


@pytest.mark.asyncio
async def test_send_handler_refuses_a_slot_whose_line_is_restricted(monkeypatch):
    """The tunnel send maps the builder's refusal to its own slot-gate answer, so a
    persistent slot over a restricted file sends nothing to the peer."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    class _Log(_PersistentLine):
        metadata = {"memory_mode": "incognito"}

        def read_messages_chained(self, _key):
            return list(MSGS)

    slot = _slot(MSGS)
    slot.key = "slot-1"
    assert slot.memory_mode == "persistent"

    async def _save(_state, s, best_effort=True):
        return True

    monkeypatch.setattr(st, "save_slot_off_loop", _save)

    sent: list = []

    class _Mgr:
        async def send_session_bundle(self, _id, bundle, **_kw):
            sent.append(bundle)
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=_Log(),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 400, resp.body
    assert json.loads(resp.body)["code"] == "transfer_slot_not_persistent"
    assert sent == [], "a bundle reached the peer despite the restricted line"


@pytest.mark.asyncio
async def test_send_handler_maps_a_busy_transcript_to_the_retryable_503(monkeypatch):
    """A lock the seam could not take is 'retry', not 'not persistent'."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import TranscriptBusy

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    class _Log(_PersistentLine):
        def read_messages_chained(self, _key):
            return list(MSGS)

        def derive_messages_chained(self, _key):
            raise TranscriptBusy("held by another writer")

    slot = _slot(MSGS)
    slot.key = "slot-1"

    async def _save(_state, s, best_effort=True):
        return True

    monkeypatch.setattr(st, "save_slot_off_loop", _save)
    sent: list = []

    class _Mgr:
        async def send_session_bundle(self, _id, bundle, **_kw):
            sent.append(bundle)
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=_Log(),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 503, resp.body
    assert json.loads(resp.body)["code"] == "transfer_snapshot_unstable"
    assert sent == []


@pytest.mark.asyncio
async def test_send_handler_rechecks_the_privacy_line_before_the_request(monkeypatch):
    """A line that tightens after the pre-send check, while the body serialises,
    is caught by the recheck the send runs just before the request."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )

    class _Log(_PersistentLine):
        def read_messages_chained(self, _key):
            return list(MSGS)

    log = _Log()
    slot = _slot(MSGS)
    slot.key = "slot-1"

    async def _save(_state, s, best_effort=True):
        return True

    monkeypatch.setattr(st, "save_slot_off_loop", _save)
    sent: list = []

    class _Mgr:
        async def send_session_bundle(self, _id, bundle, *, serialise, recheck=None):
            # The line tightens while the body serialises.
            log.metadata = {"memory_mode": "incognito"}
            refusal = await asyncio.to_thread(recheck) if recheck else None
            if refusal is not None:
                return False, refusal
            sent.append(bundle)
            return True, {"key": "remote-1"}

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=log,
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 400, resp.body
    assert json.loads(resp.body)["code"] == "transfer_slot_not_persistent"
    assert sent == [], "the bundle was sent after its privacy line tightened"


def _events_file(tmp_path, text: str):
    p = tmp_path / "events.jsonl"
    p.write_bytes(text.encode("utf-8", "surrogatepass"))
    return p


def test_the_streaming_writer_matches_json_dumps_byte_for_byte(tmp_path):
    """Every importer parses what ``json.dumps`` wrote, so the streamed body has
    to be exactly that: CJK, a character outside the BMP, a lone surrogate, and a
    log long enough to cross many chunk boundaries."""
    import io

    from kiro_crew.dashboard import session_transfer as st

    events = '{"a":"é 😀"}\r\n' + "x中😀" * 200_000 + "\n"
    bundle = {
        "bundle_version": 2,
        "title": "t",
        "messages": [{"role": "user", "content": "hi \ud800 😀", "ts": ""}] * 3,
        "layer_b": {
            "envelope": {"k": [1, 2]},
            "events": st.LayerBEvents(_events_file(tmp_path, events)),
        },
        "source": {"exported_at": "x"},
    }
    out = io.BytesIO()
    st.write_bundle_json(bundle, out.write)

    expected = {**bundle, "layer_b": {"envelope": {"k": [1, 2]}, "events": events}}
    assert out.getvalue() == json.dumps(expected, separators=(",", ":")).encode()


def test_the_layer_b_snapshot_is_the_log_byte_for_byte(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    src = _events_file(tmp_path, '{"k":1}\r\n\n{"k":"中"}\n')
    snap = st._snapshot_events_file(src)

    assert snap is not None and snap.read_bytes() == src.read_bytes()


def test_an_unparseable_log_leaves_no_snapshot_behind(tmp_path):
    from kiro_crew.dashboard import session_transfer as st

    src = _events_file(tmp_path, '{"k":1}\n{"k":\n')

    assert st._snapshot_events_file(src) is None
    assert list(st._egress_tmp_dir().iterdir()) == []


def test_a_failed_assembly_releases_the_snapshot(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    sid = "s1"
    (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    (tmp_path / f"{sid}.jsonl").write_text('{"k":1}\n', encoding="utf-8")
    monkeypatch.setattr(st, "kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(st, "_read_chained_history", lambda *_a: ([], ("k",)))

    def _boom(*_a, **_k):
        raise RuntimeError("assembly failed")

    monkeypatch.setattr(st, "_assemble_bundle", _boom)

    with pytest.raises(RuntimeError):
        st._read_and_assemble(SimpleNamespace(), "k", [], "t", "", "", sid)
    assert list(st._egress_tmp_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_send_bundle_uploads_the_serialised_file_and_removes_it(tmp_path):
    """With a serialiser the peer receives the file's bytes, never ``json=``;
    each attempt's file is gone afterwards, the retry's included."""
    import kiro_crew.instances.ssh_tunnel_manager as mod
    from kiro_crew.dashboard import session_transfer as st

    mgr, _seen, _ = _size_refusing_peer("transfer_layer_b_too_large")
    posted: list[bytes] = []
    written: list = []

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

        def post(self, _url, data=None, json=None, headers=None):
            assert json is None, "a serialised bundle must not be re-encoded in memory"
            assert headers["Content-Type"] == "application/json"

            class _Upload:
                async def __aenter__(self_inner):
                    body = b"".join([c async for c in data])
                    posted.append(body)
                    if b'"layer_b"' in body:
                        return _Resp(400, {"code": "transfer_layer_b_too_large"})
                    return _Resp(200, {"key": "remote-1", "resume_mode": "prefix"})

                async def __aexit__(self_inner, *a):
                    return False

            return _Upload()

    def _serialise(b):
        p = st.write_bundle_file(b, compress=False)
        written.append(p)
        return p

    bundle = {
        "bundle_version": 2,
        "messages": [{"role": "user", "content": "hi", "ts": ""}],
        "layer_b": {"envelope": {}, "events": st.LayerBEvents(_events_file(tmp_path, '{"k":1}\n'))},
    }
    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = lambda *a, **k: _Session()  # type: ignore[assignment]
    try:
        ok, payload = await _send(mgr, "peer", bundle, serialise=_serialise)
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is True, payload
    assert json.loads(posted[0])["layer_b"]["events"] == '{"k":1}\n'
    assert "layer_b" not in json.loads(posted[1])
    assert len(written) == 2 and not any(p.exists() for p in written)


@pytest.mark.asyncio
async def test_an_upload_the_peer_stops_reading_times_out(tmp_path, monkeypatch):
    """No total timeout, but no progress for ``_TRANSFER_TIMEOUT`` ends it."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    monkeypatch.setattr(mod, "_TRANSFER_TIMEOUT", 0.05)
    body = tmp_path / "body.json"
    body.write_bytes(b"x" * (3 * mod._UPLOAD_CHUNK_BYTES))

    async def _stalled_peer():
        stall = asyncio.timeout(None)
        async with stall:
            with body.open("rb") as fh:
                async for _chunk in mod._upload_chunks(fh, stall):
                    # The peer never reads the next chunk: only the lapsed
                    # no-progress deadline can end this wait.
                    await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await _stalled_peer()


@pytest.mark.asyncio
async def test_a_finished_upload_leaves_the_reply_wait_to_the_read_timeout(tmp_path):
    """The no-progress deadline is armed while chunks flow and cleared by the
    last pull, so the wait for the peer's reply is the per-read timeout's alone.

    The hand-off is read from the stall itself (``Timeout.when()``): a deadline
    of ``None`` cannot lapse, however slow the reply. ``_TRANSFER_TIMEOUT`` stays
    at its default, because each pull is a thread hop whose latency is the
    runner's, and a deadline shorter than that hop times the UPLOAD out -- the
    sibling test's case, not this one's.
    """
    import kiro_crew.instances.ssh_tunnel_manager as mod

    body = tmp_path / "body.json"
    body.write_bytes(b"{}")

    armed: list[float | None] = []
    stall = asyncio.timeout(None)
    async with stall:
        with body.open("rb") as fh:
            got = []
            async for chunk in mod._upload_chunks(fh, stall):
                armed.append(stall.when())
                got.append(chunk)
        handed_off = stall.when()

    assert got == [b"{}"]
    assert armed and all(when is not None for when in armed), armed
    assert handed_off is None


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["busy", "withheld"])
async def test_a_publication_refusal_releases_the_layer_b_snapshot(monkeypatch, refusal):
    """The refusal returns before the send, so the snapshot the bundle carries
    must be removed there too, or every refused attempt leaves one on disk."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.history import TranscriptBusy, TranscriptWithheld

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )
    snap = st._egress_tmp_dir() / "snap.jsonl"
    snap.write_bytes(b'{"k":1}\n')

    async def _build(*_a, **_k):
        return st.TransferBundle(
            {"messages": [], "layer_b": {"envelope": {}, "events": st.LayerBEvents(snap)}},
            publication_keys=("k",),
        )

    monkeypatch.setattr(hi, "build_transfer_bundle_async", _build)

    class _RefusesAtCommit(_FakeLog):
        @contextlib.contextmanager
        def publication_hold(self, _key, *, expected_keys=None):
            raise (TranscriptBusy("busy") if refusal == "busy" else TranscriptWithheld("tightened"))
            yield

    class _Mgr:
        async def send_session_bundle(self, _id, _bundle, **_kw):
            raise AssertionError("the tunnel POST ran after publication was refused")

    slot = _slot([{"role": "user", "content": "hi", "ts": ""}])
    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=_RefusesAtCommit(slot.messages),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status in (400, 503)
    assert not snap.exists(), "the refused send left its Layer B snapshot behind"


@pytest.mark.asyncio
async def test_a_forward_replaced_during_serialisation_gets_nothing(tmp_path):
    """Serialising a large session is the longest await before the request, so a
    tunnel replaced inside it must not receive the body or the credential."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    mgr, seen, session_factory = _size_refusing_peer("unused")

    def _serialise_then_replace(bundle):
        path = _serialise(bundle)
        mgr._tunnel_epoch["peer"] = mgr._tunnel_epoch.get("peer", 0) + 1
        return path

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = session_factory  # type: ignore[assignment]
    try:
        ok, payload = await mgr.send_session_bundle(
            "peer", {"bundle_version": 2, "messages": []}, serialise=_serialise_then_replace
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is False
    assert payload["code"] == "transfer_peer_not_connected"
    assert seen == [], "the request went out to a replaced forward"


@pytest.mark.asyncio
async def test_a_source_withheld_during_serialisation_gets_nothing(tmp_path):
    """The caller's recheck runs after serialising, before the request: a privacy
    line tightened while a large session serialised must stop the send."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    mgr, seen, session_factory = _size_refusing_peer("unused")
    order: list[str] = []

    def _serialise_logged(bundle):
        order.append("serialise")
        return _serialise(bundle)

    def _recheck():
        order.append("recheck")
        return {"error": "withheld", "code": "transfer_slot_not_persistent"}

    original = mod.aiohttp.ClientSession
    mod.aiohttp.ClientSession = session_factory  # type: ignore[assignment]
    try:
        ok, payload = await mgr.send_session_bundle(
            "peer",
            {"bundle_version": 2, "messages": []},
            serialise=_serialise_logged,
            recheck=_recheck,
        )
    finally:
        mod.aiohttp.ClientSession = original  # type: ignore[assignment]

    assert ok is False
    assert payload["code"] == "transfer_slot_not_persistent"
    assert order == ["serialise", "recheck"]
    assert seen == [], "the request went out after the source was withheld"


def test_a_log_that_cannot_be_opened_leaves_no_descriptor(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    opened: list[int] = []
    real_mkstemp = st.tempfile.mkstemp

    def _mkstemp(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        opened.append(fd)
        return fd, name

    monkeypatch.setattr(st.tempfile, "mkstemp", _mkstemp)

    assert st._snapshot_events_file(tmp_path / "gone.jsonl") is None
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert list(st._egress_tmp_dir().iterdir()) == []


@pytest.mark.asyncio
async def test_a_cleanup_failure_after_a_committed_send_keeps_the_success(monkeypatch):
    """The peer committed the import; failing to remove the staged body must not
    turn that into an error whose retry would duplicate the session."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    mgr, seen, session_factory = _size_refusing_peer("unused")

    def _unlink(self, missing_ok=False):
        raise OSError(116, "Stale file handle")

    monkeypatch.setattr(mod.aiohttp, "ClientSession", session_factory)
    monkeypatch.setattr(mod.Path, "unlink", _unlink)
    ok, _payload = await mgr.send_session_bundle(
        "peer", {"bundle_version": 2, "messages": []}, serialise=_serialise
    )

    assert ok is True
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_peer_reply_is_read_under_a_cap_and_never_buffered_past_it(monkeypatch):
    """A peer that keeps sending is cut off at the cap: the reader stops pulling
    and the reply reads as ``{}``, so the gateway never buffers it without end."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    monkeypatch.setattr(mod, "_TRANSFER_REPLY_MAX_BYTES", 1000)
    pulled = {"n": 0}

    class _Endless:
        async def iter_chunked(self, _n: int):
            while True:
                pulled["n"] += 1
                if pulled["n"] > 1000:
                    raise AssertionError("kept reading past the cap")
                yield b" " * 100

    assert await mod._read_transfer_reply(SimpleNamespace(content=_Endless())) == {}
    assert pulled["n"] == 11

    ok_body = SimpleNamespace(content=_ChunkedContent([b'{"key":', b'"remote-1"}']))
    assert await mod._read_transfer_reply(ok_body) == {"key": "remote-1"}


def test_staging_sweep_removes_only_files_older_than_this_process(monkeypatch, tmp_path):
    """Crash orphans are reclaimed; a live transfer's file, a symlink target and a
    subdirectory are never touched."""
    from kiro_crew.dashboard import session_transfer as st

    started = 1_000_000.0
    monkeypatch.setattr(st, "_PROCESS_STARTED_AT", started)
    monkeypatch.setattr(st, "_swept_staging_dirs", set())
    d = tmp_path / "staging"
    d.mkdir()
    orphan = d / "orphan.json"
    orphan.write_bytes(b"x")
    os.utime(orphan, (started - 60, started - 60))
    live = d / "live.json"
    live.write_bytes(b"x")
    os.utime(live, (started + 60, started + 60))
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"keep")
    os.utime(outside, (started - 60, started - 60))
    sub = d / "sub"
    sub.mkdir()
    link = d / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        link = None

    st._sweep_orphaned_staging(d)

    assert not orphan.exists()
    assert live.exists()
    assert sub.is_dir()
    assert outside.read_bytes() == b"keep"
    if link is not None:
        assert link.is_symlink()

    # Once per directory per process: a later orphan waits for the next process.
    late = d / "late.json"
    late.write_bytes(b"x")
    os.utime(late, (started - 60, started - 60))
    st._sweep_orphaned_staging(d)
    assert late.exists()


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX directory symlink")
def test_a_symlinked_staging_dir_is_never_swept(monkeypatch, tmp_path):
    """A link planted where the staging directory should be must not turn the
    sweep onto the files it points at."""
    from kiro_crew.dashboard import session_transfer as st

    started = 1_000_000.0
    monkeypatch.setattr(st, "_PROCESS_STARTED_AT", started)
    monkeypatch.setattr(st, "_swept_staging_dirs", set())
    victim = tmp_path / "victim"
    victim.mkdir()
    precious = victim / "precious.txt"
    precious.write_bytes(b"keep")
    os.utime(precious, (started - 60, started - 60))
    staging = tmp_path / "staging"
    staging.symlink_to(victim, target_is_directory=True)

    st._sweep_orphaned_staging(staging)

    assert precious.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "helper,leaf", [("_import_tmp_dir", "session-import"), ("_egress_tmp_dir", "session-export")]
)
def test_staging_helpers_sweep_orphans_on_first_use(monkeypatch, tmp_path, helper, leaf):
    import importlib.util

    from kiro_crew.dashboard import session_transfer_staging as patched

    # The autouse isolation fixture replaces both helpers on the imported module,
    # so load a private copy whose helpers are the real ones.
    spec = importlib.util.spec_from_file_location("_st_unpatched", patched.__file__)
    assert spec is not None and spec.loader is not None
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)

    started = 1_000_000.0
    monkeypatch.setattr(st, "_PROCESS_STARTED_AT", started)
    monkeypatch.setattr(st, "_swept_staging_dirs", set())
    monkeypatch.setattr(st, "data_home", lambda: tmp_path)
    d = tmp_path / "tmp" / leaf
    d.mkdir(parents=True)
    orphan = d / "orphan.json"
    orphan.write_bytes(b"x")
    os.utime(orphan, (started - 60, started - 60))

    assert getattr(st, helper)() == d
    assert not orphan.exists()
