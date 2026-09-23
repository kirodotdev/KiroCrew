"""Session work ledger — core primitive, nudge injection, routes, cleanup.

The state record is now a PROJECTION of the session's crew log (see
docs/system-specs/modules/session-work-ledger.md and the ``ledger`` fold in
``crew_log/projection.py``): :func:`session_ledger.record` appends one
``ledger/recorded`` entry per call and every reader folds those entries back
into the record. This file covers the surface that lives on top of that fold —
the phase-requires-event discipline, the bounds, terminal-phase handling, the
snapshot rendering and its caps, the async nudge composer, the route layer's
session-identity gating, the MCP tools' strict-identity gate, and the LEGACY
``ledger/`` store's delete machinery (``purge_matching`` and its lock/inode
helpers, still the one spelling of removing one of those directories).

The record-to-fold round trip and the slot join across units are pinned in the
sibling ``test_session_ledger_projection.py``; this file cross-references those
rather than re-testing the fold's internals, and reaches the crew log through
the same fixtures that file established.

Two fixture families here, on purpose:

* ``_unit`` builds a real crew log for a slot and records through the public
  path, for the properties that live on the fold.
* the purge/lock tests build a LEGACY store by writing ``state.json`` and the
  ``slot_key`` breadcrumb on disk directly: nothing writes that store, and its
  delete machinery still has to hold for the residue on disk.
"""

from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger as sl
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.platform_compat import IS_POSIX

SESSION = "acp-1"
LATER_SESSION = "acp-2"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no writer state carried between tests.

    ``env -u KIROCREW_HOME`` is what the suite is run with; the fixture points
    the data home at a tmp path so a test never writes into the live one, and
    turns the crew log on because the ledger's authority is that log now.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    sl._fold_cache.clear()
    yield
    crew_log_emit.reset_caches()
    sl._fold_cache.clear()


def _unit(unit_id: str = SESSION, *, slot: str) -> None:
    """Create one session crew log for *slot*, then drop the handle.

    The emitter opens its own handle when the ledger appends, and a handle this
    test kept would own the write lease it needs (the pattern the projection
    suite's fixtures established).
    """
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _record(slot_key: str, *, session_id: str = SESSION, **fields: Any) -> dict[str, Any]:
    """``sl.record`` for *slot_key* on *session_id*, waited out — the fold-backed write.

    The flush is load-bearing in the ASYNC tests. ``record`` hands the entry to
    the crew log's writer and answers from the fold plus that pending entry, so it
    never has to wait; a caller that goes on to READ the record does, and inside a
    running event loop the writer defers instead of appending inline.
    """
    state = sl.record(slot_key, session_id=session_id, **fields)
    assert crew_log_emit.flush(timeout=5.0), "the crew log writer did not drain"
    return state


# ── key identity ──────────────────────────────────────────────────────────


def test_ledger_key_strips_dashboard_prefixes_only():
    assert sl.ledger_key("dashboard_chat-1-111") == "chat-1-111"
    assert sl.ledger_key("dashboard:chat-1-111") == "chat-1-111"
    assert sl.ledger_key("chat-1-111") == "chat-1-111"
    # Channel keys pass through UNTOUCHED — no charset fold.
    assert sl.ledger_key("slack:C123:456.789") == "slack:C123:456.789"


def test_distinct_channel_keys_never_share_a_ledger():
    """A lossy charset fold would map both of these onto one directory —
    one session reading and overwriting another's state."""
    a = sl.ledger_dir("wecom:agent:direct:user_gen1")
    b = sl.ledger_dir("wecom:agent:direct:user:gen1")
    assert a != b


def test_ledger_dir_distinct_per_exact_key():
    assert sl.ledger_dir("chat-1-111") != sl.ledger_dir("chat-1-112")
    # Case-insensitive-FS safety: Foo and foo must not share a directory.
    assert sl.ledger_dir("Foo") != sl.ledger_dir("foo")


def test_ledger_dir_stays_inside_root():
    root = sl._ledger_root().resolve()
    d = sl.ledger_dir("weird key with spaces and ünïcødé")
    assert d.is_relative_to(root)


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", "x\0y"])
def test_ledger_dir_refuses_hostile_keys(bad):
    with pytest.raises(ValueError):
        sl.ledger_dir(bad)


def test_long_key_dir_name_bounded():
    d = sl.ledger_dir("k" * 500)
    assert len(d.name) <= sl._STORE_NAME_READABLE_MAX + 1 + 8


# ── record / read ─────────────────────────────────────────────────────────


def test_record_roundtrip_and_partial_update():
    key = "chat-1-111"
    _unit(slot=key)
    _record(key, goal="ship the ledger", next_step="write tests")
    state = sl.read_state(key)
    assert state["goal"] == "ship the ledger"
    assert state["next"] == "write tests"
    assert state["created_at"]
    # Partial update: untouched fields keep their stored values.
    _record(key, next_step="run gates")
    state = sl.read_state(key)
    assert state["goal"] == "ship the ledger"
    assert state["next"] == "run gates"


