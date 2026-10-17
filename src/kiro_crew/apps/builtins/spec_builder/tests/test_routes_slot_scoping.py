"""Route tests: acquiring the spec's worker slot.

Every slot goes through ``_ensure_worker_slot``, which adopts only a slot this
app owns, scopes a new one, stamps the default model only on a bare slot,
restores a cold slot's transcript after a gateway restart, and refuses a missing
or invalid working dir or an identity that moves while it awaits.
"""

from __future__ import annotations

import inspect
import json
import types
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
    _slot_stub,
)

# ── handlers offload filesystem work; delete tears down the slot ─────────────


@pytest.mark.asyncio
async def test_teardown_refuses_a_slot_this_app_does_not_own():
    """Anti-collision: a slot whose _app is not ours must be left alone rather
    than deleted because its key happens to match."""

    class _Slot:
        _app = "some-other-app"
        running = False
        task = None

    slots = {"spec-builder-x": _Slot()}

    class _State:
        _slots = slots

        def get_slot(self, key):
            return slots.get(key)

    await routes._teardown_worker_slot(_State(), "x")
    assert "spec-builder-x" in slots


# ── atomic state writes and git auditing ─────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_payload_reports_live_running_state(tmp_path, monkeypatch):
    """The reported defect: the detail payload carried `status` but not `running`,
    so the selected spec's working indicator, document skeleton and fast 2.5s poll
    were all permanently off — the one place they matter."""
    monkeypatch.setattr(routes, "_CAN_PUBLISH_DIR_NOREPLACE", False)
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "live"
    spec_dir.mkdir(parents=True)
    routes._save_index({"live": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}})

    class _Slot:
        key = routes._slot_key("live")
        running = True
        messages: list = []
        # Our own slot: an UNSCOPED slot under this key is somebody else's
        # conversation and is now refused rather than adopted.
        _app = routes.APP_NAME
        project = ""
        _titled = False

    _slot = _Slot()

    class _State:
        def get_slot(self, key):
            return _slot if key == routes._slot_key("live") else None

        def get_or_create_slot(self, name, app=""):
            return _slot

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/live")
        body = await resp.json()
    finally:
        await client.close()

    assert body["running"] is True, "detail payload omits the live running flag"
    assert body["duplicate_supported"] is False


# ── sentinel isolation; the index is arbitrated before the slot ──────────────


def test_create_arbitrates_the_index_before_touching_the_shared_slot():
    """The reported race: get_or_create_slot keys off the NAME, so two concurrent
    same-name creates share one slot. Configuring it before the index decided the
    winner let the LOSER stamp its own working_dir onto the shared slot, so the
    accepted spec's agent ran in the rejected directory."""

    src = inspect.getsource(routes._handle_create)
    arbitration = src.index("_mutate_index(_insert)")
    for acquisition in ("get_or_create_slot(", "_ensure_worker_slot("):
        assert (
            acquisition not in src[:arbitration]
        ), "create acquires the shared slot before the index arbitration"
    for mutation in ("slot.project =", "slot._app =", "slot.title ="):
        assert mutation not in src[:arbitration], f"{mutation} happens before arbitration"
    assert "_ensure_worker_slot(" in src[arbitration:]


# ── path validation off the loop; the slot-scoping chokepoint ────────────────


@pytest.mark.asyncio
async def test_discovered_spec_gets_a_scoped_slot(tmp_path, monkeypatch):
    """The reported defect: a spec created OUTSIDE the app (Kiro CLI/IDE, then
    discovered) had no slot, so the embedded chat's /api/chat created the first
    one — unscoped. No _app (it surfaced in the main sidebar) and no project, so
    approved tools ran from the gateway's working directory instead of the user's
    project. Reading the spec now materializes a SCOPED slot."""
    # The indexed working_dir now goes through _safe_dir; these fixtures use
    # synthetic paths, so accept them (the validation itself is covered
    # separately by test_indexed_working_dir_is_revalidated).
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))

    created: list[str] = []

    class _Slot:
        key = "spec-builder-found"
        running = False
        messages: list = []
        _app = ""
        project = ""
        _titled = False

    slot = _Slot()

    class _State:
        def get_slot(self, key):
            return None  # nothing exists yet — the discovered-spec case

        def get_or_create_slot(self, name, app=""):
            created.append(name)
            return slot

    meta = {"working_dir": "/projects/thing", "spec_dir": "/projects/thing/.kiro/specs/found"}
    out = await routes._ensure_worker_slot(_State(), "found", meta)

    assert out is slot
    assert created == ["spec-builder-found"], "the missing slot was not created"
    assert slot._app == routes.APP_NAME, "slot left unscoped — it would show in the main sidebar"
    assert (
        slot.project == "/projects/thing"
    ), "slot has no project — tools would run in the wrong dir"
    assert slot._titled is True


