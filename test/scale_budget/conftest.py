"""Seeding helpers and doubles for the scale-budget tests (Lane A).

Every budget in this directory is an operation or byte count, never a wall-clock
limit, so a slow CI host cannot fail one. The helpers here build the inputs those
counts are taken over without a seeder subprocess or a real home:

* transcripts are written with ``Path.write_text`` and stamped with an explicit
  ``os.utime`` in the past, so no listing treats them as too young to trust;
* slots come from the shared ``slot_scale_helpers.seed_slots``, which holds
  ``push_slots_update`` for the whole seed and broadcasts once (one
  ``get_or_create_slot`` per slot without the hold re-serializes the whole list
  each time, O(N^2)); :func:`seed_slots` here adds transcript rows to each;
* :func:`budget_state` is a ``DashboardState`` over a real ``SessionMap``, because a
  ``MagicMock`` session manager turns the slot-row link memo off;
* :class:`FakeClock` is patched into every coalescing clock that exists on the
  code under test, and :class:`FakeLoop` runs the timers those windows arm.

Nothing here is shared between test files: every double is built per test, so
each file passes alone and in any order.
"""

from __future__ import annotations

import heapq
import importlib
import itertools
import json
import os
import time
import types
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import pytest
from slot_scale_helpers import seed_slots as _seed_plain_slots

#: Seeded files are stamped this far in the past. Older than every "too young to
#: trust" window the listing code has (a few seconds), so the counts measure the
#: steady path rather than a race guard.
SEED_AGE_SECS = 86_400.0


def optional_module(name: str) -> types.ModuleType | None:
    """*name* imported, or ``None`` when the code under test does not have it yet."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


# ── transcripts ───────────────────────────────────────────────────────────────


def write_transcript(
    directory: Path,
    stem: str,
    *,
    title: str | None = None,
    user_line: str | None = None,
    agent: str | None = None,
    mtime: float,
) -> Path:
    """Write one transcript in the on-disk ``ConversationLog`` format and stamp it."""
    meta: dict[str, Any] = {
        "_type": "metadata",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_consolidated": 0,
    }
    if title is not None:
        meta["title"] = title
    if agent is not None:
        meta["agent"] = agent
    lines = [json.dumps(meta)]
    if user_line is not None:
        lines.append(
            json.dumps({"role": "user", "content": user_line, "ts": "2026-01-01T00:00:01+00:00"})
        )
    path = directory / f"{stem}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def seed_history(directory: Path, *, per_class: int, cron_every: int = 10) -> dict[str, int]:
    """Seed three classes of *per_class* transcripts each; return the class counts.

    ``titled`` carries a metadata title, ``untitled`` a first user line and no title,
    ``empty`` only a metadata line (the row ``user_only`` must read to drop). Every
    *cron_every*-th file of each class sits in the ``cron`` namespace, the
    shape a scheduled job's transcripts take; the listing treats them like any
    other row.
    """
    directory.mkdir(parents=True, exist_ok=True)
    base = time.time() - SEED_AGE_SECS
    counter = itertools.count()
    counts: Counter[str] = Counter()
    for cls in ("titled", "untitled", "empty"):
        for i in range(per_class):
            n = next(counter)
            stem = f"cron_job-{cls}-{i}" if i % cron_every == 0 else f"dashboard_chat-{n}-{i}"
            mtime = base + n
            if cls == "titled":
                write_transcript(directory, stem, title=f"t{n}", user_line="hello", mtime=mtime)
            elif cls == "untitled":
                write_transcript(directory, stem, user_line=f"question {n}", mtime=mtime)
            else:
                write_transcript(directory, stem, agent="kirocrew", mtime=mtime)
            counts[cls] += 1
            if stem.startswith("cron_"):
                counts["cron"] += 1
    return dict(counts)


def seed_one_line_history(directory: Path, n: int) -> None:
    """Seed *n* metadata-only one-line transcripts (the cron fan-out shape)."""
    directory.mkdir(parents=True, exist_ok=True)
    base = time.time() - SEED_AGE_SECS
    line = json.dumps({"_type": "metadata", "created_at": "2026-01-01T00:00:00+00:00"}) + "\n"
    for i in range(n):
        path = directory / f"cron_tick-{i}.jsonl"
        path.write_text(line, encoding="utf-8")
        os.utime(path, (base + i, base + i))


class TranscriptIO:
    """Counts transcript opens (``*.jsonl`` under one directory) and transcript locks.

    Opens are counted at ``builtins.open``, ``io.open`` (what ``Path.open`` calls)
    and ``os.open``, so the count does not depend on which reader a version uses.
    Locks are ``ConversationLog._locked`` entries.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = str(directory)
        self.opens = 0
        self.locks = 0
        self.stats = 0
        self.scandirs = 0

    def _is_transcript(self, path: object) -> bool:
        try:
            text = os.fspath(path)  # type: ignore[arg-type]
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return text.endswith(".jsonl") and text.startswith(self.directory)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "TranscriptIO":
        import builtins
        import io

        from kiro_crew.history import ConversationLog

        real_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_stat = os.stat
        real_scandir = os.scandir
        real_locked = ConversationLog._locked

        def _open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if self._is_transcript(file):
                self.opens += 1
            return real_open(file, *args, **kwargs)

        def _io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if self._is_transcript(file):
                self.opens += 1
            return real_io_open(file, *args, **kwargs)

        def _os_open(path: Any, *args: Any, **kwargs: Any) -> Any:
            if self._is_transcript(path):
                self.opens += 1
            return real_os_open(path, *args, **kwargs)

        def _stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, (str, bytes, os.PathLike)) and (
                self._is_transcript(path) or os.fspath(path) == self.directory
            ):
                self.stats += 1
            return real_stat(path, *args, **kwargs)

        def _scandir(path: Any = ".") -> Any:
            if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == self.directory:
                self.scandirs += 1
            return real_scandir(path)

        def _locked(log: Any, key: str) -> Any:
            self.locks += 1
            return real_locked(log, key)

        monkeypatch.setattr(builtins, "open", _open)
        monkeypatch.setattr(io, "open", _io_open)
        monkeypatch.setattr(os, "open", _os_open)
        monkeypatch.setattr(os, "stat", _stat)
        monkeypatch.setattr(os, "scandir", _scandir)
        monkeypatch.setattr(ConversationLog, "_locked", _locked)
        return self

    def reset(self) -> None:
        self.opens = self.locks = self.stats = self.scandirs = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "opens": self.opens,
            "locks": self.locks,
            "stats": self.stats,
            "scandirs": self.scandirs,
        }