def test_phase_change_requires_event_and_kind():
    key = "chat-2-222"
    _unit(slot=key)
    with pytest.raises(ValueError, match="requires an event"):
        _record(key, phase="implementing")
    with pytest.raises(ValueError, match="event_kind"):
        _record(key, phase="implementing", event="started")
    with pytest.raises(ValueError, match="event_kind"):
        _record(key, phase="implementing", event="started", event_kind="bogus")


def test_phase_and_event_ride_on_one_entry():
    """State and event share ONE appended entry: after any accepted phase change
    the single ``ledger/recorded`` line holds BOTH — there is no ordering in which
    a reader sees the phase moved while the event is missing. The old model made
    this true with one atomic state.json write; the fold makes it true by there
    being exactly one entry to see."""
    key = "chat-3-333"
    _unit(slot=key)
    _record(key, phase="implementing", event="started the fix", event_kind="phase")
    assert crew_log_emit.flush(timeout=5.0)
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    try:
        entries = [e for e in handle.iter_from(1) if e.type == sl.LEDGER_ENTRY_TYPE]
    finally:
        del handle
    assert len(entries) == 1
    data = entries[0].data
    assert data["phase"] == "implementing"
    assert data["event"] == "started the fix"
    assert data["event_kind"] == "phase"
    # And the fold reads both back off that one entry.
    state = sl.read_state(key)
    assert state["phase"] == "implementing"
    assert state["events"][-1]["text"] == "started the fix"
    assert state["events"][-1]["kind"] == "phase"


def test_terminal_phase_sets_finished_at_and_reopening_clears_it():
    key = "chat-4-444"
    _unit(slot=key)
    _record(key, phase="done", event="all green", event_kind="progress")
    assert sl.read_state(key)["finished_at"]
    _record(key, phase="implementing", event="reopened", event_kind="phase")
    assert sl.read_state(key)["finished_at"] == ""


def test_tried_appends_and_caps():
    key = "chat-5-555"
    _unit(slot=key)
    for i in range(sl._MAX_TRIED + 5):
        _record(key, tried_approach=f"approach {i}", tried_rejected_because="no")
    tried = sl.read_state(key)["tried"]
    assert len(tried) == sl._MAX_TRIED
    assert tried[-1]["approach"] == f"approach {sl._MAX_TRIED + 4}"
    assert tried[-1]["rejected_because"] == "no"


def test_events_tail_bounded():
    key = "chat-5-556"
    _unit(slot=key)
    for i in range(sl._MAX_EVENTS + 10):
        _record(key, event=f"event {i}", event_kind="progress")
    events = sl.read_state(key)["events"]
    assert len(events) == sl._MAX_EVENTS
    assert events[-1]["text"] == f"event {sl._MAX_EVENTS + 9}"


def test_artifacts_merge_and_clamp():
    key = "chat-6-666"
    _unit(slot=key)
    _record(key, artifacts={"branch": "feat/x"})
    _record(key, artifacts={"pr": "123"})
    arts = sl.read_state(key)["artifacts"]
    assert arts == {"branch": "feat/x", "pr": "123"}
    _record(key, goal="g" * 10_000)
    assert len(sl.read_state(key)["goal"]) == sl._MAX_TEXT


def test_updating_oldest_artifact_on_full_map_survives_the_cap():
    """A dict update keeps the key's ORIGINAL insertion position, so without the
    pop-before-reassign an update to the oldest pointer on a full map would age
    out the very artifact the call just wrote. Driven through the public write so
    it pins the property end to end; the fold-internal version is in the
    projection suite (``test_updating_the_oldest_artifact_does_not_age_it_out``)."""
    key = "chat-6-667"
    _unit(slot=key)
    for i in range(sl._MAX_ARTIFACTS):
        _record(key, artifacts={f"k{i}": "v"})
    # Map is full; update the OLDEST key and add one new key in the same call.
    _record(key, artifacts={"k0": "updated", "brand-new": "v"})
    arts = sl.read_state(key)["artifacts"]
    assert arts["k0"] == "updated"
    assert "brand-new" in arts
    assert len(arts) == sl._MAX_ARTIFACTS


def test_unknown_event_kind_without_phase_coerced_to_note():
    key = "chat-7-777"
    _unit(slot=key)
    _record(key, event="something happened", event_kind="bogus")
    assert sl.read_state(key)["events"][0]["kind"] == "note"


def test_read_state_of_a_slot_that_recorded_nothing_is_the_empty_record():
    """A slot with no crew log entries folds to the empty record, and never
    raises — a nudge cycle reads this on its way into a turn.

    The record is a fold rather than a file this function parses, so there is no
    malformed or over-ceiling FILE for it to refuse and zero out. The append-only crew
    log a
    reader folds cannot be read to nothing by a damaged line; a fold that cannot
    be made returns the empty record by contract, which the projection suite
    pins directly."""
    _unit(slot="chat-8-888")
    assert sl.read_state("chat-8-888") == sl._empty_state()
    assert sl.has_ledger("chat-8-888") is False
    # And a slot with no crew log at all is the same empty record, not an error.
    assert sl.read_state("chat-never-ran") == sl._empty_state()


