"""Durable records: routing decisions and channel bindings.

Two tables in one JSON file, ``routing/slack-code-channels.json`` under the
Kiro Crew data home:

* **routes**, keyed by the deterministic ``session_id`` of the origin message
  (:func:`kiro_crew.slack.code_channels.creation.session_id_for`). A route
  records what the classifier decided and, once created, which channel it
  opened. Slack re-delivers an event it thinks we missed; the second delivery
  finds the route and does not classify or create again. Writes are monotonic:
  a decided route never changes its decision, and a destination channel, once
  recorded, never changes.
* **bindings**, keyed by channel id: the dashboard session a code channel runs
  in. See :class:`Binding`.

Reads are lock-free snapshots that never touch the disk once loaded (load it off
the event loop at start); every write rewrites the file atomically under a lock
and is published only once the file write succeeded -- a failed write raises
:class:`StoreWriteError` and changes nothing. A missing or unreadable file
reads as empty -- it never crashes the gateway.

The file decides which session a channel's messages run in, so no agent may
read or write it. It lives in the data home's ``routing`` directory, which the
security floors already fence as a whole for the other channels' delivery
state (``teams.service_urls``, the Telegram cursor): agent file tools refuse
it on the read and write floors, every sandbox masks the directory, and it is
pre-created so the mask holds on a fresh install. The directory mask also hides
the temp file ``atomic_write`` publishes through. Only the gateway opens it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

STORE_FILENAME = "slack-code-channels.json"
STORE_DIRNAME = "routing"

ROUTE_STAY = "stay"
ROUTE_CODE_CHANNEL = "code_channel"

#: How long a route is kept. Its only job is to make a RE-DELIVERED event (Slack
#: retries within minutes) or a restart mid-creation reuse the first decision;
#: past this window it is pruned, and the binding is what records the channel.
ROUTE_TTL_SECS = 24 * 3600
#: Most routes kept at once, whatever their age; the oldest go first.
MAX_ROUTES = 2048


def _bounded_routes(routes: dict[str, "Route"], now: float) -> dict[str, "Route"]:
    """*routes* without expired rows, capped at :data:`MAX_ROUTES`.

    Past the cap the oldest ``stay`` rows go first: thread replies record one
    each, and they must not push out a ``code_channel`` row a restart still
    needs to finish opening its channel.
    """
    fresh = {k: r for k, r in routes.items() if now - r.created_at <= ROUTE_TTL_SECS}
    if len(fresh) <= MAX_ROUTES:
        return fresh
    ranked = sorted(
        fresh.items(),
        key=lambda kv: (kv[1].decision == ROUTE_CODE_CHANNEL, kv[1].created_at),
        reverse=True,
    )
    return dict(ranked[:MAX_ROUTES])


@dataclass(frozen=True)
class Route:
    """One origin message's routing decision."""

    session_id: str
    origin_channel_id: str
    origin_ts: str
    team_id: str = ""
    decision: str = ""
    title: str = ""
    channel_id: str = ""
    created_at: float = 0.0
    #: When the first turn reached the channel's session (0 = not yet). A
    #: re-delivered event for a route with a channel but no seed delivers it.
    seeded_at: float = 0.0
    #: The origin thread the message was in (its own ts when it opened one).
    origin_thread_ts: str = ""