# ── call counters ─────────────────────────────────────────────────────────────


class Spy:
    """Counts calls to named functions, forwarding each call to the original."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.counts: Counter[str] = Counter()
        self.calls: dict[str, list[tuple[tuple, dict]]] = {}

    def wrap(
        self, target: Any, name: str, label: str | None = None, *, record: bool = False
    ) -> None:
        """Count calls to ``target.name`` under *label* (default: *name*)."""
        label = label or name
        original = getattr(target, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.counts[label] += 1
            if record:
                self.calls.setdefault(label, []).append((args, kwargs))
            return original(*args, **kwargs)

        self._monkeypatch.setattr(target, name, counted)

    def reset(self) -> None:
        self.counts.clear()
        self.calls.clear()


def harness_fail(message: str) -> None:
    """Fail as a harness break, never as a budget's expected failure.

    Every strict xfail here carries ``raises=AssertionError``. ``pytest.fail``
    raises ``Failed``, which is not an ``AssertionError``, so a patch that stopped
    reaching the code under test reads red on main instead of "expected".
    """
    pytest.fail(f"harness: {message}", pytrace=False)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    return Spy(monkeypatch)


# ── fake time ─────────────────────────────────────────────────────────────────


class FakeClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Handle:
    def __init__(self, loop: "FakeLoop", when: float, seq: int, cb: Callable, args: tuple) -> None:
        self._loop = loop
        self.when = when
        self.seq = seq
        self.cb = cb
        self.args = args
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __lt__(self, other: "_Handle") -> bool:
        return (self.when, self.seq) < (other.when, other.seq)


class FakeLoop:
    """The parts of an event loop the coalescing windows use, on a :class:`FakeClock`.

    ``call_later`` queues a timer due at the fake time; :meth:`run_due` fires every
    timer that is due. ``call_soon_threadsafe`` runs its callback at once, which is
    what the serving loop would do on its next tick.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self._timers: list[_Handle] = []
        self._seq = itertools.count()

    def call_later(self, delay: float, cb: Callable, *args: Any) -> _Handle:
        handle = _Handle(self, self.clock.now + max(0.0, delay), next(self._seq), cb, args)
        heapq.heappush(self._timers, handle)
        return handle

    def call_soon_threadsafe(self, cb: Callable, *args: Any) -> None:
        cb(*args)

    def call_soon(self, cb: Callable, *args: Any) -> None:
        cb(*args)

    def is_closed(self) -> bool:
        return False

    def time(self) -> float:
        return self.clock.now

    def run_due(self) -> int:
        fired = 0
        while self._timers and self._timers[0].when <= self.clock.now:
            handle = heapq.heappop(self._timers)
            if not handle.cancelled:
                handle.cb(*handle.args)
                fired += 1
        return fired

    def pending(self) -> int:
        return sum(1 for h in self._timers if not h.cancelled)