def test_every_slot_acquisition_goes_through_the_scoping_chokepoint():
    """Source guard: no handler may call get_or_create_slot directly, or a future
    endpoint reintroduces an unscoped slot."""

    src = routes_source()
    sites = [
        ln.strip()
        for ln in src.splitlines()
        if "get_or_create_slot(" in ln and not ln.strip().startswith("#")
    ]
    assert len(sites) == 1, f"get_or_create_slot called outside the chokepoint: {sites}"
    for handler in (
        routes._handle_get,
        routes._handle_messages,
        routes._handle_message,
        routes._handle_handoff,
        routes._handle_create,
    ):
        assert "_ensure_worker_slot(" in inspect.getsource(
            handler
        ), f"{handler.__name__} does not scope the slot it uses"


# ── slot ownership and default-model stamping ────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_slot_is_not_silently_re_owned(monkeypatch):
    """The reported defect: _ensure_worker_slot stamped _app unconditionally, so a
    slot another app already held under the same key was re-owned — its _app
    overwritten and its project repointed at our spec's directory, taking the
    session (and transcript) away from the app that created it."""
    # The indexed working_dir now goes through _safe_dir; these fixtures use
    # synthetic paths, so accept them (the validation itself is covered
    # separately by test_indexed_working_dir_is_revalidated).
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))

    class _Foreign:
        key = "spec-builder-demo"
        _app = "some-other-app"
        project = "/other/project"
        _titled = True

    foreign = _Foreign()

    class _State:
        def get_slot(self, key):
            return foreign if key == "spec-builder-demo" else None

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over a foreign slot")

    out = await routes._ensure_worker_slot(_State(), "demo", {"working_dir": "/ours"})
    assert out is None, "a foreign slot was handed back as ours"
    assert foreign._app == "some-other-app", "foreign ownership was overwritten"
    assert foreign.project == "/other/project", "foreign slot was repointed"


@pytest.mark.asyncio
async def test_only_our_own_slot_is_adopted_and_a_missing_one_is_created(monkeypatch):
    """An UNSCOPED slot under our key is somebody else's conversation (a main-chat
    session that happens to be named `spec-builder-<x>`), and adopting it would
    rewrite its ownership, repoint its project and pull its transcript into this
    app. Only a slot already owned by this app is adopted; a MISSING slot is still
    created and scoped, which is what keeps a discovered spec usable."""
    # The indexed working_dir now goes through _safe_dir; these fixtures use
    # synthetic paths, so accept them (the validation itself is covered
    # separately by test_indexed_working_dir_is_revalidated).
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))

    # (a) already ours -> adopted and (re)scoped.
    class _Ours:
        key = "spec-builder-mine"
        _app = routes.APP_NAME
        project = ""
        _titled = False

    ours = _Ours()

    class _StateOurs:
        def get_slot(self, key):
            return ours

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("should not recreate an existing owned slot")

    assert await routes._ensure_worker_slot(_StateOurs(), "mine", {"working_dir": "/p"}) is ours
    assert ours.project == "/p"

    # (b) unscoped -> refused, untouched.
    class _Bare:
        key = "spec-builder-bare"
        _app = ""
        project = "/somebody/else"
        _titled = True

    bare = _Bare()

    class _StateBare:
        def get_slot(self, key):
            return bare

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over an unscoped slot")

    assert await routes._ensure_worker_slot(_StateBare(), "bare", {"working_dir": "/ours"}) is None
    assert bare._app == "" and bare.project == "/somebody/else"

    # (c) missing -> created and scoped.
    class _New:
        key = "spec-builder-fresh"
        _app = ""
        project = ""
        _titled = False

    fresh = _New()

    class _StateNew:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return fresh

    assert await routes._ensure_worker_slot(_StateNew(), "fresh", {"working_dir": "/new"}) is fresh
    assert fresh._app == routes.APP_NAME and fresh.project == "/new"