def test_ledger_root_is_behind_the_agent_file_gate():
    """The ledger's authorization model is session-scoped routes; the agent
    file-tool gate must fence the on-disk subtree or any session could read
    another's ledger sideways. Home-anchored like every matcher in that list,
    so probe with home-relative spellings."""
    from pathlib import Path

    from kiro_crew.security import is_sensitive_path

    home = Path.home()
    assert is_sensitive_path(str(home / ".kiro/crew/ledger/chat-1-abc12345/state.json"))
    assert is_sensitive_path(str(home / ".kirocrew/ledger/x-deadbeef/state.json"))


# ── record refusals ───────────────────────────────────────────────────────


def test_record_refuses_when_the_session_has_no_crew_log():
    """The update has nowhere to go, and a write that went nowhere is the one
    outcome a durable record must never produce."""
    with pytest.raises(sl.LedgerUnavailable, match="no crew log"):
        _record("chat-nolog-1", goal="g")


def test_record_refuses_when_the_crew_log_is_switched_off(monkeypatch):
    key = "chat-off-1"
    _unit(slot=key)
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    crew_log_emit.reset_caches()
    with pytest.raises(sl.LedgerUnavailable, match="KIROCREW_CREW_LOG"):
        _record(key, goal="g")


def test_record_refuses_a_session_with_no_live_acp_unit():
    """An update filed under a guessed session is worse than one that is refused."""
    key = "chat-nosid-1"
    _unit(slot=key)
    with pytest.raises(sl.LedgerUnavailable, match="no live crew log"):
        sl.record(key, session_id="", goal="g")


def test_record_refuses_an_empty_slot_key():
    _unit(slot="chat-emptykey")
    with pytest.raises(ValueError, match="Invalid slot key"):
        sl.record("", session_id=SESSION, goal="g")


# ── snapshot rendering ────────────────────────────────────────────────────


def test_snapshot_empty_without_ledger_or_when_terminal():
    assert sl.render_snapshot("no-such-session") == ""
    key = "chat-11-111"
    _unit(slot=key)
    _record(key, goal="g", phase="done", event="done", event_kind="progress")
    assert sl.render_snapshot(key) == ""


def test_snapshot_includes_artifact_only_ledger():
    key = "chat-11-222"
    _unit(slot=key)
    _record(key, artifacts={"branch": "feat/x"})
    snap = sl.render_snapshot(key)
    assert "feat/x" in snap


def test_snapshot_contains_state_and_is_capped():
    key = "chat-12-222"
    _unit(slot=key)
    _record(
        key,
        goal="ship it",
        phase="implementing",
        next_step="fix the test",
        event="e",
        event_kind="phase",
        artifacts={"branch": "feat/x"},
        tried_approach="approach A",
        tried_rejected_because="too slow",
    )
    snap = sl.render_snapshot(key)
    assert snap.startswith("[work ledger")
    for needle in (
        "ship it",
        "implementing",
        "fix the test",
        "feat/x",
        "approach A",
        "too slow",
    ):
        assert needle in snap
    # Cap holds even against a clamped-but-full record.
    for i in range(10):
        _record(
            key,
            tried_approach=("x" * sl._MAX_TEXT),
            tried_rejected_because="y" * 500,
        )
    assert len(sl.render_snapshot(key)) <= sl._SNAPSHOT_MAX_CHARS


# ── nudge composer integration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_nudge_body_prefixes_snapshot():
    from kiro_crew.dashboard.handlers.autonudge import compose_nudge_body

    slot_key = "chat-13-333"
    _unit(slot=sl.ledger_key(slot_key))
    _record(sl.ledger_key(slot_key), goal="babysit PR 42", next_step="check CI")
    out = await compose_nudge_body("check {{STOP_FILE}}", "/x/.stop", slot_key)
    assert out.startswith("[work ledger")
    assert "babysit PR 42" in out
    assert out.endswith("check /x/.stop")


@pytest.mark.asyncio
async def test_compose_nudge_body_folds_key_like_the_write_path():
    """A ledger written under the route's fold of `dashboard_chat-X` must be the
    one a loop keyed `chat-X` reads — both fold to the same slot, so both resolve
    to the same crew log units. Changing either side's fold breaks this pairing."""
    from kiro_crew.dashboard.handlers.autonudge import compose_nudge_body

    written_key = sl.ledger_key("dashboard_chat-15-555")
    _unit(slot=written_key)
    _record(written_key, goal="paired")
    out = await compose_nudge_body("m", None, "chat-15-555")
    assert "paired" in out


@pytest.mark.asyncio
async def test_compose_nudge_body_unchanged_without_ledger():
    from kiro_crew.dashboard.handlers.autonudge import compose_nudge_body

    assert await compose_nudge_body("m {{STOP_FILE}}", "/x", "chat-none-1") == "m /x"
    assert await compose_nudge_body("m {{STOP_FILE}}", "/x", None) == "m /x"