#: How far ahead of the wall clock a "too young to trust" check reads. A
#: directory's ctime is the moment the seed wrote it and ``os.utime`` cannot move
#: it, so a check that compares ctime with ``time_ns()`` would distrust a freshly
#: seeded directory for its whole racy window (2 s in every version so far).
#: Ten windows ahead makes the seed old without depending on test speed.
TRUST_AHEAD_NS = 10 * 2_000_000_000


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """One fake clock in every coalescing clock section 6 names that exists here.

    * ``slots_push._slots_broadcast_clock`` (main);
    * ``refresh_coalesce._clock``;
    * ``history_listing_memo``'s ``time.monotonic``: its whole-listing memo
      answers a repeat listing for ``TTL_SECS``, so on the real clock whether a
      second listing hits it depends on how fast the first one ran;
    * the listing index (once F9 lands). ``SessionListingIndex`` binds
      ``time.monotonic`` as a default argument at import, so patching the
      module's ``time`` cannot reach it: the class ``ConversationLog`` builds
      from is replaced by a subclass that passes this clock. The index module's
      ``time`` still becomes a namespace whose ``time_ns`` reads
      :data:`TRUST_AHEAD_NS` ahead (its directory-stamp trust check) and whose
      ``sleep`` is a no-op (its paced warm).
    """
    clock = FakeClock()
    # Patched through the ``state`` facade, which mirrors the name into every
    # owner that binds it; a patch on ``slots_push`` alone would be undone
    # wrongly by the facade's teardown.
    monkeypatch.setattr("kiro_crew.dashboard.state._slots_broadcast_clock", clock)
    refresh = optional_module("kiro_crew.dashboard.state_parts.refresh_coalesce")
    if refresh is not None:
        monkeypatch.setattr(refresh, "_clock", clock)
    listing_memo = optional_module("kiro_crew.history_listing_memo")
    if listing_memo is not None:
        monkeypatch.setattr(listing_memo, "time", _DelegatingTime(monotonic=clock))
    index = optional_module("kiro_crew.history_listing_index")
    if index is not None:
        from kiro_crew import history

        base = getattr(history, "SessionListingIndex", None)
        if base is None:
            harness_fail(
                "kiro_crew.history_listing_index exists but kiro_crew.history no longer "
                "builds SessionListingIndex; point fake_clock at the new seam"
            )

        class _ClockedIndex(base):  # type: ignore[misc, valid-type]
            def __init__(self, log: Any, **kwargs: Any) -> None:
                kwargs.setdefault("clock", clock)
                super().__init__(log, **kwargs)

        monkeypatch.setattr(history, "SessionListingIndex", _ClockedIndex)
        monkeypatch.setattr(
            index,
            "time",
            _DelegatingTime(
                monotonic=clock,
                time_ns=lambda: time.time_ns() + TRUST_AHEAD_NS,
                sleep=lambda _s: None,
            ),
        )
    return clock


class _DelegatingTime(types.SimpleNamespace):
    """A ``time`` stand-in: the names given are overridden, every other name is the real module's."""

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


# ── dashboard state ───────────────────────────────────────────────────────────

_LINK_ACCESSORS = (
    "clear_mirror_link",
    "clear_slack_link",
    "get_mirror_link",
    "get_slack_link",
    "is_mirror_paused",
    "is_slack_paused",
    "mirror_accepts_inbound",
    "mirror_link_nonce",
    "session_map_generation",
    "set_mirror_link",
    "set_mirror_paused",
    "set_slack_link",
    "set_slack_paused",
    "slack_link_nonce",
)