@pytest.mark.asyncio
async def test_default_model_is_stamped_on_a_bare_slot(monkeypatch, tmp_path):
    """The app-wide default model from settings lands on a worker slot that has
    no explicit pick — the single creation chokepoint, same place project and
    title are stamped."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": "test-model-x"})

    class _New:
        key = "spec-builder-fresh"
        _app = ""
        project = ""
        model = ""
        _titled = False

    fresh = _New()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return fresh

    out = await routes._ensure_worker_slot(_State(), "fresh", {"working_dir": "/new"})
    assert out is fresh
    assert fresh.model == "test-model-x", "the app default was not stamped"


@pytest.mark.asyncio
async def test_default_model_never_overwrites_an_explicit_slot_pick(monkeypatch, tmp_path):
    """A per-slot model set through the chat API stays authoritative: the ensure
    chokepoint runs on EVERY dispatch, so an unconditional stamp would silently
    revert an explicit pick on the next message — the main defect to avoid."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": "test-model-x"})

    class _Ours:
        key = "spec-builder-mine"
        _app = routes.APP_NAME
        project = ""
        model = "test-model-explicit"
        _titled = True

    ours = _Ours()

    class _State:
        def get_slot(self, key):
            return ours

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("should not recreate an existing owned slot")

    out = await routes._ensure_worker_slot(_State(), "mine", {"working_dir": "/p"})
    assert out is ours
    assert ours.model == "test-model-explicit", "an explicit per-slot pick was overwritten"


