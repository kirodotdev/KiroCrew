"""Durable records: routing decisions and channel bindings.

Two tables in one JSON file under the Kiro Crew config dir
(``slack-code-channels.json``):

* **routes**, keyed by the deterministic ``session_id`` of the origin message
  (:func:`kiro_crew.slack.code_channels.creation.session_id_for`). A route
  records what the classifier decided and, once created, which channel it
  opened. Slack re-delivers an event it thinks we missed; the second delivery
  finds the route and does not classify or create again. Writes are monotonic:
  a decided route never changes its decision, and a destination channel, once
  recorded, never changes.
* **bindings**, keyed by channel id: the dashboard session a code channel runs
  in. See :class:`Binding`.

The in-memory copy is authoritative for reads; every write rewrites the file
atomically under a lock (writers run on the loop and on worker threads). A
missing or unreadable file reads as empty -- it never crashes the gateway.
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

ROUTE_STAY = "stay"
ROUTE_CODE_CHANNEL = "code_channel"


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

    @property
    def origin_link(self) -> tuple[str, str] | None:
        if self.origin_channel_id and self.origin_ts:
            return self.origin_channel_id, self.origin_ts
        return None


def _default_path() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / STORE_FILENAME


def _from_dict(cls: type, raw: Any) -> Any | None:
    if not isinstance(raw, dict):
        return None
    names = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
    try:
        return cls(**{k: v for k, v in raw.items() if k in names})
    except TypeError:
        return None


class CodeChannelStore:
    """The routes and bindings tables (see the module docstring)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._routes: dict[str, Route] = {}
        self._bindings: dict[str, Binding] = {}

    @property
    def path(self) -> Path:
        return self._path or _default_path()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("code channel store unreadable, starting empty: %s", exc)
            return
        if not isinstance(data, dict):
            return
        for key, raw in (data.get("routes") or {}).items():
            route = _from_dict(Route, raw)
            if route is not None and route.session_id == key:
                self._routes[key] = route
        for key, raw in (data.get("bindings") or {}).items():
            binding = _from_dict(Binding, raw)
            if binding is not None and binding.channel_id == key:
                self._bindings[key] = binding

    def _flush(self) -> None:
        payload = {
            "routes": {k: asdict(v) for k, v in self._routes.items()},
            "bindings": {k: asdict(v) for k, v in self._bindings.items()},
        }
        try:
            atomic_write(self.path, json.dumps(payload, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.warning("code channel store write failed: %s", exc)

    # ── Routes ───────────────────────────────────────────────────────────

    def get_route(self, session_id: str) -> Route | None:
        with self._lock:
            self._ensure_loaded()
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
            self._routes[route.session_id] = stored
            self._flush()
            return stored

    def record_destination(self, session_id: str, channel_id: str) -> Route | None:
        """Record the channel a route opened; a recorded one never changes."""
        with self._lock:
            self._ensure_loaded()
            existing = self._routes.get(session_id)
            if existing is None or existing.channel_id:
                return existing
            stored = Route(**{**asdict(existing), "channel_id": channel_id})
            self._routes[session_id] = stored
            self._flush()
            return stored

    # ── Bindings ─────────────────────────────────────────────────────────

    def get_binding(self, channel_id: str) -> Binding | None:
        with self._lock:
            self._ensure_loaded()
            return self._bindings.get(channel_id)

    def bindings(self) -> list[Binding]:
        with self._lock:
            self._ensure_loaded()
            return list(self._bindings.values())

    def put_binding(self, binding: Binding) -> Binding:
        """Store *binding* unless the channel is already bound; returns the stored one."""
        with self._lock:
            self._ensure_loaded()
            existing = self._bindings.get(binding.channel_id)
            if existing is not None:
                return existing
            stored = Binding(**{**asdict(binding), "created_at": binding.created_at or time.time()})
            self._bindings[binding.channel_id] = stored
            self._flush()
            return stored

    def drop_binding(self, channel_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            if self._bindings.pop(channel_id, None) is None:
                return False
            self._flush()
            return True

    def replace_binding(self, binding: Binding) -> Binding:
        """Overwrite the channel's binding (its session was closed and remade)."""
        with self._lock:
            self._ensure_loaded()
            stored = Binding(**{**asdict(binding), "created_at": binding.created_at or time.time()})
            self._bindings[binding.channel_id] = stored
            self._flush()
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