@dataclass(frozen=True)
class Binding:
    """A code channel and the dashboard session it runs in.

    ``session_key`` is the dashboard slot key (``chat-...``). ``anchor_ts`` is
    the ts of the kickoff message this gateway posted in the channel; the slot's
    Slack link is recorded against it, which is how an inbound message in the
    channel finds its slot (the Slack link index is keyed by a ts). Replies are
    still posted top-level -- the anchor is an identity, not a thread.

    ``created_by_us`` is False for a channel opened from Slack's own UI that
    names this app as its agent; such a channel is adopted on first use.

    ``channel_name`` is the Slack channel name at bind time; ``archived_at`` /
    ``wrapped_up_at`` are wall-clock stamps the wrap-up feature sets.
    ``origin_thread_ts`` is the origin thread the channel was opened from, which
    stops that thread's follow-ups being classified again. ``""`` / ``0`` mean
    unset, so a record written before these fields existed loads unchanged.
    """

    channel_id: str
    team_id: str
    session_key: str
    anchor_ts: str
    project_dir: str = ""
    origin_channel_id: str = ""
    origin_ts: str = ""
    created_by_us: bool = True
    title: str = ""
    created_at: float = field(default=0.0)
    channel_name: str = ""
    archived_at: float = 0.0
    wrapped_up_at: float = 0.0
    origin_thread_ts: str = ""

    @property
    def origin_link(self) -> tuple[str, str] | None:
        if self.origin_channel_id and self.origin_ts:
            return self.origin_channel_id, self.origin_ts
        return None


def _default_path() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / STORE_DIRNAME / STORE_FILENAME


def _from_dict(cls: type, raw: Any) -> Any | None:
    if not isinstance(raw, dict):
        return None
    names = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
    try:
        return cls(**{k: v for k, v in raw.items() if k in names})
    except TypeError:
        return None


class StoreWriteError(Exception):
    """The store file could not be written; the change was NOT applied."""


class StoreUnavailable(Exception):
    """The store cannot answer: not loaded yet, or its file could not be read."""