class FakeWs(dict):
    """A dashboard socket double: a mapping for the per-socket flags, plus a frame log."""

    def __init__(self, *, owner: bool, slot_patch: bool = True, slot_diff: bool = False) -> None:
        super().__init__()
        from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG

        self.closed = False
        self.owner = owner
        self.frames: list[str] = []
        self["_is_dashboard_user"] = True
        self[SLOT_PATCH_WS_FLAG] = slot_patch
        hub = importlib.import_module("kiro_crew.dashboard.websocket_hub")
        flag = getattr(hub, "SLOT_DIFF_WS_FLAG", None)
        if flag is not None:
            self[flag] = slot_diff

    # Identity, not dict equality: the hub keeps sockets in sets.
    __hash__ = object.__hash__  # type: ignore[assignment]

    def __eq__(self, other: object) -> bool:
        return self is other

    async def send_str(self, msg: str) -> None:  # pragma: no cover - sends are recorded
        self.frames.append(msg)

    def types(self) -> list[str]:
        return [json.loads(f).get("type", "") for f in self.frames]

    def count(self, msg_type: str) -> int:
        return self.types().count(msg_type)

    def bytes(self, *msg_types: str) -> int:
        return sum(
            len(f.encode("utf-8"))
            for f in self.frames
            if not msg_types or json.loads(f).get("type") in msg_types
        )


def budget_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock) -> Any:
    """A ``DashboardState`` over a real ``SessionMap``, on a fake loop and clock.

    Sends are recorded on each :class:`FakeWs` synchronously instead of being
    spawned as tasks, so every frame a push produces is visible as soon as the
    push returns.
    """
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.session_map import SessionMap
    from kiro_crew.session_parts import links as session_links

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers._session_card_chips_snapshot", True
    )
    # ``state`` re-exports these, but ``source_links`` calls its own module globals.
    for owner in ("kiro_crew.dashboard.state", "kiro_crew.dashboard.state_parts.source_links"):
        monkeypatch.setattr(
            f"{owner}._cached_check_status", lambda url: {"ci": "passed", "state": "OPEN"}
        )
        monkeypatch.setattr(f"{owner}._repo_is_public", lambda url: "public" in url)
        monkeypatch.setattr(f"{owner}._audit_public_status_grant", lambda url: None)
        monkeypatch.setattr(f"{owner}._audit_public_status_denied", lambda url: None)
    sessions = MagicMock(count=0)
    sessions._session_map = SessionMap()
    for name in _LINK_ACCESSORS:
        accessor = getattr(session_links, name, None)
        if accessor is not None:
            setattr(sessions, name, types.MethodType(accessor, sessions))
    state = DashboardState(
        sessions=sessions, crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    monkeypatch.setattr(state, "is_yolo_active", lambda: False)
    loop = FakeLoop(clock)
    state._serving_loop = loop
    monkeypatch.setattr(state, "_running_loop", lambda: loop)
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, msg: ws.frames.append(msg))
    state.fake_loop = loop
    return state


def add_socket(state: Any, ws: FakeWs) -> FakeWs:
    """Register *ws* the way ``register_ws`` does: every socket, plus the owner set."""
    state._ws_clients.append(ws)
    if ws.owner:
        state._owner_ws_clients.add(ws)
    return ws


def seed_slots(state: Any, n: int, *, rows: int = 2) -> list[Any]:
    """:func:`slot_scale_helpers.seed_slots` with *rows* transcript rows per slot.

    The slots themselves come from the shared helper, the one definition of a
    seeded slot. The rows give the push budgets something to summarize and
    redact, so a link or redaction budget cannot pass on empty transcripts.
    The nested suspension keeps the whole seed to one slot-list broadcast.
    """
    with state.suspend_slots_push():
        slots = _seed_plain_slots(state, n)
        for i, slot in enumerate(slots):
            for r in range(rows):
                role = "user" if r % 2 == 0 else "assistant"
                slot.append(role, f"{role} row {r} of slot {i}", "msg")
    return slots


@pytest.fixture
def frames_of() -> Callable[[FakeWs, str], list[dict]]:
    def _frames(ws: FakeWs, msg_type: str) -> list[dict]:
        return [json.loads(f) for f in ws.frames if json.loads(f).get("type") == msg_type]

    return _frames


def iter_budget(**pairs: tuple[int, int]) -> Iterator[str]:
    """``name=(measured, budget)`` pairs over budget, as readable lines."""
    for name, (measured, budget) in pairs.items():
        if measured > budget:
            yield f"{name}: {measured} > {budget}"