@pytest.mark.asyncio
async def test_empty_default_model_leaves_the_slot_inheriting(monkeypatch, tmp_path):
    """'' = inherit: with no app default configured, the slot's model stays
    empty and the session layer's resolution chain applies unchanged."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": ""})

    class _New:
        key = "spec-builder-plain"
        _app = ""
        project = ""
        model = ""
        _titled = False

    fresh = _New()

    class _State:
        def get_slot(self, key):
            return None

        def get_or_create_slot(self, name, app=""):
            return fresh

    out = await routes._ensure_worker_slot(_State(), "plain", {"working_dir": "/new"})
    assert out is fresh
    assert fresh.model == "", "an empty default must not stamp anything"


@pytest.mark.asyncio
async def test_default_model_is_not_stamped_on_an_adopted_slot(monkeypatch, tmp_path):
    """A slot that already exists (restored across a gateway restart, or simply
    re-ensured on a later dispatch) keeps running exactly as it was: the help
    copy promises a changed default applies to spec sessions started AFTER the
    change, so only a slot this call CREATES may receive the stamp."""
    monkeypatch.setattr(routes, "_safe_dir", lambda raw, **_k: Path(raw))
    monkeypatch.setattr(routes, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path)
    routes._save_settings({"base_path": "", "model": "test-model-x"})

    class _Restored:
        key = "spec-builder-mine"
        _app = routes.APP_NAME
        project = ""
        model = ""  # inheriting, and it must STAY inheriting
        _titled = True

    ours = _Restored()

    class _State:
        def get_slot(self, key):
            return ours

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("should not recreate an existing owned slot")

    out = await routes._ensure_worker_slot(_State(), "mine", {"working_dir": "/p"})
    assert out is ours
    assert ours.model == "", "an adopted slot was re-stamped with the app default"


def test_dispatching_handlers_refuse_a_foreign_slot():
    """Source guard: any handler that dispatches a turn must bail when the slot
    could not be claimed, rather than passing None into _dispatch_turn."""

    for handler in (routes._handle_message, routes._handle_handoff, routes._handle_create):
        src = inspect.getsource(handler)
        claim = src.index("_ensure_worker_slot(")
        assert "if slot is None:" in src[claim:], f"{handler.__name__} ignores a refused slot"
        assert "status=409" in src[claim:], f"{handler.__name__} does not report the conflict"


# ── foreign-slot refusal; the seed prompt is self-contained ──────────────────


@pytest.mark.asyncio
async def test_detail_refuses_when_the_slot_is_foreign(tmp_path, monkeypatch):
    """The reported defect: the detail handler ignored _ensure_worker_slot's
    refusal and still returned 200, so ChatEmbed mounted against the unrelated
    session -- the user could read it, message into it and approve its tool
    calls from this app."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "hijacked"
    spec_dir.mkdir(parents=True)
    routes._save_index(
        {"hijacked": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
    )

    class _Foreign:
        key = "spec-builder-hijacked"
        _app = "some-other-app"
        project = "/other/project"
        running = False
        messages: list = []

    class _State:
        def get_slot(self, key):
            return _Foreign()

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over a foreign slot")

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/hijacked")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, "detail served a spec whose session belongs to another app"
    assert "another app" in body.get("error", "")


# ── every ownership check is exact ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_refuses_an_unscoped_slot():
    """The reported collision: _halt_active_turn tolerated an owner of None, so a
    plain POST /api/chat on slot `spec-builder-<name>` -- somebody else's
    conversation that merely shares the key -- could be cancelled mid-turn by this
    app's Stop button, losing that turn's response. Ownership must be EXACT here,
    as it already is in _ensure_worker_slot and _teardown_worker_slot."""
    stopped: list[str] = []
    cancelled = {"v": False}

    class _Task:
        def cancel(self):
            cancelled["v"] = True

        def done(self):
            return False

    class _Slot:
        def __init__(self, owner):
            self.key = "spec-builder-shared"
            self._app = owner
            self.running = True
            self.task = _Task()

    class _Sessions:
        async def stop_turn(self, key, force=False):
            stopped.append(key)

    def _state_for(slot):
        class _State:
            sessions = _Sessions()

            def get_slot(self, key):
                return slot

        return _State()

    for owner in (None, "", "some-other-app"):
        stopped.clear()
        cancelled["v"] = False
        slot = _Slot(owner)
        assert (
            await routes._halt_active_turn(_state_for(slot), "shared") is False
        ), f"stopped a turn on a slot owned by {owner!r}"
        assert stopped == [] and cancelled["v"] is False

    # Our own slot is still stopped.
    ours = _Slot(routes.APP_NAME)
    assert await routes._halt_active_turn(_state_for(ours), "shared") is True
    assert stopped and cancelled["v"] is True


def test_every_ownership_check_is_exact():
    """Source guard for the class: no ownership comparison may treat an unscoped
    slot as ours."""
    src = routes_source()
    for line in src.splitlines():
        stripped = line.strip()
        if "_app" not in stripped or stripped.startswith("#"):
            continue
        assert "not in (None" not in stripped, f"lax ownership check: {stripped}"


# ── transcript ownership; registration touches no filesystem ─────────────────


@pytest.mark.asyncio
async def test_messages_refuses_a_foreign_transcript(tmp_path, monkeypatch):
    """The reported leak: the messages endpoint ignored _ensure_worker_slot's
    refusal and served the transcript anyway -- somebody else's conversation read
    out through this app. Same refusal the detail endpoint already makes."""
    client = _make_client(monkeypatch, tmp_path)
    routes._save_index({"shared": {"spec_dir": "/s/shared", "working_dir": "/w"}})

    class _Foreign:
        key = "spec-builder-shared"
        _app = "some-other-app"
        running = True
        messages = [{"role": "user", "content": "private", "ts": "1"}]

    class _State:
        def get_slot(self, key):
            return _Foreign()

        def get_or_create_slot(self, name, app=""):
            raise AssertionError("must not create over a foreign slot")

    client.app["state"] = _State()
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/shared/messages")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, "served a transcript belonging to another app"
    assert "private" not in json.dumps(body)


# ── Cold worker slot after a gateway restart ─────────────────────────────────


class _RestartState:
    """A gateway that came back up: no live slots, transcripts still on disk."""

    def __init__(self):
        self._slots: dict = {}
        self.created: list[str] = []

    def get_slot(self, key):
        return self._slots.get(key)

    def get_or_create_slot(self, name, app=""):
        self.created.append(name)
        slot = self._slots.get(name)
        if slot is None:
            slot = types.SimpleNamespace(
                key=name, messages=[], _app=app, project="", _titled=False, running=False
            )
            self._slots[name] = slot
        return slot


def _stub_restore(monkeypatch, state, *, app: str, messages: list[str]):
    """Stand in for core's rehydrate: land a slot carrying a transcript."""

    async def _restore(_state, slot_key, *, adopt_closed=False):
        assert adopt_closed is True, "a worker transcript must survive an idle-archive close"
        slot = types.SimpleNamespace(
            key=slot_key,
            messages=[{"role": "user", "content": m} for m in messages],
            _app=app,
            project="",
            _titled=True,
            running=False,
        )
        state._slots[slot_key] = slot
        return slot

    monkeypatch.setattr(routes, "rehydrate_slot_from_history_async", _restore)


@pytest.mark.asyncio
async def test_restart_restores_the_worker_transcript(tmp_path, monkeypatch):
    """The reported bug: slots live in memory, so a gateway restart emptied the
    chat column ("Session ready. Type a message to start.") for a spec that was
    mid-build -- and the next message started a context-free turn -- even though
    the whole transcript was still on disk under the same key. A cold slot now
    rehydrates from history before anything creates an empty one."""
    _redirect_state(monkeypatch, tmp_path)
    state = _RestartState()
    _stub_restore(monkeypatch, state, app=routes.APP_NAME, messages=["build me a spec", "on it"])

    slot = await routes._ensure_worker_slot(state, "s1", {"working_dir": str(tmp_path)})

    assert slot is not None
    assert [m["content"] for m in slot.messages] == [
        "build me a spec",
        "on it",
    ], "the persisted conversation did not come back"
    assert state.created == [], "an empty slot was created instead of restoring"


@pytest.mark.asyncio
async def test_restore_does_not_adopt_another_apps_transcript(tmp_path, monkeypatch):
    """A transcript is as owned as a live slot. The restore lands the slot in
    state._slots, so the SAME ownership check must govern it -- otherwise
    rehydration became a way to pull another app's conversation (and its project
    dir) into this app, which the live-slot check explicitly forbids."""
    _redirect_state(monkeypatch, tmp_path)
    state = _RestartState()
    _stub_restore(monkeypatch, state, app="some-other-app", messages=["their work"])

    slot = await routes._ensure_worker_slot(state, "s1", {"working_dir": str(tmp_path)})

    assert slot is None, "a foreign transcript was adopted"


@pytest.mark.asyncio
async def test_restore_failure_still_yields_a_working_slot(tmp_path, monkeypatch):
    """Fail-open on the RESTORE only: a malformed or unreadable transcript must
    not take the spec offline. The user loses scrollback, not the app."""
    _redirect_state(monkeypatch, tmp_path)
    state = _RestartState()

    async def _boom(*_a, **_k):
        raise OSError("history is corrupt")

    monkeypatch.setattr(routes, "rehydrate_slot_from_history_async", _boom)

    slot = await routes._ensure_worker_slot(state, "s1", {"working_dir": str(tmp_path)})

    assert slot is not None, "a broken transcript took the spec offline"
    assert getattr(slot, "_app", None) == routes.APP_NAME


def test_transcript_restore_runs_before_slot_creation():
    """Source guard: the restore must precede get_or_create_slot. Core's resume
    returns early when a slot already exists, so creating the empty slot first
    permanently hides the transcript -- including from the user's own manual
    resume in the sidebar."""
    # Compare CODE only: the docstring names get_or_create_slot too.
    body = inspect.getsource(routes._ensure_worker_slot).split('"""')[-1]
    assert body.index("_restore_worker_transcript") < body.index(
        "get_or_create_slot"
    ), "the empty slot is created before the transcript is restored"


# ── settings shapes, slot-name grammar, and the browse skip list ─────────────


@pytest.mark.asyncio
async def test_slot_acquisition_refuses_an_out_of_grammar_name(tmp_path, monkeypatch):
    """A name read back from index.json is untrusted app state, and from here it
    becomes a slot key and then a history key -- reaching core's session-key
    parsing. Unbounded input on that path is what CodeQL flagged once this
    function started restoring transcripts."""
    _redirect_state(monkeypatch, tmp_path)

    class _State:
        def __init__(self):
            self.touched: list[str] = []

        def get_slot(self, key):
            self.touched.append(key)
            return None

        def get_or_create_slot(self, name, app=""):
            self.touched.append(name)
            return types.SimpleNamespace(key=name, _app=app, project="", messages=[])

    for bad in ("x" * 400, "../etc/passwd", "has space", "", "9" * 200 + "." + "9" * 200):
        state = _State()
        assert (
            await routes._ensure_worker_slot(state, bad, {"working_dir": str(tmp_path)}) is None
        ), bad
        assert state.touched == [], f"{bad[:20]!r} reached the slot layer"


# ── the body is parsed first; sentinel clear is directory-pinned ─────────────


def test_slack_ts_regex_is_bounded():
    """CodeQL py/polynomial-redos: this predicate is reached with keys that did
    not come from Slack (an app backend restoring a saved conversation), so an
    unbounded \\d+ on both sides of the dot backtracked quadratically."""
    from kiro_crew.messaging import link as link_mod

    assert "\\d+\\." not in link_mod._SLACK_TS_RE.pattern, "the runs are unbounded again"
    # Real timestamps still match; a long digit run does not hang or match.
    assert link_mod.is_legacy_slack_key("1785360749.165719")
    assert not link_mod.is_legacy_slack_key("9" * 400 + "." + "9" * 400)
    assert not link_mod.is_legacy_slack_key("dashboard:spec-builder-x")


# ── an index entry without a working dir is refused a slot ───────────────────


@pytest.mark.asyncio
async def test_index_entry_without_a_working_dir_is_refused_a_slot(tmp_path, monkeypatch):
    """The reported escape: the refusal was gated on `wd` being truthy, so DELETING
    working_dir from the agent-writable index skipped it entirely. The slot then
    came back with no project, and chat_runner passes cwd=slot.project -- so the
    worker's CLI inherited the GATEWAY's working directory and would run approved
    relative tools from there. Absent must refuse exactly like invalid."""
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "s").mkdir(parents=True)
    meta = {
        "spec_dir": str(base / "s"),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": routes._new_slot_key("s"),
    }
    assert "working_dir" not in meta
    # The entry still passes the load filter, which is deliberate: it is the slot
    # chokepoint that must refuse, not the index reader.
    assert routes._entry_is_usable(meta)

    state, slot = _slot_stub()
    assert (
        await routes._ensure_worker_slot(state, "s", meta) is None
    ), "a projectless entry was handed a slot"
    assert slot.project == "", "the slot was scoped from a missing working_dir"


