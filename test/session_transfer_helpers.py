"""Shared fakes and fixtures for the ``test_session_transfer`` family of test files.

Holds, among other support, the request, slot, log, state and peer doubles (the
``DashboardState`` stub included), a valid-bundle factory, the import driver and the autouse
fixture that points the arrival and egress temp dirs at per-test directories.
``test_session_transfer.py`` and the four sibling files its docstring names import them from
here.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.dashboard.session_transfer import BUNDLE_VERSION


class _OwnerReq(SimpleNamespace):
    """Request double carrying dashboard claims as a real MAPPING.

    ``handlers_instances._guard`` runs the owner predicate, which calls
    ``request.get("user")``, tests ``"app" in request`` and then compares
    ``request["app"]`` -- a plain ``SimpleNamespace`` with a ``get`` attribute
    raises on the ``in`` test. Defaults to the configured owner with no app token;
    pass ``claims=`` for any other caller.
    """

    def __init__(self, *, claims=None, **kw):
        super().__init__(**kw)
        self._claims = dict(claims or {"user": "owner", "app": ""})

    def get(self, key, default=None):
        return self._claims.get(key, default)

    def __contains__(self, key):
        return key in self._claims

    def __getitem__(self, key):
        return self._claims[key]


def _export_gz(document) -> bytes:
    """The compressed export of *document*, staged, read and removed."""
    from kiro_crew.dashboard.session_export import _stage_export

    path = _stage_export(document)
    try:
        return path.read_bytes()
    finally:
        path.unlink()


def _serialise(bundle):
    """The production serialiser, writing under the per-test egress dir."""
    from kiro_crew.dashboard import session_transfer as st

    return st.write_bundle_file(bundle, compress=False)


def _send(mgr, *args, **kwargs):
    """``send_session_bundle`` with the production serialiser."""
    kwargs.setdefault("serialise", _serialise)
    return mgr.send_session_bundle(*args, **kwargs)


class _PostedBody:
    """Adapts a fake that answers from the parsed bundle to the streamed upload:
    the body is read off the upload generator on enter, the way aiohttp sends
    it, then handed to *respond* as the bundle the peer received."""

    def __init__(self, respond, data):
        self._respond, self._data = respond, data

    async def __aenter__(self):
        body = json.loads(b"".join([c async for c in self._data]))
        self._resp = self._respond(body)
        return await self._resp.__aenter__()

    async def __aexit__(self, *a):
        return await self._resp.__aexit__(*a)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no module of this family calls monkeypatch.undo()
def _isolate_import_tmp(tmp_path_factory, monkeypatch):
    """Point the arrival temp dir at a per-test, auto-cleaned directory.

    Arriving bodies stream to disk before they are parsed; without this they would
    land under the real crew home. ``tmp_path_factory`` is pytest-managed, so
    nothing leaks (conftest's tmp-residue watchdog would otherwise flag it)."""
    from kiro_crew.dashboard import session_transfer as st

    d = tmp_path_factory.mktemp("kc-import")
    monkeypatch.setattr(st, "_import_tmp_dir", lambda: d)
    out = tmp_path_factory.mktemp("kc-export")
    monkeypatch.setattr(st, "_egress_tmp_dir", lambda: out)


class _PersistentLine:
    """The on-disk metadata line the builder's privacy gate reads: persistent,
    readable. Tests that need a restricted or unreadable line override these."""

    metadata: dict = {}
    readable: bool = True

    def get_metadata_status(self, _key):
        return dict(self.metadata), self.readable

    def derive_messages_chained(self, key):
        """The derivation seam, as the real log implements it: line, then rows."""
        from kiro_crew.history import TranscriptWithheld, is_incognito_transcript

        meta, readable = self.get_metadata_status(key)
        if not readable or is_incognito_transcript(meta.get("memory_mode")):
            raise TranscriptWithheld("fake: restricted or unreadable")
        return self.read_messages_chained(key)

    @contextlib.contextmanager
    def publication_hold(self, key, *, expected_keys=None):
        from kiro_crew.history import TranscriptBusy, TranscriptWithheld, is_incognito_transcript

        meta, readable = self.get_metadata_status(key)
        if not readable:
            raise TranscriptBusy("fake: unreadable at publication")
        if is_incognito_transcript(meta.get("memory_mode")):
            raise TranscriptWithheld("fake: restricted at publication")
        yield


class _FakeLog(_PersistentLine):
    def __init__(self, messages):
        self._messages = messages

    def read_messages_chained(self, _key):
        return list(self._messages)


def _slot(
    messages, *, title="My session", titled=True, agent="", dirty=False, project="", disk_older=0
):
    return SimpleNamespace(
        key="slot-1",
        title=title,
        _titled=titled,
        agent=agent,
        project=project,
        messages=list(messages),
        _dirty=dirty,
        _resumed_count=len(messages),
        _disk_window_len=len(messages),
        # Frozen-prefix length for the id-based tail merge (_append_unflushed_tail
        # scans the disk read from this offset). 0 fits fakes whose disk read IS
        # the window; a test whose window is a tail of a longer transcript must
        # override it with the real prefix length.
        _disk_older_count=disk_older,
        _pending_rewrite=False,
        _dirty_gen=0,
        memory_mode="persistent",
        # Idle by default: Layer B only travels when no turn is in flight.
        running=False,
    )


def _state(messages):
    return SimpleNamespace(conversation_log=_FakeLog(messages))


def _valid(**over):
    body = {
        "bundle_version": BUNDLE_VERSION,
        "origin": "mac",
        "title": "t",
        "agent": "",
        "messages": [{"role": "user", "content": "hi", "ts": ""}],
    }
    body.update(over)
    return body


async def _identity_exchange(url, link, cookie_name):
    """Stand in for the peer's link exchange: the session cookie is the link."""
    return link


def _size_refusing_peer(refusal_code: str):
    """A manager whose peer refuses any bundle carrying Layer B with *refusal_code*,
    the way an importer that still enforces the size ceilings does. Returns the
    manager, the list of posted bundles, and the fake ``ClientSession`` factory."""
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    mgr = SshTunnelManager.__new__(SshTunnelManager)
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
            if "layer_b" in json:
                return _Resp(400, {"code": refusal_code})
            return _Resp(200, {"key": "remote-1", "resume_mode": "prefix"})

    return mgr, seen, lambda *a, **k: _Session()


# ── helpers ──────────────────────────────────────────────────────────────


def _make_request(state, body, *, raw: str | None = None, gz: bytes | None = None):
    """A minimal aiohttp-request stand-in for the import handler.

    Serves BYTES via ``read()``, not a pre-parsed dict, because the handler
    decides the body format from the first two bytes — a stub that handed back a
    dict would skip the very branch under test. ``gz`` sends compressed bytes
    verbatim (what a browser uploads); ``raw`` sends an exact string (malformed
    JSON); otherwise *body* is serialised the way a real caller would.
    """

    if gz is not None:
        payload = gz
    elif raw is not None:
        payload = raw.encode("utf-8")
    else:
        payload = json.dumps(body).encode("utf-8")

    async def _read():
        return payload

    class _FakeContent:
        """A minimal ``StreamReader`` stand-in exposing ``iter_chunked``.

        The handler streams ``request.content`` to disk rather than calling
        ``request.read()`` (that is the whole point of the no-size-ceiling change),
        so the stub has to serve the bytes the way the real StreamReader does. It
        hands them out in small chunks so the chunk-sniff and the streaming loop
        are actually exercised.
        """

        def __init__(self, data: bytes) -> None:
            self._data = data

        async def iter_chunked(self, n: int):
            for i in range(0, len(self._data), n):
                yield self._data[i : i + n]

    return SimpleNamespace(
        app={"state": state},
        get=lambda _k, default="": default,
        # A real request always has headers, and the caller-identity rule reads
        # ``X-Session-Key`` off them when the auth middleware published no app
        # claim. Empty is the dashboard owner, which is what these tests are.
        headers={},
        content=_FakeContent(payload),
        read=_read,
    )


def _stub_state(st, monkeypatch, save=None):
    # A REAL _ChatSlot, not a hand-rolled fake: the fold routes import through
    # the shared materialiser, which sets ~20 slot attributes and re-runs the
    # title/provenance helpers. A fake that only grows the fields the test
    # happens to touch would silently diverge from the production slot; a real
    # one cannot. get_or_create_slot below mints the key the same way the real
    # one does (name=None -> a fresh key).
    from kiro_crew.dashboard.chat_handlers import _ChatSlot

    def _get_or_create(name=None, *_a, **kwargs):
        s = _ChatSlot(name or "imported-1", agent=kwargs.get("agent", ""))
        # Production's get_or_create_slot REGISTERS the slot in _slots (and
        # broadcasts) before returning; model that, or a test asserting the slot
        # is hidden-during-construction passes vacuously because the stub never
        # put it in _slots at all.
        state._slots[s.key] = s
        state._imported_slot = s
        return s

    async def _save(*_a, **_k):
        return True

    monkeypatch.setattr(st, "save_slot_off_loop", save or _save)
    monkeypatch.setattr(st, "_sync_dashboard_slots", lambda _s: None)
    state = SimpleNamespace(
        _slots={},
        # Mirrors DashboardState's construction accounting: a slot retracted
        # while it is built is absent from _slots but still counts against every
        # cap, so the stub has to model both halves or the handler's cap check
        # and its release would not be exercised at all.
        _slots_under_construction=set(),
        # The shared materialiser toggles this per the imported session's
        # memory_mode; import's bundle carries none, so it stays untouched here,
        # but the attribute must exist for the discard/add to run.
        _restricted_keys=set(),
        _tags=[],
        _tags_authoritative=True,
        _folders=[],
        # ``DashboardState`` always HAS this attribute, and ``None`` is a real
        # production value for it (``state_parts/dashboard_state.py``: ``conversation_log:
        # ConversationLog | None = None``). Present here because the import
        # handler's delete witness reads it on every import; without it the stub
        # raises ``AttributeError`` where production reads a legitimate ``None``.
        #
        # ``None`` makes the witness answer False by its own documented rule — a
        # store with no path resolver cannot witness a delete — so these tests
        # keep exercising the real ``session_was_deleted``, and the DELETE path is
        # driven deliberately in
        # ``test_a_delete_during_the_folder_check_is_not_reported_as_landed``
        # rather than being simulated everywhere.
        conversation_log=None,
        get_or_create_slot=_get_or_create,
        push_slots_update=lambda: None,
        # The materialiser wraps creation + begin_slot_construction in this to
        # defer the creation broadcast; the stub's push is already a no-op, so a
        # nullcontext models it faithfully enough for the paths these tests
        # exercise (the deferred-broadcast/filter interaction is covered on a real
        # DashboardState in test_resume_publishes_hydrated_slot).
        suspend_slots_push=lambda: contextlib.nullcontext(),
        # The live SessionManager surface the Layer B path threads through.
        sessions=SimpleNamespace(
            seed_conversation=lambda *a, **k: None,
            forget_conversation=lambda _k: "",
            resumable_sid=lambda _k: None,
            set_autocompact_pct=lambda *a, **k: None,
        ),
    )
    state.live_slot_count = lambda: len(state._slots) + len(state._slots_under_construction)
    state.begin_slot_construction = state._slots_under_construction.add
    state.end_slot_construction = state._slots_under_construction.discard

    # The folder store, modelled because arrival-provenance filing writes to it
    # on EVERY import (``arrival_folders``). Omitting it would not make these
    # tests fail — the filing is best-effort and swallows the AttributeError —
    # which is exactly why it is here: without it every import test would cover
    # the unfiled fallback and none would cover the shipped path.
    #
    # Mirrors DashboardState.mutate_folders' contract: the mutator runs against
    # the LIVE list and returns ``(changed, value)``, ``on_committed`` fires only
    # when something changed, and the caller gets ``value``.
    async def _mutate_folders(mutate, on_committed=None):
        changed, value = mutate(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    async def _read_folders(read):
        return read(state._folders)

    state.mutate_folders = _mutate_folders
    state.read_folders = _read_folders

    # Seed with the first slot the handler will mint, so tests that read
    # ``state._imported_slot`` before the call still resolve; _get_or_create
    # rebinds it to the real minted slot when the handler runs.
    state._imported_slot = _ChatSlot("imported-1")
    return state


async def _run_import(
    st, monkeypatch, body, *, return_slot=False, created=None, save=None, gz=None, state=None
):
    state = state if state is not None else _stub_state(st, monkeypatch, save=save)
    if created is not None:
        from kiro_crew.dashboard.chat_handlers import _ChatSlot

        def _get_or_create(name=None, *_a, **kwargs):
            created.clear()
            created.update(kwargs)
            if name is not None:
                created["name"] = name
            s = _ChatSlot(name or "imported-1", agent=kwargs.get("agent", ""))
            state._slots[s.key] = s
            state._imported_slot = s
            return s

        state.get_or_create_slot = _get_or_create
    resp = await st.api_chat_slot_import(_make_request(state, body, gz=gz))
    assert isinstance(resp, web.Response)
    if return_slot:
        return state._imported_slot
    return resp


def _async_value(value):
    """Return a zero-arg coroutine function yielding *value* (stub for request.json)."""

    async def _inner():
        return value

    return _inner


class _ChunkedContent:
    """``request.content`` stand-in that yields the given chunks verbatim."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _n: int):
        for c in self._chunks:
            yield c


class _JsonReply:
    """Mixin for a fake peer response: serves its ``json()`` as the chunked body
    the sender reads under its cap, and a ``json()`` that raises as a body that
    is not JSON."""

    @property
    def content(self):
        resp = self

        class _Body:
            async def iter_chunked(self, _n: int):
                try:
                    value = await resp.json()
                except Exception:
                    yield b"not json"
                    return
                yield json.dumps(value).encode()

        return _Body()
