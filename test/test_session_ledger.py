"""Session work ledger — core primitive, nudge injection, routes, cleanup.

Covers the contracts docs/system-specs/modules/session-work-ledger.md pins:
exact-key identity (lossless fold, no channel-key collisions), directory
guarding, the crash-atomic phase-requires-event discipline, partial updates
preserving stored state, bounds (tried/events/artifacts/state-file size), the
bounded cross-process lock, snapshot rendering and its caps, the async nudge
composer, the route layer's session-identity gating, and the permanent-delete
purge.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import session_ledger as sl
from kiro_crew.platform_compat import IS_POSIX


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


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
    sl.record(key, goal="ship the ledger", next_step="write tests")
    state = sl.read_state(key)
    assert state["goal"] == "ship the ledger"
    assert state["next"] == "write tests"
    assert state["created_at"]
    # Partial update: untouched fields keep their stored values.
    sl.record(key, next_step="run gates")
    state = sl.read_state(key)
    assert state["goal"] == "ship the ledger"
    assert state["next"] == "run gates"
    # Breadcrumb maps the folded dir name back to the key.
    assert (sl.ledger_dir(key) / "slot_key").read_text().strip() == key


def test_phase_change_requires_event_and_kind():
    with pytest.raises(ValueError, match="requires an event"):
        sl.record("chat-2-222", phase="implementing")
    with pytest.raises(ValueError, match="event_kind"):
        sl.record("chat-2-222", phase="implementing", event="started")
    with pytest.raises(ValueError, match="event_kind"):
        sl.record("chat-2-222", phase="implementing", event="started", event_kind="bogus")


def test_phase_and_event_land_in_one_document():
    """State and event share one atomic write: after any accepted phase
    change, the on-disk document holds BOTH — there is no observable state
    where the phase moved and the event is missing."""
    key = "chat-3-333"
    sl.record(key, phase="implementing", event="started the fix", event_kind="phase")
    raw = json.loads((sl.ledger_dir(key) / "state.json").read_text())
    assert raw["phase"] == "implementing"
    assert raw["events"][-1]["text"] == "started the fix"
    assert raw["events"][-1]["kind"] == "phase"


def test_terminal_phase_sets_finished_at_and_reopening_clears_it():
    key = "chat-4-444"
    sl.record(key, phase="done", event="all green", event_kind="progress")
    assert sl.read_state(key)["finished_at"]
    sl.record(key, phase="implementing", event="reopened", event_kind="phase")
    assert sl.read_state(key)["finished_at"] == ""


def test_tried_appends_and_caps():
    key = "chat-5-555"
    for i in range(sl._MAX_TRIED + 5):
        sl.record(key, tried_approach=f"approach {i}", tried_rejected_because="no")
    tried = sl.read_state(key)["tried"]
    assert len(tried) == sl._MAX_TRIED
    assert tried[-1]["approach"] == f"approach {sl._MAX_TRIED + 4}"
    assert tried[-1]["rejected_because"] == "no"


def test_events_tail_bounded():
    key = "chat-5-556"
    for i in range(sl._MAX_EVENTS + 10):
        sl.record(key, event=f"event {i}", event_kind="progress")
    events = sl.read_state(key)["events"]
    assert len(events) == sl._MAX_EVENTS
    assert events[-1]["text"] == f"event {sl._MAX_EVENTS + 9}"


def test_artifacts_merge_and_clamp():
    key = "chat-6-666"
    sl.record(key, artifacts={"branch": "feat/x"})
    sl.record(key, artifacts={"pr": "123"})
    arts = sl.read_state(key)["artifacts"]
    assert arts == {"branch": "feat/x", "pr": "123"}
    sl.record(key, goal="g" * 10_000)
    assert len(sl.read_state(key)["goal"]) == sl._MAX_TEXT


def test_updating_oldest_artifact_on_full_map_survives_the_cap():
    """A dict update keeps the key's original insertion position, so without
    the pop-before-reassign an update to the oldest pointer on a full map
    would age out the very artifact the call just wrote."""
    key = "chat-6-667"
    for i in range(sl._MAX_ARTIFACTS):
        sl.record(key, artifacts={f"k{i}": "v"})
    # Map is full; update the OLDEST key and add one new key in the same call.
    sl.record(key, artifacts={"k0": "updated", "brand-new": "v"})
    arts = sl.read_state(key)["artifacts"]
    assert arts["k0"] == "updated"
    assert "brand-new" in arts
    assert len(arts) == sl._MAX_ARTIFACTS


def test_unknown_event_kind_without_phase_coerced_to_note():
    key = "chat-7-777"
    sl.record(key, event="something happened", event_kind="bogus")
    assert sl.read_state(key)["events"][0]["kind"] == "note"


def test_read_state_malformed_oversized_or_undecodable_reads_empty():
    key = "chat-9-999"
    sl.record(key, goal="x")
    path = sl.ledger_dir(key) / "state.json"
    path.write_text("{broken", encoding="utf-8")
    assert sl.read_state(key)["goal"] == ""
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert sl.read_state(key)["goal"] == ""
    # A file past the size ceiling is refused BEFORE parsing.
    path.write_text(json.dumps({"goal": "big", "junk": "j" * sl._MAX_STATE_BYTES}))
    assert sl.read_state(key)["goal"] == ""


def test_writer_guarantees_the_read_ceiling_for_legitimate_records():
    """An accepted record must never produce a file its own reader zeroes.

    Worst legitimate case: clamped-but-full events of astral-plane characters
    (4 bytes each in UTF-8; 12 each if escaped). The writer serializes UTF-8
    and evicts the oldest history until the document fits, so the state
    survives and reads back intact."""
    key = "chat-9-998"
    glyph = "\N{PILE OF POO}" * sl._MAX_TEXT  # clamps to _MAX_TEXT astral chars
    for i in range(sl._MAX_EVENTS):
        sl.record(key, event=glyph, event_kind="progress")
    sl.record(key, goal="still here", next_step="keep going")
    size = (sl.ledger_dir(key) / "state.json").stat().st_size
    assert size <= sl._MAX_STATE_BYTES
    state = sl.read_state(key)
    assert state["goal"] == "still here"  # NOT zeroed by the ceiling
    assert state["events"]  # history trimmed, not destroyed


def test_oversized_unknown_fields_are_dropped_not_self_corrupting():
    """Unknown fields are unclamped forward-compat baggage; when history
    eviction cannot bring the document under the budget, they are dropped
    rather than written past the ceiling (which would make the next read
    discard the whole ledger, known state included)."""
    key = "chat-9-997"
    sl.record(key, goal="protect me")
    path = sl.ledger_dir(key) / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["future_blob"] = "z" * (sl._MAX_STATE_BYTES - 2_000)  # parses, but over the write budget
    path.write_text(json.dumps(raw), encoding="utf-8")
    # The next record must neither fail nor write an over-ceiling document.
    sl.record(key, next_step="still writable")
    assert (sl.ledger_dir(key) / "state.json").stat().st_size <= sl._MAX_STATE_BYTES
    state = sl.read_state(key)
    assert state["goal"] == "protect me"
    assert state["next"] == "still writable"
    assert "future_blob" not in state


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


def test_coerce_preserves_unknown_fields():
    raw = {"goal": "g", "future_field": {"a": 1}, "tried": "wrong-type"}
    state = sl._coerce_state(raw)
    assert state["goal"] == "g"
    assert state["future_field"] == {"a": 1}
    assert state["tried"] == []


# -- the bounded write reports what it discards -----------------------------


def test_record_returns_exactly_what_landed_on_disk(monkeypatch):
    """``record``'s return value is the authoritative post-write view.

    The dashboard handler puts it straight into ``{"ok": True, "state": ...}``
    and the MCP tool reports it as "what is now recorded", so it must not
    describe entries the write budget evicted. ``_serialize_bounded`` evicts
    from the very dict ``record`` returns, which is what keeps the two equal;
    serializing a copy instead would return the pre-eviction lists while disk
    held the evicted ones. Pinned so that aliasing is not "cleaned up" later.
    """
    key = "chat-62-90a"
    sl.record(key, goal="keep me", event="seed", event_kind="progress")
    for i in range(4):
        sl.record(key, tried_approach=f"a{i}", tried_rejected_because="x" * 400)
        sl.record(key, event=f"e{i}" + "y" * 400, event_kind="progress")
    path = sl.ledger_dir(key) / sl._STATE_FILE
    before = json.loads(path.read_text(encoding="utf-8"))

    # Squeeze the ceiling so the budget lands just UNDER the current document:
    # the next write must evict, while the read that precedes it still passes
    # the same ceiling's file-size guard. Derived from the real size so a
    # timestamp-width change cannot silently turn this into a no-op.
    size = path.stat().st_size
    monkeypatch.setattr(sl, "_MAX_STATE_BYTES", size + 4096 - 200)
    returned = sl.record(key, next_step="advance")
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    kept = len(returned["events"]) + len(returned["tried"])
    assert kept < len(before["events"]) + len(before["tried"]), "no eviction happened"
    assert returned["events"] or returned["tried"], "eviction emptied the history entirely"
    assert returned == on_disk, "the caller's post-write view diverged from disk"
    assert returned["goal"] == "keep me"


class TestBoundedWriteIsLoudAboutLoss:
    """``_serialize_bounded`` drops history to keep the document readable.

    That data never reaches disk, so — unlike the read side, where the
    original file survives until a write-back — the loss is unrecoverable the
    moment the write lands. ``_read_state_unlocked`` already WARNs when the
    same ceiling makes it discard a whole file; these pin that the partial
    discard is reported too, that the counts are named, that a refused write
    still reports, and that a document which fits stays silent.
    """

    def _warnings(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING and "serialization budget" in r.getMessage()
        ]

    def _over_budget_state(self, extra_events: int = 6) -> dict[str, Any]:
        state = sl._empty_state()
        state["goal"] = "survives"
        state["events"] = [
            {"ts": "t", "kind": "progress", "text": "e" * 200} for _ in range(extra_events)
        ]
        state["tried"] = [
            {"approach": "a" * 200, "rejected_because": "r" * 200, "at": "t"} for _ in range(3)
        ]
        return state

    def test_a_record_that_fits_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """The control that makes this safe to ship: no discard, no line.
        Without it every ledger write would emit a WARNING."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_ledger"):
            sl.record("chat-62-fits", goal="small", event="tiny", event_kind="note")
        assert self._warnings(caplog) == []

    def test_evicted_history_is_counted(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sl, "_MAX_STATE_BYTES", 4096 + 600)
        state = self._over_budget_state()
        before_events, before_tried = len(state["events"]), len(state["tried"])
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_ledger"):
            blob = sl._serialize_bounded(state, source="chat-62-abcd1234")
        found = self._warnings(caplog)
        assert len(found) == 1, f"the discard was silent (warnings: {found})"
        msg = found[0]
        evicted_events = before_events - len(state["events"])
        assert evicted_events > 0, "fixture did not cross the budget"
        assert f"oldest events[] evicted x{evicted_events}" in msg
        assert "chat-62-abcd1234" in msg, "the line must name which ledger lost data"
        # Reporting the loss changes nothing about what is kept or written.
        assert len(blob.encode("utf-8")) <= sl._MAX_STATE_BYTES - 4096
        assert json.loads(blob) == state
        assert json.loads(blob)["goal"] == "survives"
        if len(state["tried"]) < before_tried:
            assert f"oldest tried[] evicted x{before_tried - len(state['tried'])}" in msg

    def test_dropped_unknown_fields_are_named(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forward-compat baggage from a newer writer is dropped wholesale.
        That is the widest discard this function makes and was the quietest."""
        monkeypatch.setattr(sl, "_MAX_STATE_BYTES", 4096 + 600)
        state = sl._empty_state()
        state["goal"] = "survives"
        state["future_blob"] = "z" * 4000
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_ledger"):
            blob = sl._serialize_bounded(state, source="chat-62-ffff0000")
        found = self._warnings(caplog)
        assert len(found) == 1, f"the discard was silent (warnings: {found})"
        assert "unknown fields dropped: future_blob" in found[0]
        assert json.loads(blob)["goal"] == "survives"
        assert "future_blob" not in json.loads(blob)

    def test_a_refused_write_reports_what_it_gutted(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal raises with the in-memory record already stripped —
        the moment an operator most needs to know what went."""
        monkeypatch.setattr(sl, "_MAX_STATE_BYTES", 4096 + 10)
        state = self._over_budget_state(extra_events=2)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_ledger"):
            with pytest.raises(ValueError, match="too large"):
                sl._serialize_bounded(state, source="chat-62-dead0000")
        found = self._warnings(caplog)
        assert len(found) == 1, f"the refusal was silent (warnings: {found})"
        assert "oldest events[] evicted x2" in found[0]

    def test_a_failed_write_still_reports_but_leaves_the_stored_file_intact(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why the line describes the DOCUMENT, not a durable write.

        ``atomic_write`` runs after the serializer and can fail (ENOSPC),
        leaving the previous state file whole — so nothing was durably lost,
        even though this call's in-memory record was already stripped. A
        message claiming a write discarded data would be false here.
        """
        key = "chat-62-enospc"
        sl.record(key, goal="on disk")
        for i in range(5):
            sl.record(key, event=f"e{i}" + "y" * 300, event_kind="progress")
        path = sl.ledger_dir(key) / sl._STATE_FILE
        before = path.read_text(encoding="utf-8")

        def _boom(*a: Any, **kw: Any) -> None:
            raise OSError(28, "No space left on device")

        # Gentle squeeze: one evicted event is enough to fit, so serialization
        # SUCCEEDS and the failing write is genuinely reached.
        monkeypatch.setattr(sl, "_MAX_STATE_BYTES", path.stat().st_size + 4096 - 200)
        monkeypatch.setattr(sl, "atomic_write", _boom)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_ledger"):
            with pytest.raises(OSError):
                sl.record(key, next_step="never lands")
        found = self._warnings(caplog)
        assert len(found) == 1, f"the discard was silent (warnings: {found})"
        assert "oldest events[] evicted x" in found[0]
        assert "write" not in found[0], "must not claim a write that did not happen"
        assert path.read_text(encoding="utf-8") == before, "the stored file must be untouched"

    def test_the_eviction_branch_needs_both_lists_at_scale(self) -> None:
        """Why these tests squeeze the budget instead of using real bounds.

        A full astral-script EVENT tail is ~806 KB — under the 995,904-byte
        budget on its own, so ``test_writer_guarantees_the_read_ceiling_...``
        never actually reaches the eviction branch. Crossing it at production
        bounds needs ``events`` AND ``tried`` both loaded (~1.87 MB), which
        costs 150 locked read-modify-write cycles over a multi-megabyte
        document. Pinned as arithmetic so the reason stays visible.
        """
        four_byte = 4  # astral-plane char in UTF-8
        events = sl._MAX_EVENTS * sl._MAX_TEXT * four_byte
        tried = sl._MAX_TRIED * 2 * sl._MAX_TEXT * four_byte
        budget = sl._MAX_STATE_BYTES - 4096
        assert events < budget, "events alone would cross the budget; revisit the fast tests"
        assert events + tried > budget, "the eviction branch would be unreachable"


def test_purge_matching_tolerates_bad_and_absent_keys():
    """The one delete primitive: a hostile or unknown key removes nothing and never
    raises."""
    sl.record("chat-10-000", goal="x")
    assert sl.purge_matching({"", "a/b", "chat-never"}, guard=lambda _d: True) == 0
    assert sl.has_ledger("chat-10-000")


@pytest.mark.skipif(not IS_POSIX, reason="flock-based holder simulation is POSIX-only")
def test_record_fails_closed_on_held_lock(monkeypatch):
    """A live cross-process flock HOLDER costs one refused write (``OSError``),
    and the refusal is bounded rather than an unbounded wait — removing the
    lock entirely would break this test, because an unlocked record would
    succeed while the lock is held.

    Deliberately NOT asserted: a bound on a wedged FILESYSTEM/mount. The
    pre-lock mkdir/os.open are ordinary syscalls that a dead mount can stall
    unboundedly, and no in-process deadline can interrupt a stalled syscall
    (see ``session_ledger._locked``). There is no portable way to simulate a
    hung mkdir/os.open, and asserting a bound there would re-assert the
    over-broad "never a parked worker thread" promise the docs do not make.
    """
    import fcntl

    timeout = 0.2
    # Generous absolute ceiling: the claim under test is "bounded, not
    # forever", so the margin only has to exclude an unbounded wait.
    slack = 2.0
    key = "chat-lock-1"
    sl.record(key, goal="x")  # create the dir + lock inode
    monkeypatch.setattr(sl, "_LOCK_TIMEOUT_SECS", timeout)
    fd = os.open(str(sl.ledger_dir(key) / ".lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(OSError, match="held by another process"):
            sl.record(key, goal="y")
        elapsed = time.monotonic() - start
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert elapsed < timeout + slack, (
        f"refusal took {elapsed:.3f}s; the deadline is not governing the "
        f"acquire poll (expected < {timeout + slack:.3f}s)"
    )
    # Holder released: the next write goes through.
    sl.record(key, goal="z")
    assert sl.read_state(key)["goal"] == "z"


# ── snapshot rendering ────────────────────────────────────────────────────


def test_snapshot_empty_without_ledger_or_when_terminal():
    assert sl.render_snapshot("no-such-session") == ""
    key = "chat-11-111"
    sl.record(key, goal="g", phase="done", event="done", event_kind="progress")
    assert sl.render_snapshot(key) == ""


def test_snapshot_includes_artifact_only_ledger():
    key = "chat-11-222"
    sl.record(key, artifacts={"branch": "feat/x"})
    snap = sl.render_snapshot(key)
    assert "feat/x" in snap


def test_snapshot_contains_state_and_is_capped():
    key = "chat-12-222"
    sl.record(
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
        sl.record(
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
    sl.record(sl.ledger_key(slot_key), goal="babysit PR 42", next_step="check CI")
    out = await compose_nudge_body("check {{STOP_FILE}}", "/x/.stop", slot_key)
    assert out.startswith("[work ledger")
    assert "babysit PR 42" in out
    assert out.endswith("check /x/.stop")


@pytest.mark.asyncio
async def test_compose_nudge_body_folds_key_like_the_write_path():
    """A ledger written under the route's fold of `dashboard_chat-X` must be
    the one a loop keyed `chat-X` reads — changing either side's fold breaks
    this pairing."""
    from kiro_crew.dashboard.handlers.autonudge import compose_nudge_body

    sl.record(sl.ledger_key("dashboard_chat-15-555"), goal="paired")
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
    app["state"] = MagicMock()
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
    req = _mk_request("POST", "/api/session-ledger/record", body={"phase": "implementing"})
    resp = await routes.api_session_ledger_record(req)
    assert resp.status == 400
    assert "event" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_route_rejects_non_string_artifacts(_open_route):
    routes = _open_route
    req = _mk_request("POST", "/api/session-ledger/record", body={"artifacts": {"pr": 123}})
    resp = await routes.api_session_ledger_record(req)
    assert resp.status == 400


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
    losslessly, dashboard prefixes only."""
    routes = _open_route
    sk = "dashboard_chat-77-999"
    req = _mk_request("POST", "/api/session-ledger/record", body={"goal": "fold me"}, sk=sk)
    assert (await routes.api_session_ledger_record(req)).status == 200
    assert sl.read_state(sl.ledger_key(sk))["goal"] == "fold me"
    # And a colon-structured channel key writes to its own exact-key ledger.
    sk2 = "slack:C123:456.789"
    req2 = _mk_request("POST", "/api/session-ledger/record", body={"goal": "channel"}, sk=sk2)
    assert (await routes.api_session_ledger_record(req2)).status == 200
    assert sl.read_state(sk2)["goal"] == "channel"


def test_routes_are_on_the_strict_internal_allowlist():
    """The tools authenticate with the internal secret; without this entry the
    call falls through to cookie auth and every tool call 403s."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-ledger" in _STRICT_INTERNAL_API_PATHS


# ── permanent-delete preservation boundary ─────────────────────────────────


@pytest.mark.asyncio
async def test_remove_slot_for_history_key_preserves_ledger():
    from kiro_crew.dashboard.handlers.sessions import _remove_slot_for_history_key

    history_key = "dashboard_chat-88-123"
    ledger_key = sl.ledger_key(history_key)
    sl.record(ledger_key, goal="doomed")
    assert sl.has_ledger(ledger_key)

    state = MagicMock()
    state._slots = {}
    await _remove_slot_for_history_key(state, history_key)
    assert sl.has_ledger(ledger_key)


@pytest.mark.asyncio
async def test_delete_with_folded_spelling_preserves_exact_channel_key_ledger():
    """A lossy transcript spelling never authorizes work-ledger deletion."""
    from kiro_crew.dashboard.handlers.sessions import _remove_slot_for_history_key
    from kiro_crew.dashboard.state import _normalize_slot_key

    channel_key = "slack:C042:1712793600.123456"
    sl.record(channel_key, goal="channel state")
    assert sl.has_ledger(channel_key)

    state = MagicMock()
    state._slots = {}
    # The route receives only the folded transcript spelling; that lossy alias
    # does not authorize deletion of the exact channel ledger.
    await _remove_slot_for_history_key(state, _normalize_slot_key(channel_key))
    assert sl.has_ledger(channel_key)


@pytest.mark.skipif(not IS_POSIX, reason="symlink creation needs no privilege on POSIX")
def test_purge_matching_never_follows_a_linked_store_or_a_linked_entry(tmp_path):
    """A linked store directory is skipped whatever its breadcrumb says, and a
    linked entry INSIDE a store is unlinked as a name, never walked."""
    import shutil

    # A whole store that is a link: skipped, target untouched.
    key = "chat-60-linked-store"
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
    target = tmp_path / "store-target"
    shutil.move(str(directory), str(target))
    directory.symlink_to(target, target_is_directory=True)
    assert sl.purge_matching({key}, guard=lambda _d: True) == 0
    assert (target / "state.json").exists()

    # A linked entry inside a real store: the link goes, the target stays.
    key2 = "chat-61-linked-entry"
    sl.record(key2, goal="g", phase="done", event="x", event_kind="phase")
    directory2 = sl.ledger_dir(key2)
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

    sl.record("slack:C1:1.1", goal="a")
    sl.record("slack:C2:2.2", goal="b")
    sl.record("chat-keep-1", goal="keep")
    removed = sl.purge_matching(
        {"slack:C1:1.1", _normalize_slot_key("slack:C2:2.2")}, guard=lambda _d: True
    )
    assert removed == 1
    assert not sl.has_ledger("slack:C1:1.1")
    assert sl.has_ledger("slack:C2:2.2"), "a folded spelling is not the exact key"
    assert sl.has_ledger("chat-keep-1")


def test_guarded_purge_keeps_the_record_and_breadcrumb_when_any_content_survives(
    monkeypatch,
):
    """Removal is ordered and the identity files go LAST, only once everything
    else is gone -- so a failed removal (a Windows sharing violation on one held
    entry) leaves a store that still has its record and still names itself,
    never a residue no purge can address."""
    key = "chat-30-held"
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
    (directory / "stray.bin").write_bytes(b"held by another handle")
    real_unlink = os.path.exists  # placeholder to keep the name in scope

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
    assert sl.read_state(key)["phase"] == "done", "the store is still a ledger"
    del real_unlink


def test_guarded_purge_counts_rmtree_failures_instead_of_ignoring_them(monkeypatch):
    key = "chat-31-subtree"
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
    (directory / "attachments").mkdir()
    (directory / "attachments" / "a.bin").write_bytes(b"x")
    real_rmtree = sl.shutil.rmtree

    def _refuse(path, *args, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror is not None:
            onerror(None, str(path), None)
            return None
        return real_rmtree(path, *args, **kwargs)

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
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
    lock_path = directory / ".lock"
    import shutil

    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        shutil.rmtree(directory)  # the purge completes while this writer holds a stale fd
        with pytest.raises(OSError, match="removed while waiting"):
            sl.require_lock_inode(fd, lock_path)
    finally:
        os.close(fd)
    # The store is gone and stays gone: nothing was published into it.
    assert not directory.exists()


def test_locked_runs_the_inode_check_inside_the_hold(monkeypatch):
    seen: list[str] = []
    real = sl.require_lock_inode

    def _spy(fd, path):
        seen.append(path.name)
        return real(fd, path)

    monkeypatch.setattr(sl, "require_lock_inode", _spy)
    sl.record("chat-41-checked", goal="g")
    assert seen == [".lock"]


@pytest.mark.skipif(not IS_POSIX, reason="in-hold unlink is the POSIX path; Windows refuses it")
def test_the_lock_inode_is_unlinked_inside_the_hold(monkeypatch):
    """Unlinked INSIDE the hold, a queued writer that acquires the old inode finds
    its path gone and refuses. Unlinked after release there is a window where it
    acquires, validates against a path that still exists, proceeds, and the late
    unlink detaches the inode it holds -- so the next writer gets a second inode
    and the two are not serialised."""
    key = "chat-50-inhold"
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
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
    from pathlib import Path

    key = "chat-52-windows"
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)

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
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
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
    key = "chat-52-rebuilt"
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
    lock_path = directory / ".lock"

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
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
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
    sl.record(key, goal="g", phase="done", event="x", event_kind="phase")
    directory = sl.ledger_dir(key)
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