@pytest.mark.asyncio
async def test_working_dir_absent_and_invalid_refuse_identically(tmp_path, monkeypatch):
    """Both arms of the same class, so a future edit cannot re-split them."""
    base = tmp_path / "wd" / ".kiro" / "specs"
    (base / "s").mkdir(parents=True)
    (base / "s" / "requirements.md").write_text("# r")
    common = {
        "spec_dir": str(base / "s"),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": routes._new_slot_key("s"),
    }

    for label, meta in (
        ("absent", dict(common)),
        ("empty", {**common, "working_dir": ""}),
        ("nonexistent", {**common, "working_dir": str(tmp_path / "gone")}),
        ("not-a-dir", {**common, "working_dir": str(base / "s" / "requirements.md")}),
    ):
        state, _ = _slot_stub()
        assert (
            await routes._ensure_worker_slot(state, "s", meta) is None
        ), f"{label} working_dir was allowed to produce a slot"

    # ...and a real one still works, so the guard is not vacuous.
    state, slot = _slot_stub()
    ok = {**common, "working_dir": str(tmp_path / "wd")}
    assert await routes._ensure_worker_slot(state, "s", ok) is not None
    assert slot.project == str(tmp_path / "wd")


def test_slot_scoping_never_gates_its_working_dir_check_on_presence():
    """Source guard: the refusal must not be conditioned on the value being
    non-empty, which is the shape that let a deleted key through. Asserts on the
    CONDITION, not on a comment."""
    src = inspect.getsource(routes._ensure_worker_slot)
    assert "if safe_wd is None:" in src, "the unconditional refusal is gone"
    assert "if wd and safe_wd is None:" not in src, (
        "the working_dir refusal is gated on presence again: deleting the key from "
        "the index would yield an unscoped slot running in the gateway's cwd"
    )


