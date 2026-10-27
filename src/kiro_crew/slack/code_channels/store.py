"""Durable records: routing decisions and channel bindings.

Three tables in one JSON file, ``routing/slack-code-channels.json`` under the
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
* **revoked**, channel id to :class:`Revoked` (unbind time and the old
  anchor): channels unbound because Slack does not list us as their agent, so a button answer clicked there later is
  refused (bounded to the newest :data:`MAX_REVOKED`; a rebind clears it).
  A file without it reads as none revoked.

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
#: Channels unbound because Slack does not list us as their agent, newest kept.
MAX_REVOKED = 2048


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


@dataclass(frozen=True)
class Revoked:
    """A channel unbound because Slack stopped listing us as its agent.

    *anchor_ts* is the old binding's anchor: the thread the dashboard session
    stays linked to, which the inbound gate keeps closed.
    """

    at: float
    anchor_ts: str = ""


def _bounded_revoked(revoked: dict[str, Revoked]) -> dict[str, Revoked]:
    """*revoked* capped at :data:`MAX_REVOKED`, newest unbinds kept."""
    if len(revoked) <= MAX_REVOKED:
        return revoked
    return dict(sorted(revoked.items(), key=lambda kv: kv[1].at, reverse=True)[:MAX_REVOKED])


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
        self._revoked: dict[str, Revoked] = {}

    @property
    def path(self) -> Path:
        return self._path or _default_path()

    def load(self) -> None:
        """Read the file now (blocking; call it off the event loop)."""
        with self._lock:
            self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        """Load once. Callers hold the lock (or are the first, cold read)."""
        if self._loaded:
            return
        routes: dict[str, Route] = {}
        bindings: dict[str, Binding] = {}
        revoked: dict[str, Revoked] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except Exception as exc:
            logger.warning("code channel store unreadable, starting empty: %s", exc)
            data = {}
        if isinstance(data, dict):
            for key, raw in (data.get("routes") or {}).items():
                route = _from_dict(Route, raw)
                if route is not None and route.session_id == key:
                    routes[key] = route
            for key, raw in (data.get("bindings") or {}).items():
                binding = _from_dict(Binding, raw)
                if binding is not None and binding.channel_id == key:
                    bindings[key] = binding
            for key, raw in (data.get("revoked") or {}).items():
                record = _from_dict(Revoked, raw)
                if (
                    record is not None
                    and isinstance(key, str)
                    and key not in bindings
                    and isinstance(record.at, (int, float))
                    and not isinstance(record.at, bool)
                    and isinstance(record.anchor_ts, str)
                ):
                    revoked[key] = record
        self._routes, self._bindings = _bounded_routes(routes, time.time()), bindings
        self._revoked = _bounded_revoked(revoked)
        self._loaded = True

    def _read(self) -> None:
        """Make sure a snapshot exists before a lock-free read."""
        if not self._loaded:
            with self._lock:
                self._ensure_loaded()

    def _commit(
        self,
        routes: dict[str, Route] | None = None,
        bindings: dict[str, Binding] | None = None,
        revoked: dict[str, Revoked] | None = None,
    ) -> None:
        """Write the new tables, then publish them. Callers hold the lock."""
        new_routes = self._routes if routes is None else routes
        new_bindings = self._bindings if bindings is None else bindings
        new_revoked = self._revoked if revoked is None else revoked
        payload = {
            "routes": {k: asdict(v) for k, v in new_routes.items()},
            "bindings": {k: asdict(v) for k, v in new_bindings.items()},
            "revoked": {k: asdict(v) for k, v in new_revoked.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(payload, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.warning("code channel store write failed: %s", exc)
            raise StoreWriteError(str(exc)) from exc
        # Revocations first: a lock-free reader that sees a binding gone must
        # already see the channel as revoked.
        self._revoked = new_revoked
        self._routes, self._bindings = new_routes, new_bindings

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

    def put_binding(self, binding: Binding) -> Binding:
        """Store *binding* unless the channel is already bound; returns the stored one."""
        with self._lock:
            self._ensure_loaded()
            existing = self._bindings.get(binding.channel_id)
            if existing is not None:
                return existing
            stored = Binding(**{**asdict(binding), "created_at": binding.created_at or time.time()})
            self._commit(
                bindings={**self._bindings, binding.channel_id: stored},
                revoked=self._without_revoked(binding.channel_id),
            )
            return stored

    def drop_binding(self, channel_id: str) -> bool:
        """Unbind *channel_id* because Slack does not list us as its agent.

        The channel is remembered as revoked: a control its session posted
        before the unbind is still clickable, and an answer to it must be
        refused rather than read as coming from a channel that was never ours.
        """
        with self._lock:
            self._ensure_loaded()
            if channel_id not in self._bindings:
                return False
            old = self._bindings[channel_id]
            self._commit(
                bindings={k: v for k, v in self._bindings.items() if k != channel_id},
                revoked=_bounded_revoked(
                    {**self._revoked, channel_id: Revoked(time.time(), old.anchor_ts)}
                ),
            )
            return True

    def was_revoked(self, channel_id: str) -> bool:
        """Whether *channel_id* was a code channel this gateway has since unbound."""
        self._read()
        return channel_id in self._revoked

    def revoked(self, channel_id: str) -> Revoked | None:
        """The revocation record for *channel_id*, or None."""
        self._read()
        return self._revoked.get(channel_id)

    def _without_revoked(self, channel_id: str) -> dict[str, Revoked] | None:
        if channel_id not in self._revoked:
            return None
        return {k: v for k, v in self._revoked.items() if k != channel_id}

    def replace_binding(self, binding: Binding) -> Binding:
        """Overwrite the channel's binding (its session was closed and remade)."""
        with self._lock:
            self._ensure_loaded()
            stored = Binding(**{**asdict(binding), "created_at": binding.created_at or time.time()})
            self._commit(
                bindings={**self._bindings, binding.channel_id: stored},
                revoked=self._without_revoked(binding.channel_id),
            )
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