class CodeChannelStore:
    """The routes and bindings tables (see the module docstring).

    Reads take no lock and never touch the disk once the store is loaded: each
    write builds new tables and swaps them in whole after the file write
    succeeds, so a reader on the event loop sees either the old snapshot or the
    new one and never waits on a writer. Load it off the loop at start with
    :meth:`load` (``code_channels.install`` arranges that).

    A write is persisted before it is published: when the file cannot be
    written the in-memory tables keep their previous state and the write raises
    :class:`StoreWriteError`, so nothing can go on using a binding a restart
    would forget.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._routes: dict[str, Route] = {}
        self._bindings: dict[str, Binding] = {}
        #: Session keys a binding named before it was replaced or dropped, oldest
        #: first: a worker such a session created stays a code-channel worker.
        self._retired: tuple[str, ...] = ()
        #: Worker slot key -> the code-channel session (slot key) that created it,
        #: written by ``create_session`` at the worker's birth. Server-owned in this
        #: write-protected file, so a worker cannot erase its own lineage the way it
        #: could edit its transcript's ``created_by``. Never evicted.
        self._workers: dict[str, str] = {}
        #: The file existed and could not be read or parsed, so the tables started
        #: empty. Sticky for the process: every reader that must not mistake
        #: "unreadable" for "empty" (:meth:`session_keys`) refuses on it.
        self._load_failed = False

    @property
    def path(self) -> Path:
        return self._path or _default_path()

    @property
    def loaded(self) -> bool:
        """Whether the file has been read, so a read here touches no disk."""
        return self._loaded

    @property
    def unreadable(self) -> bool:
        """The file was loaded and could not be read whole. No disk access."""
        return self._loaded and self._load_failed

    def load(self) -> None:
        """Read the file now (blocking; call it off the event loop)."""
        with self._lock:
            self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        """Load once. Callers hold the lock (or are the first, cold read)."""
        if self._loaded:
            return
        try:
            data = self._read_document()
            routes, bindings, retired, workers = self._parse(data)
        except Exception as exc:
            # Any failure to read the file whole -- unreadable, unparsable, an
            # existing path that is not a readable file (a dangling symlink, a
            # directory), or a document whose structure does not read back -- is
            # "unavailable", never "empty": an empty store would vouch that no
            # session is a code-channel worker. Sticky for the process, and
            # :meth:`_commit` refuses to write over it.
            logger.warning("code channel store unreadable, starting empty: %s", exc)
            routes, bindings, retired, workers = {}, {}, (), {}
            self._load_failed = True
        self._routes, self._bindings = _bounded_routes(routes, time.time()), bindings
        self._retired = retired
        self._workers = workers
        self._loaded = True

    def _read_document(self) -> Any:
        """The parsed file, ``{}`` only when nothing is at the path. Raises otherwise."""
        path = self.path
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(st.st_mode):
            os.stat(path)  # FileNotFoundError here: a dangling link, not absence
        return json.loads(path.read_text(encoding="utf-8"))

    def _parse(
        self, data: Any
    ) -> tuple[dict[str, Route], dict[str, Binding], tuple[str, ...], dict[str, str]]:
        """The tables in *data*. Raises :class:`ValueError` on any malformed part."""
        if not isinstance(data, dict):
            raise ValueError("the store is not a JSON object")
        routes: dict[str, Route] = {}
        raw_routes = data.get("routes", {})
        if not isinstance(raw_routes, dict):
            raise ValueError("routes is not an object")
        for key, raw in raw_routes.items():
            route = _from_dict(Route, raw)
            if route is not None and route.session_id == key:
                routes[key] = route
        bindings: dict[str, Binding] = {}
        raw_bindings = data.get("bindings", {})
        if not isinstance(raw_bindings, dict):
            raise ValueError("bindings is not an object")
        for key, raw in raw_bindings.items():
            binding = _from_dict(Binding, raw)
            if binding is None or binding.channel_id != key:
                # A row that does not read back whole may have named a session
                # whose workers the lineage must still recognise: unavailable,
                # not "no such channel".
                raise ValueError(f"binding {key!r} does not read back whole")
            bindings[key] = binding
        raw_retired = data.get("retired_session_keys", [])
        if not (
            isinstance(raw_retired, list) and all(isinstance(k, str) and k for k in raw_retired)
        ):
            raise ValueError("retired_session_keys is malformed")
        raw_workers = data.get("workers", {})
        if not (
            isinstance(raw_workers, dict)
            and all(
                isinstance(k, str) and k and isinstance(v, str) and v
                for k, v in raw_workers.items()
            )
        ):
            raise ValueError("workers is malformed")
        return routes, bindings, tuple(raw_retired), dict(raw_workers)

    def _read(self) -> None:
        """Make sure a snapshot exists before a lock-free read."""
        if not self._loaded:
            with self._lock:
                self._ensure_loaded()

    def _commit(
        self,
        routes: dict[str, Route] | None = None,
        bindings: dict[str, Binding] | None = None,
        workers: dict[str, str] | None = None,
    ) -> None:
        """Write the new tables, then publish them. Callers hold the lock.

        A binding the new table no longer names under the same session (dropped, or
        replaced by a new session) retires its session key, kept beside the tables.

        Refused (:class:`StoreWriteError`, nothing changes) on a store whose file
        could not be read: the write would replace that file with these empty
        tables and erase every worker lineage and retired key it held. The file
        stays as it is until a person repairs it and restarts the gateway.
        """
        if self._load_failed:
            raise StoreWriteError(
                "the code-channel store could not be read; refusing to overwrite it"
            )
        new_routes = self._routes if routes is None else routes
        new_bindings = self._bindings if bindings is None else bindings
        retired = list(self._retired)
        for channel_id, old in self._bindings.items():
            new = new_bindings.get(channel_id)
            if old.session_key and (new is None or new.session_key != old.session_key):
                if old.session_key not in retired:
                    retired.append(old.session_key)
        # Never evicted: a retired key marks every worker its session created as a
        # code-channel worker for good, and dropping one would let such a worker
        # dispatch. It grows by one key per replaced or dropped binding -- a
        # human-scale event -- so it stays small.
        new_retired = tuple(retired)
        new_workers = self._workers if workers is None else workers
        payload = {
            "routes": {k: asdict(v) for k, v in new_routes.items()},
            "bindings": {k: asdict(v) for k, v in new_bindings.items()},
            "retired_session_keys": list(new_retired),
            "workers": dict(new_workers),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(payload, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.warning("code channel store write failed: %s", exc)
            raise StoreWriteError(str(exc)) from exc
        self._routes, self._bindings = new_routes, new_bindings
        self._retired, self._workers = new_retired, new_workers

    # ── Routes ───────────────────────────────────────────────────────────

    def get_route(self, session_id: str) -> Route | None:
        self._read()
        return self._routes.get(session_id)

    def record_decision(self, route: Route) -> Route:
        """Store *route*'s decision unless one is already recorded.

        Returns the stored route, which is the EARLIER one when a decision
        already exists: a retried event keeps the first answer.
        """
        with self._lock:
            self._ensure_loaded()
            existing = self._routes.get(route.session_id)
            if existing is not None and existing.decision:
                return existing
            stored = Route(**{**asdict(route), "created_at": route.created_at or time.time()})
            routes = _bounded_routes({**self._routes, route.session_id: stored}, time.time())
            self._commit(routes=routes)
            return stored

    def record_destination(self, session_id: str, channel_id: str) -> Route | None:
        """Record the channel a route opened; a recorded one never changes."""
        with self._lock:
            self._ensure_loaded()
            existing = self._routes.get(session_id)
            if existing is None or existing.channel_id:
                return existing
            stored = Route(**{**asdict(existing), "channel_id": channel_id})
            self._commit(routes={**self._routes, session_id: stored})
            return stored

    def record_seeded(self, session_id: str) -> Route | None:
        """Record that the route's first turn reached its session (set once)."""
        with self._lock:
            self._ensure_loaded()
            existing = self._routes.get(session_id)
            if existing is None or existing.seeded_at:
                return existing
            stored = Route(**{**asdict(existing), "seeded_at": time.time()})
            self._commit(routes={**self._routes, session_id: stored})
            return stored

    # ── Bindings ─────────────────────────────────────────────────────────

    def get_binding(self, channel_id: str) -> Binding | None:
        self._read()
        return self._bindings.get(channel_id)

    def bindings(self) -> list[Binding]:
        self._read()
        return list(self._bindings.values())

    def thread_opened_channel(
        self, origin_channel_id: str, thread_ts: str, *, except_ts: str = ""
    ) -> bool:
        """Whether a code channel was opened (or is being opened) from this thread.

        A bound channel whose origin is the thread, or a route that decided to
        open one there. A thread's root message counts as being in its thread.
        Records whose origin message is *except_ts* are left out, so a
        re-delivered message that opened the channel is still recognised as its
        own opener rather than as a later reply. A route whose create failed
        keeps the thread unclassified until the route expires, which errs
        toward answering in place.
        """
        if not (origin_channel_id and thread_ts):
            return False
        self._read()

        def _in_thread(origin: str, origin_ts: str, origin_thread: str) -> bool:
            return (
                origin == origin_channel_id
                and thread_ts in (origin_thread, origin_ts)
                and not (except_ts and origin_ts == except_ts)
            )

        if any(
            _in_thread(b.origin_channel_id, b.origin_ts, b.origin_thread_ts)
            for b in self._bindings.values()
        ):
            return True
        return any(
            r.decision == ROUTE_CODE_CHANNEL
            and _in_thread(r.origin_channel_id, r.origin_ts, r.origin_thread_ts)
            for r in self._routes.values()
        )

    def loaded_bindings(self) -> list[Binding] | None:
        """The bindings snapshot WITHOUT loading it: ``None`` before :meth:`load`.

        For a synchronous security gate that must never touch the disk (it can
        run inside a no-suspension window): an unloaded store answers "unknown",
        which that gate reads as "not a code channel" and refuses.
        """
        if not self._loaded:
            return None
        return list(self._bindings.values())

    def session_keys(self) -> frozenset[str]:
        """Every session key a code channel runs in or ever ran in.

        The current bindings' sessions plus those a binding named before it was
        replaced or dropped. A session one of these created is a code-channel
        worker for good, whether or not its creator is still open or Slack is
        connected: the dispatch exemption keeps workers one level deep, and that
        must not end when the conductor's tab is closed.

        Answers from the loaded snapshot only and never touches the disk (its
        callers are synchronous gates on the event loop; they load the store off
        the loop first). Raises :class:`StoreUnavailable` when the store is not
        loaded or its file could not be read, so the caller can refuse rather
        than read "unreadable" as "no code channels".
        """
        if not self._loaded:
            raise StoreUnavailable("the code-channel store is not loaded")
        if self._load_failed:
            raise StoreUnavailable("the code-channel store could not be read")
        current = {b.session_key for b in self._bindings.values() if b.session_key}
        return frozenset(current | set(self._retired))

    def worker_conductor(self, worker_key: str) -> str:
        """The code-channel session that created *worker_key*, ``""`` when none.

        From the loaded snapshot only, like :meth:`session_keys`, and raising
        :class:`StoreUnavailable` on the same conditions.
        """
        if not self._loaded:
            raise StoreUnavailable("the code-channel store is not loaded")
        if self._load_failed:
            raise StoreUnavailable("the code-channel store could not be read")
        return self._workers.get(worker_key, "")

    def record_worker(self, worker_key: str, conductor_key: str) -> None:
        """Record that the code-channel session *conductor_key* created *worker_key*.

        Persisted before it is published (:class:`StoreWriteError` changes nothing).
        Refused on a store that failed to load: a write there would replace the
        unreadable file and every lineage it held.
        """
        if not worker_key or not conductor_key:
            raise ValueError("worker and conductor keys are required")
        with self._lock:
            self._ensure_loaded()
            if self._load_failed:
                raise StoreUnavailable("the code-channel store could not be read")
            if self._workers.get(worker_key) == conductor_key:
                return
            self._commit(workers={**self._workers, worker_key: conductor_key})

    def put_binding(self, binding: Binding) -> Binding:
        """Store *binding* unless the channel is already bound; returns the stored one."""
        with self._lock:
            self._ensure_loaded()
            existing = self._bindings.get(binding.channel_id)
            if existing is not None:
                return existing
            stored = Binding(**{**asdict(binding), "created_at": binding.created_at or time.time()})
            self._commit(bindings={**self._bindings, binding.channel_id: stored})
            return stored

    def drop_binding(self, channel_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            if channel_id not in self._bindings:
                return False
            self._commit(bindings={k: v for k, v in self._bindings.items() if k != channel_id})
            return True

    def replace_binding(self, binding: Binding) -> Binding:
        """Overwrite the channel's binding (its session was closed and remade)."""
        with self._lock:
            self._ensure_loaded()
            stored = Binding(**{**asdict(binding), "created_at": binding.created_at or time.time()})
            self._commit(bindings={**self._bindings, binding.channel_id: stored})
            return stored

    def update_binding(self, channel_id: str, **fields: Any) -> Binding | None:
        """Change some fields of a stored binding; None when the channel is unbound.

        Read, change and write happen under the store lock, so two updaters
        never lose each other's fields. ``channel_id`` itself cannot change, and
        an unknown field name is refused (``TypeError``) before anything is
        written.
        """
        unknown = set(fields) - (set(Binding.__dataclass_fields__) - {"channel_id"})
        if unknown:
            raise TypeError(f"unknown Binding field(s): {', '.join(sorted(unknown))}")
        with self._lock:
            self._ensure_loaded()
            existing = self._bindings.get(channel_id)
            if existing is None:
                return None
            stored = Binding(**{**asdict(existing), **fields})
            self._commit(bindings={**self._bindings, channel_id: stored})
            return stored


_STORE: CodeChannelStore | None = None


def store() -> CodeChannelStore:
    """The process-wide store."""
    global _STORE
    if _STORE is None:
        _STORE = CodeChannelStore()
    return _STORE


def set_store(value: CodeChannelStore | None) -> None:
    """Replace the process-wide store (tests)."""
    global _STORE
    _STORE = value