# ── Slot identity must survive both awaits in _ensure_worker_slot ────────────


_IDENTITY_SPEC = "identity-probe"


def _identity_state(slot_key: str, *, slot=None):
    """Minimal state whose registry is keyed by ONE slot key."""

    created: list = []

    class _State:
        def __init__(self):
            self._slots = {slot_key: slot} if slot is not None else {}

        def get_slot(self, key):
            return self._slots.get(key)

        def get_or_create_slot(self, name, app=""):
            created.append(name)
            made = types.SimpleNamespace(key=name, _app=app, project="", _titled=False)
            self._slots[name] = made
            return made

    return _State(), created


@pytest.mark.asyncio
async def test_replacement_during_transcript_restore_is_refused(monkeypatch, tmp_path):
    """Window 1. _slot_key reads the module-global _SLOT_KEYS; a delete +
    same-name recreate rewrites it. Recomputing after the restore await made the
    stale request adopt the REPLACEMENT's slot and stamp its own project on it."""
    monkeypatch.setitem(routes._SLOT_KEYS, _IDENTITY_SPEC, "spec-builder-old")

    async def _restore(state, name, adopt_closed=False):
        # The concurrent delete + recreate lands while we are awaiting.
        routes._SLOT_KEYS[name] = "spec-builder-new"

    monkeypatch.setattr(routes, "_restore_worker_transcript", _restore)
    state, created = _identity_state("spec-builder-old")

    got = await routes._ensure_worker_slot(state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)})
    assert got is None, "a stale request acquired the replacement spec's slot"
    assert created == [], "the stale request created a slot under the new identity"