@pytest.mark.asyncio
async def test_compose_nudge_body_survives_snapshot_failure(monkeypatch):
    from kiro_crew.dashboard.handlers.autonudge import compose_nudge_body

    monkeypatch.setattr(sl, "render_snapshot", MagicMock(side_effect=RuntimeError("boom")))
    assert await compose_nudge_body("m", None, "chat-14-444") == "m"


def _fire_adapter_bodies(src: str) -> dict[str, str]:
    """Map each ``_fire_*_nudge`` adapter name to its own body text.

    Split rather than matched so a body is attributed to the adapter it belongs
    to and cannot borrow the next one's lines.
    """
    parts = re.split(r"\n    async def (?=_fire_\w+_nudge\()", "\n" + src.lstrip("\n"))[1:]
    return {p.split("(", 1)[0]: p for p in parts}


def _composer_offenders(adapters: dict[str, str]) -> list[str]:
    """Adapters that never reach ``compose_nudge_body``, named in sorted order.

    An adapter reaches it directly, or by delegating ONE hop to a sibling that
    reaches it directly. One hop is deliberate: a longer chain would let two
    adapters forward to each other and satisfy the rule without either ever
    composing.
    """
    direct = {name for name, body in adapters.items() if "compose_nudge_body" in body}
    offenders = []
    for name, body in adapters.items():
        if name in direct:
            continue
        if any(f"self.{target}(" in body for target in direct):
            continue
        offenders.append(name)
    return sorted(offenders)


def test_gateway_fire_callbacks_use_the_composer():
    """EVERY fire path must reach compose_nudge_body -- reverting a call site to
    the snapshot-less render_nudge_message drops ledger injection for that
    surface silently.

    Enumerated rather than counted. A hardcoded total says "3" until a channel is
    added, and then it fails for the one reason that is NOT a defect (a new
    adapter) while a channel that quietly opted itself out could keep the total
    correct by existing. Naming the offenders also tells whoever broke it which
    surface lost its ledger.

    Reaching the composer counts whether an adapter calls it itself or delegates
    to a shared fire path that does, because a channel that hands its whole turn
    to a spine gets the snapshot from the spine. The delegation is resolved ONE
    hop and only onto a target that calls the composer DIRECTLY, so a chain of
    adapters forwarding to each other can never satisfy this by passing the
    obligation around. An adapter that neither composes nor delegates is still an
    offender, which is what ``test_the_composer_ratchet_is_not_vacuous`` pins.
    """
    from kiro_crew.slack import gateway

    src = inspect.getsource(gateway)
    adapters = _fire_adapter_bodies(src)
    assert adapters, "no _fire_*_nudge adapters found -- this pattern went stale"

    offenders = _composer_offenders(adapters)
    assert not offenders, (
        "these fire adapters neither call compose_nudge_body nor delegate to a "
        "fire path that does, so their surface's loops start each cycle without "
        f"the work-ledger snapshot: {offenders}"
    )


def test_the_composer_ratchet_is_not_vacuous():
    """The allowance above must not let a real opt-out through.

    Three shapes are checked against the same helpers the ratchet uses: an
    adapter that composes directly passes, one that delegates to a spine which
    composes passes, and one that does neither is named. The fourth shape is the
    one the one-hop rule exists for: two adapters that only forward to each other
    never reach the composer, so both are named rather than excusing each other.
    """
    composes = """
    async def _fire_alpha_nudge(self, loop):
        body = await compose_nudge_body(loop.message, None, loop.slot_key)
        return True
"""
    spine = """
    async def _fire_dm_nudge(self, loop, adapter):
        body = await compose_nudge_body(loop.message, None, loop.slot_key)
        return True
"""
    delegates = """
    async def _fire_beta_nudge(self, loop):
        return await self._fire_dm_nudge(loop, _adapter())
"""
    opts_out = """
    async def _fire_gamma_nudge(self, loop):
        return await self._client.send(render_nudge_message(loop.message))
"""
    circular = """
    async def _fire_delta_nudge(self, loop):
        return await self._fire_epsilon_nudge(loop)

    async def _fire_epsilon_nudge(self, loop):
        return await self._fire_delta_nudge(loop)
"""

    assert _composer_offenders(_fire_adapter_bodies(composes + spine + delegates)) == []
    assert _composer_offenders(_fire_adapter_bodies(composes + spine + opts_out)) == [
        "_fire_gamma_nudge"
    ]
    assert _composer_offenders(_fire_adapter_bodies(spine + circular)) == [
        "_fire_delta_nudge",
        "_fire_epsilon_nudge",
    ]


# ── HTTP routes ───────────────────────────────────────────────────────────


def _mk_request(method: str, path: str, *, body: Any = ..., sk: str = "chat-r-1") -> web.Request:
    app = web.Application()
    state = MagicMock()
    # The record route resolves the calling session's crew log unit through
    # ``crew_log.resolve.unit_for_session_key(state.sessions, sk)``, which reads
    # ``sessions.get_provider(sk)`` and then the provider's own ``session_id``. A
    # bare MagicMock will not do: the resolver requires that attribute to be a
    # non-empty STRING and reads an unset one as "no live ACP session", so the
    # route would answer 409 for a reason this suite is not testing.
    state.sessions.get_provider = MagicMock(return_value=SimpleNamespace(session_id=SESSION))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