@pytest.mark.asyncio
async def test_replacement_during_working_dir_check_is_refused(monkeypatch, tmp_path):
    """Window 2. _safe_dir runs off-loop; the identity can move during it, and
    the code then stamped _app/project onto the slot it captured earlier."""
    monkeypatch.setitem(routes._SLOT_KEYS, _IDENTITY_SPEC, "spec-builder-old")
    ours = types.SimpleNamespace(
        key="spec-builder-old", _app=routes.APP_NAME, project="", _titled=False
    )
    state, _ = _identity_state("spec-builder-old", slot=ours)

    real_safe_dir = routes._safe_dir

    def _safe_dir_then_replace(path):
        out = real_safe_dir(path)
        routes._SLOT_KEYS[_IDENTITY_SPEC] = "spec-builder-new"
        return out

    monkeypatch.setattr(routes, "_safe_dir", _safe_dir_then_replace)

    got = await routes._ensure_worker_slot(state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)})
    assert got is None, "the stale request kept going after its spec was replaced"
    assert ours.project == "", "a replaced spec's slot was repointed at the stale project"
    assert ours._app == routes.APP_NAME


@pytest.mark.asyncio
async def test_stable_identity_still_acquires_the_slot(monkeypatch, tmp_path):
    """The control: nothing moves, so the slot is acquired and scoped as before.
    Without this, refusing unconditionally would also pass the two tests above."""
    monkeypatch.setitem(routes._SLOT_KEYS, _IDENTITY_SPEC, "spec-builder-stable")
    ours = types.SimpleNamespace(
        key="spec-builder-stable", _app=routes.APP_NAME, project="", _titled=False
    )
    state, _ = _identity_state("spec-builder-stable", slot=ours)

    got = await routes._ensure_worker_slot(state, _IDENTITY_SPEC, {"working_dir": str(tmp_path)})
    assert got is ours, "a stable identity failed to acquire its own slot"
    assert got.project == str(tmp_path), "the slot was not scoped to the spec's project"


def test_slot_key_is_resolved_once_and_both_awaits_are_guarded():
    """Source guard on the CALLS and their ORDER, not on a comment.

    One resolution before any await, and an identity re-check after each await.
    A second resolution anywhere in the body is the bug this round fixed.
    """
    src = inspect.getsource(routes._ensure_worker_slot)
    assert src.count("_slot_key(name)") == 1, (
        f"_slot_key(name) is resolved {src.count('_slot_key(name)')} times -- "
        "recomputing it after an await reintroduces the mid-flight identity swap"
    )
    resolve = src.index("slot_key = _slot_key(name)")
    awaits = [
        src.index("await _restore_worker_transcript("),
        src.index("await asyncio.to_thread(_safe_dir"),
    ]
    assert resolve < min(awaits), "the identity is captured after an await"
    guards = [
        i for i in range(len(src)) if src.startswith("_slot_identity_moved(name, slot_key)", i)
    ]
    assert len(guards) == 2, f"expected 2 identity re-checks, found {len(guards)}"
    for a in awaits:
        assert any(g > a for g in guards), "an await is not followed by an identity re-check"
    # Creation must use the captured key.
    assert (
        "get_or_create_slot(name=slot_key" in src
    ), "slot creation does not use the captured identity"


def test_identity_guard_refuses_and_audits():
    """The guard itself: same key passes, changed key refuses."""
    routes._SLOT_KEYS["guard-probe"] = "spec-builder-k1"
    assert routes._slot_identity_moved("guard-probe", "spec-builder-k1") is False
    assert routes._slot_identity_moved("guard-probe", "spec-builder-k0") is True