@pytest.fixture()
def _open_route(monkeypatch):
    """Bypass session recognition/restriction (their own suites cover them)."""
    from kiro_crew.dashboard.handlers import session_ledger as routes

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    return routes


@pytest.mark.asyncio
async def test_route_record_and_get_roundtrip(_open_route):
    routes = _open_route
    _unit(slot=sl.ledger_key("chat-r-1"))
    req = _mk_request(
        "POST",
        "/api/session-ledger/record",
        body={"goal": "route goal", "next": "route next"},
    )
    resp = await routes.api_session_ledger_record(req)
    assert resp.status == 200
    resp2 = await routes.api_session_ledger_get(_mk_request("GET", "/api/session-ledger"))
    data = json.loads(resp2.text)
    assert data["state"]["goal"] == "route goal"


@pytest.mark.asyncio
async def test_route_phase_without_event_is_400(_open_route):
    routes = _open_route
    _unit(slot=sl.ledger_key("chat-r-1"))
    req = _mk_request("POST", "/api/session-ledger/record", body={"phase": "implementing"})
    resp = await routes.api_session_ledger_record(req)
    assert resp.status == 400
    assert "event" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_route_rejects_non_string_artifacts(_open_route):
    routes = _open_route
    _unit(slot=sl.ledger_key("chat-r-1"))
    req = _mk_request("POST", "/api/session-ledger/record", body={"artifacts": {"pr": 123}})
    resp = await routes.api_session_ledger_record(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_route_record_without_a_crew_log_is_409(_open_route):
    """The request is well formed but the calling session has no crew log to
    append to — the route answers 409 ``crew_log_unavailable`` (the same answer
    the crew log's own reads give for a log this build cannot serve), never a
    silent success."""
    routes = _open_route
    # No _unit(): the resolved session id names no crew log.
    req = _mk_request("POST", "/api/session-ledger/record", body={"goal": "x"})
    resp = await routes.api_session_ledger_record(req)
    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "crew_log_unavailable"


@pytest.mark.asyncio
async def test_route_refuses_unrecognized_session(monkeypatch):
    from kiro_crew.dashboard.handlers import session_ledger as routes

    async def _refused(*a: Any, **k: Any) -> web.Response:
        return web.json_response({"error": "unknown session"}, status=403)

    monkeypatch.setattr(routes, "_recognize_session", _refused)
    resp = await routes.api_session_ledger_record(
        _mk_request("POST", "/api/session-ledger/record", body={"goal": "x"})
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_refuses_restricted_session(monkeypatch, _open_route):
    routes = _open_route
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: True)
    resp = await routes.api_session_ledger_record(
        _mk_request("POST", "/api/session-ledger/record", body={"goal": "x"})
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_write_lands_under_ledger_key(_open_route):
    """The route folds the header key exactly like the nudge composer does —
    losslessly, dashboard prefixes only — and the write lands in the crew log the
    calling session is serving on, read back through the fold under the folded
    key."""
    routes = _open_route
    sk = "dashboard_chat-77-999"
    _unit(slot=sl.ledger_key(sk))
    req = _mk_request("POST", "/api/session-ledger/record", body={"goal": "fold me"}, sk=sk)
    assert (await routes.api_session_ledger_record(req)).status == 200
    assert sl.read_state(sl.ledger_key(sk))["goal"] == "fold me"


def test_routes_are_on_the_strict_internal_allowlist():
    """The tools authenticate with the internal secret; without this entry the
    call falls through to cookie auth and every tool call 403s."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-ledger" in _STRICT_INTERNAL_API_PATHS


# ── legacy ledger/ store: permanent-delete preservation boundary ───────────
#
# The purge machinery removes a LEGACY ``ledger/<store>`` directory — residue on
# disk that ``kirocrew ledger-sweep`` collects and nothing writes. So these
# fixtures build one directly on
# disk: the ``state.json`` document plus the ``slot_key`` breadcrumb that lets a
# purge NAME the store, exactly what ``ledger_dir`` + ``_store_name`` expect.


def _legacy_store(key: str, *, phase: str = "done") -> Any:
    """Build a legacy ``ledger/`` store for *key* on disk (no ``record`` call).

    Writes the two identity files a purge keys on, and the lock inode it takes:
    ``purge_matching`` opens that file WITHOUT creating it, deliberately — a writer
    may bring a store into being by locking it and a deleter must not — so a
    fixture without one is skipped as a store that is already gone. ``record`` no
    longer produces this directory, so the delete machinery's fixtures build it.
    """
    directory = sl.ledger_dir(key)
    directory.mkdir(parents=True, exist_ok=True)
    state = sl._empty_state()
    state["phase"] = phase
    (directory / sl._STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    (directory / sl._KEY_FILE).write_text(key, encoding="utf-8")
    (directory / sl._LOCK_FILE).touch()
    return directory


def test_purge_matching_tolerates_bad_and_absent_keys():
    """The one delete primitive: a hostile or unknown key removes nothing and never
    raises."""
    _legacy_store("chat-10-000")
    assert sl.purge_matching({"", "a/b", "chat-never"}, guard=lambda _d: True) == 0
    assert sl.ledger_dir("chat-10-000").exists()


@pytest.mark.asyncio
async def test_remove_slot_for_history_key_preserves_ledger():
    from kiro_crew.dashboard.handlers.sessions import _remove_slot_for_history_key

    history_key = "dashboard_chat-88-123"
    ledger_key = sl.ledger_key(history_key)
    directory = _legacy_store(ledger_key)
    assert directory.exists()

    state = MagicMock()
    state._slots = {}
    await _remove_slot_for_history_key(state, history_key)
    assert directory.exists()


@pytest.mark.asyncio
async def test_delete_with_folded_spelling_preserves_exact_channel_key_ledger():
    """A lossy transcript spelling never authorizes work-ledger deletion."""
    from kiro_crew.dashboard.handlers.sessions import _remove_slot_for_history_key
    from kiro_crew.dashboard.state import _normalize_slot_key

    channel_key = "slack:C042:1712793600.123456"
    directory = _legacy_store(channel_key)
    assert directory.exists()

    state = MagicMock()
    state._slots = {}
    # The route receives only the folded transcript spelling; that lossy alias
    # does not authorize deletion of the exact channel ledger.
    await _remove_slot_for_history_key(state, _normalize_slot_key(channel_key))
    assert directory.exists()


@pytest.mark.skipif(not IS_POSIX, reason="symlink creation needs no privilege on POSIX")
def test_purge_matching_never_follows_a_linked_store_or_a_linked_entry(tmp_path):
    """A linked store directory is skipped whatever its breadcrumb says, and a
    linked entry INSIDE a store is unlinked as a name, never walked."""
    import shutil

    # A whole store that is a link: skipped, target untouched.
    key = "chat-60-linked-store"
    directory = _legacy_store(key)
    target = tmp_path / "store-target"
    shutil.move(str(directory), str(target))
    directory.symlink_to(target, target_is_directory=True)
    assert sl.purge_matching({key}, guard=lambda _d: True) == 0
    assert (target / "state.json").exists()

    # A linked entry inside a real store: the link goes, the target stays.
    key2 = "chat-61-linked-entry"
    directory2 = _legacy_store(key2)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep", encoding="utf-8")
    (directory2 / "attachments").symlink_to(elsewhere, target_is_directory=True)
    assert sl.purge_matching({key2}, guard=lambda _d: True) == 1
    assert not directory2.exists()
    assert (elsewhere / "precious.txt").read_text(encoding="utf-8") == "keep"


def test_purge_matching_is_exact_and_a_folded_spelling_matches_nothing():
    """The primitive matches exact keys only: the folded spelling of a channel key
    names no ledger, so a lossy alias can never remove one the caller did not
    list by its exact key."""
    from kiro_crew.dashboard.state import _normalize_slot_key

    _legacy_store("slack:C1:1.1")
    _legacy_store("slack:C2:2.2")
    _legacy_store("chat-keep-1")
    removed = sl.purge_matching(
        {"slack:C1:1.1", _normalize_slot_key("slack:C2:2.2")}, guard=lambda _d: True
    )
    assert removed == 1
    assert not sl.ledger_dir("slack:C1:1.1").exists()
    assert sl.ledger_dir("slack:C2:2.2").exists(), "a folded spelling is not the exact key"
    assert sl.ledger_dir("chat-keep-1").exists()


def test_guarded_purge_keeps_the_record_and_breadcrumb_when_any_content_survives(
    monkeypatch,
):
    """Removal is ordered and the identity files go LAST, only once everything
    else is gone -- so a failed removal (a Windows sharing violation on one held
    entry) leaves a store that still has its record and still names itself,
    never a residue no purge can address."""
    key = "chat-30-held"
    directory = _legacy_store(key)
    (directory / "stray.bin").write_bytes(b"held by another handle")

    import pathlib

    original = pathlib.Path.unlink

    def _refuse_stray(self, *args, **kwargs):
        if self.name == "stray.bin":
            raise PermissionError(32, "sharing violation")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _refuse_stray)

    removed = sl.purge_matching({key}, guard=lambda _d: True)

    assert removed == 0, "a store that could not be fully removed is not counted"
    assert (directory / "state.json").exists(), "the record must survive"
    assert (directory / "slot_key").exists(), "the breadcrumb must survive"


def test_guarded_purge_counts_rmtree_failures_instead_of_ignoring_them(monkeypatch):
    key = "chat-31-subtree"
    directory = _legacy_store(key)
    (directory / "attachments").mkdir()
    (directory / "attachments" / "a.bin").write_bytes(b"x")

    def _refuse(path, *args, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror is not None:
            onerror(None, str(path), None)
            return None
        return real_rmtree(path, *args, **kwargs)

    real_rmtree = sl.shutil.rmtree
    monkeypatch.setattr(sl.shutil, "rmtree", _refuse)

    with sl._locked(directory):
        assert sl._remove_store_contents(directory) is False
    assert (directory / "state.json").exists()
    assert (directory / "slot_key").exists()


@pytest.mark.skipif(not IS_POSIX, reason="the detached-inode shape needs POSIX unlink semantics")
def test_a_record_queued_behind_a_purge_refuses_instead_of_publishing():
    """A writer that acquires ``_locked`` on an inode the store does not have must
    refuse, not write a breadcrumb-less state into a recreated directory."""
    key = "chat-40-purged-under-me"
    directory = _legacy_store(key)
    lock_path = directory / ".lock"
    lock_path.touch()
    import shutil

    fd = __import__("os").open(str(lock_path), __import__("os").O_RDWR)
    try:
        shutil.rmtree(directory)  # the purge completes while this writer holds a stale fd
        with pytest.raises(OSError, match="removed while waiting"):
            sl.require_lock_inode(fd, lock_path)
    finally:
        __import__("os").close(fd)
    # The store is gone and stays gone: nothing was published into it.
    assert not directory.exists()


def test_locked_runs_the_inode_check_inside_the_hold(monkeypatch):
    seen: list[str] = []
    real = sl.require_lock_inode

    def _spy(fd, path):
        seen.append(path.name)
        return real(fd, path)

    monkeypatch.setattr(sl, "require_lock_inode", _spy)
    # ``_locked`` on a fresh directory creates the store and takes its lock; the
    # inode check runs inside that hold.
    with sl._locked(sl.ledger_dir("chat-41-checked")):
        pass
    assert seen == [".lock"]


@pytest.mark.skipif(not IS_POSIX, reason="in-hold unlink is the POSIX path; Windows refuses it")
def test_the_lock_inode_is_unlinked_inside_the_hold(monkeypatch):
    """Unlinked INSIDE the hold, a queued writer that acquires the old inode finds
    its path gone and refuses. Unlinked after release there is a window where it
    acquires, validates against a path that still exists, proceeds, and the late
    unlink detaches the inode it holds -- so the next writer gets a second inode
    and the two are not serialised."""
    key = "chat-50-inhold"
    directory = _legacy_store(key)
    lock_path = directory / ".lock"

    gone_while_held: list[bool] = []
    real_release = sl.release_lock

    def _observe_then_release(fd):
        # Observed on the way OUT of the hold: the lock path must already be gone.
        gone_while_held.append(not lock_path.exists())
        return real_release(fd)

    monkeypatch.setattr(sl, "release_lock", _observe_then_release)

    assert sl.purge_matching({key}, guard=lambda _d: True) == 1
    assert gone_while_held == [True], "the lock inode must be unlinked before release"
    assert not directory.exists()


def test_purge_matching_succeeds_under_windows_unlink_rules(monkeypatch):
    """Windows: a handle has no FILE_SHARE_DELETE, so unlinking a held lock raises.
    The store's lock must therefore be unlinked only after its descriptor is
    closed -- by the shell, after release -- or the directory stays non-empty and
    the purge reports nothing removed over a store it already emptied. Simulated
    portably by making every lock-file unlink raise while its fd is open; the
    work half has the same test, and its earlier version was blind to one of the
    two lock handles."""
    import os
    from pathlib import Path

    key = "chat-52-windows"
    directory = _legacy_store(key)

    open_locks: set[Path] = set()
    fd_paths: dict[int, Path] = {}
    real_os_open, real_close, real_unlink = os.open, os.close, Path.unlink

    def _tracking_open(path, flags, *args, **kwargs):
        fd = real_os_open(path, flags, *args, **kwargs)
        p = Path(path)
        if p.name.endswith(".lock"):
            fd_paths[fd] = p
            open_locks.add(p.resolve())
        return fd

    def _tracking_close(fd):
        p = fd_paths.pop(fd, None)
        if p is not None:
            open_locks.discard(p.resolve())
        return real_close(fd)

    def _windows_unlink(self, *args, **kwargs):
        if self.name.endswith(".lock") and self.resolve() in open_locks:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "close", _tracking_close)
    monkeypatch.setattr(Path, "unlink", _windows_unlink)

    assert sl.purge_matching({key}, guard=lambda _d: True) == 1
    assert not directory.exists()
    assert not open_locks, "every lock handle was closed"


def test_purge_matching_with_an_accept_all_guard_is_still_locked_and_ordered(monkeypatch):
    """There is one spelling of deletion: an accept-all guard means the match alone
    decides, under the same hold and the same identity-last ordering. A refused
    stray entry therefore keeps the record and breadcrumb exactly as it does with
    a selective guard."""
    key = "chat-51-noguard"
    directory = _legacy_store(key)
    (directory / "stray.bin").write_bytes(b"held")
    import pathlib

    original = pathlib.Path.unlink

    def _refuse_stray(self, *args, **kwargs):
        if self.name == "stray.bin":
            raise PermissionError(32, "sharing violation")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _refuse_stray)
    seen: list[str] = []
    real = sl.require_lock_inode
    monkeypatch.setattr(
        sl, "require_lock_inode", lambda fd, path: (seen.append(path.name), real(fd, path))[1]
    )

    assert sl.purge_matching({key}, guard=lambda _d: True) == 0
    assert seen == [".lock"], "the removal ran under the ledger lock"
    assert (directory / "state.json").exists() and (directory / "slot_key").exists()


@pytest.mark.skipif(not IS_POSIX, reason="in-hold unlink is the POSIX path")
def test_the_post_release_shell_never_touches_a_lock_the_hold_already_removed():
    """After the in-hold unlink a refused writer may retry, rebuild the store and
    take a FRESH lock at the same path. The post-release shell must not unlink
    that: it would detach the fresh inode under its holder and hand the next
    writer a third one, un-serialised against the second."""
    import os

    key = "chat-52-rebuilt"
    directory = _legacy_store(key)
    lock_path = directory / ".lock"
    lock_path.touch()

    # Simulate the race: the hold removed the lock (lock_gone=True) and, before the
    # shell runs, a writer rebuilt the store with a fresh lock inode.
    lock_path.unlink()
    lock_path.touch()
    fresh = os.stat(lock_path).st_ino

    sl._remove_store_shell(directory, lock_gone=True)

    assert lock_path.exists(), "a fresh lock at the path is not ours to remove"
    assert os.stat(lock_path).st_ino == fresh
    # And the Windows-shaped case, where the hold could NOT unlink: the shell may.
    sl._remove_store_shell(directory, lock_gone=False)
    assert not lock_path.exists()


def test_a_partial_identity_removal_is_logged_accurately(monkeypatch, caplog):
    """If ``state.json`` went and only ``slot_key`` refused, the log must not claim
    both were kept -- an operator reading it would look for a record that is gone."""
    import logging
    import pathlib

    key = "chat-53-partial"
    directory = _legacy_store(key)
    original = pathlib.Path.unlink

    def _refuse_breadcrumb(self, *args, **kwargs):
        if self.name == "slot_key":
            raise PermissionError(32, "sharing violation")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _refuse_breadcrumb)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.session_ledger"):
        assert sl.purge_matching({key}, guard=lambda _d: True) == 0

    assert not (directory / "state.json").exists()
    assert (directory / "slot_key").exists()
    text = caplog.text
    assert "kept: slot_key" in text
    assert "state.json and slot_key kept" not in text


def test_a_purge_racing_a_finished_purge_recreates_nothing(monkeypatch):
    """Same property on the session half: a store removed between the listing and
    the purge's lock is skipped, never rebuilt as a lock-only directory."""
    import shutil

    key = "chat-54-raced"
    directory = _legacy_store(key)
    real_locked = sl._locked

    def _first_sweep_wins(dir_path, **kwargs):
        shutil.rmtree(dir_path)  # the other sweep finished just before our acquire
        return real_locked(dir_path, **kwargs)

    monkeypatch.setattr(sl, "_locked", _first_sweep_wins)

    assert sl.purge_matching({key}, guard=lambda _d: True) == 0
    assert not directory.exists(), "the purge must not recreate the store it found gone"


def test_a_writer_lock_still_creates_the_store():
    directory = sl.ledger_dir("chat-55-fresh")
    assert not directory.exists()
    with sl._locked(directory):
        assert (directory / ".lock").exists()


# ── MCP tool identity ─────────────────────────────────────────────────────


def test_mcp_tools_refuse_without_strict_identity(monkeypatch):
    """A subagent's lenient PID-walk identity resolves to the PARENT session;
    the tools must refuse rather than read/write the parent's ledger."""
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import ledger as tools

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    transport = MagicMock()
    monkeypatch.setattr(mcp_core, "_get", transport)
    monkeypatch.setattr(mcp_core, "_post", transport)
    assert "could not be verified" in tools.session_ledger_read("x", {})
    assert "could not be verified" in tools.session_ledger_record("x", {"goal": "g"})
    transport.assert_not_called()


def test_mcp_tools_pass_the_verified_key_to_transport(monkeypatch):
    """The key that was CHECKED must be the key that is USED — the transport
    must not re-resolve leniently."""
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import ledger as tools

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-v-1")
    get = MagicMock(return_value={"state": {}, "events": []})
    post = MagicMock(return_value={"ok": True, "state": {"phase": "", "next": ""}})
    monkeypatch.setattr(mcp_core, "_get", get)
    monkeypatch.setattr(mcp_core, "_post", post)
    tools.session_ledger_read("x", {})
    get.assert_called_once_with("/api/session-ledger", session_key="chat-v-1")
    tools.session_ledger_record("x", {"goal": "g"})
    post.assert_called_once_with(
        "/api/session-ledger/record", {"goal": "g"}, session_key="chat-v-1"
    )
