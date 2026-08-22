"""Durable Teams routing references (conversation id -> serviceUrl).

The Bot Framework does not let a bot look up where to reach a conversation: the
``serviceUrl`` arrives on an inbound activity and the bot is expected to remember
it. A purely in-memory map therefore loses every proactive destination on restart
-- a cron result, a subagent-completion notice, or a dashboard mirror leg has
nowhere to send until the user happens to speak first, and the send fails with a
"missing service_url" that reads like a bug rather than lost state.

The store is deliberately small and boring:

* **Not on the boot path.** Loading is lazy and off-loop; nothing reads this file
  while the gateway is starting (``AUTOSDE`` forbids new boot-path work, and the
  cost here scales with the number of conversations ever seen).
* **Failure is never fatal.** An unreadable or unwritable store degrades to the
  in-memory map -- the same behavior as having no store at all -- because losing
  a routing hint must not stop message delivery.
* **Fenced from the agent, directory and all.** The file maps allow-listed identities
  to the conversations they are reachable at, and ``teams/transport.py`` resolves an
  explicit ``user:<upn>`` send target through that map -- so a writable copy is a way to
  have somebody's cron result delivered to somebody else. It therefore lives in its own
  ``routing/`` directory under the data home, which ``security`` registers as a keystone
  leaf; see :data:`STORE_DIRNAME` for why the directory and not the file.
* **Owner-only on disk.** A serviceUrl is not itself a secret (it is a
  Microsoft-operated host, already visible in the inbound activity and in the
  outbound request URL), but the file also maps allow-listed operator UPNs to the
  conversations they use, which is personal data with no reason to be
  world-readable.
* **Bounded on BOTH sides of the boundary.** Eviction keeps a live gateway from
  growing the file forever, and the load path caps what it will adopt -- a file
  that arrived oversize (hand-edited, merged, or written by a build with a larger
  cap) must not turn the first eviction into an O(n^2) scan on the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.teams.client import connector_host_allowed

logger = logging.getLogger(__name__)

#: Cap on remembered conversations. A single operator DMs from a handful of
#: conversations; anything beyond this is churn, and the oldest is the least
#: likely to receive a proactive message.
_MAX_ENTRIES = 512

STORE_FILENAME = "teams_service_urls.json"

#: Own directory under the data home, and the DIRECTORY is what
#: ``security._CREW_SECRET_LEAVES`` registers. A file leaf covers only its exact name,
#: while ``atomic_write`` publishes through a ``tempfile.mkstemp`` sibling
#: (``tmpXXXXXXXX.tmp``) in the same parent -- so with the store loose in the data-home
#: root, an agent watching that directory could overwrite the temp file, in the window
#: before the rename, with routing of its choosing and have the rename publish it. A
#: directory entry matches every child including a random temp name, which closes the
#: window rather than narrowing it.
STORE_DIRNAME = "routing"


def _store_path() -> Path:
    return data_home() / STORE_DIRNAME / STORE_FILENAME


class ServiceUrlStore:
    """Remembers where each Teams conversation can be reached.

    In-memory map is authoritative for the process; the file is a warm start.
    Every filesystem touch runs in a worker thread, so no method blocks the loop.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._urls: dict[str, str] = {}
        self._seen: dict[str, float] = {}
        # Lowercased allow-listed identity -> conversation id. Persisted so the
        # dashboard's configured-target list is accurate after a restart instead
        # of telling the operator to message the bot again when it already knows
        # where they are. No new disclosure: these identities are the operator's
        # own ``teams.allowed_emails`` entries, already in config.json.
        self._by_identity: dict[str, str] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._dirty = False

    def path(self) -> Path:
        """Resolve the store path lazily.

        ``data_home()`` CREATES the directory and may run the legacy-home
        migration, so it must not be called at import or construction time.
        """
        if self._path is None:
            self._path = _store_path()
        return self._path

    # -- reads --------------------------------------------------------------
    def get(self, conversation_id: str) -> str:
        """The last-known serviceUrl for a conversation, or empty."""
        return self._urls.get(conversation_id, "")

    def conversation_for(self, identity: str) -> str:
        """The conversation a given (lowercased) identity was last seen in."""
        return self._by_identity.get(identity.lower(), "")

    @property
    def loaded(self) -> bool:
        """Whether the persisted rows have been read yet.

        Exposed for a SYNCHRONOUS caller that must tell "this conversation is not
        in the store" from "the store has not been read yet". The two look
        identical through :meth:`get` and :meth:`conversation_for`, and treating
        the second as the first turns a route that is on disk into a route that
        does not exist. See ``TeamsTransport.may_send_to``.
        """
        return self._loaded

    async def ensure_loaded(self) -> None:
        """Populate from disk once, off-loop. Never raises."""
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                rows, identities = await asyncio.to_thread(self._read)
            except Exception:
                logger.debug("Teams: serviceUrl store read failed", exc_info=True)
                self._loaded = True
                return
            # Flagged only AFTER the read returns. Setting it first would let a
            # concurrent inbound message take the fast path, see an empty map,
            # report the serviceUrl as newly learned and write a partial file --
            # and fail a proactive send with the "missing service_url" this module
            # exists to prevent. The lock serializes; the flag must not run ahead.
            self._loaded = True
            # Disk rows never overwrite something this process already learned:
            # a live inbound activity is fresher than any persisted value.
            for conversation_id, (url, seen_at) in rows.items():
                self._urls.setdefault(conversation_id, url)
                self._seen.setdefault(conversation_id, seen_at)
            for identity, conversation_id in identities.items():
                # Only adopt an identity whose conversation actually survived, so
                # a truncated or hand-edited file cannot advertise a target with
                # no route to it.
                if conversation_id in self._urls:
                    self._by_identity.setdefault(identity, conversation_id)

    # -- writes -------------------------------------------------------------
    def remember(self, conversation_id: str, service_url: str, *, identity: str = "") -> bool:
        """Record a conversation's serviceUrl (and optionally its owner).

        Returns True when the persisted copy is now stale and :meth:`flush`
        should run, so a caller on the hot inbound path can skip the write when
        nothing changed -- which is the common case, since a conversation's
        serviceUrl is stable for its lifetime.
        """
        if not conversation_id or not service_url:
            return False
        if not connector_host_allowed(service_url):
            # Never RECORD a destination the send would refuse. Reached only with an
            # attested inbound serviceUrl today, so this is a floor rather than a
            # live gate -- but it is the one that keeps the file's contents and the
            # send's contract from drifting apart.
            logger.warning("Teams: refusing to record a non-Connector serviceUrl")
            return False
        self._seen[conversation_id] = time.time()
        changed = self._urls.get(conversation_id) != service_url
        self._urls[conversation_id] = service_url
        if identity:
            key = identity.lower()
            if self._by_identity.get(key) != conversation_id:
                self._by_identity[key] = conversation_id
                changed = True
        if changed:
            self._evict()
            self._dirty = True
        return changed

    def forget(self, conversation_id: str) -> bool:
        """Drop a conversation the Connector says is gone. Returns True if changed.

        Capacity eviction is not enough on its own: once a user blocks the bot or
        removes the app, the route stays on disk and every cron result and mirror leg
        keeps 403-ing into a red badge with nothing to clear it. This is the path that
        clears it, called from the outbound side on a permanent refusal.

        The identity row goes with the conversation, so ``configured_targets`` stops
        advertising that person as reachable rather than reporting a destination that
        cannot be delivered to.
        """
        changed = self._urls.pop(conversation_id, None) is not None
        self._seen.pop(conversation_id, None)
        for identity, mapped in list(self._by_identity.items()):
            if mapped == conversation_id:
                self._by_identity.pop(identity, None)
                changed = True
        if changed:
            self._dirty = True
        return changed

    def _evict(self) -> None:
        """Drop the least-recently-seen entries past the cap.

        Ranks once and drops the whole surplus, rather than re-scanning for a new
        minimum per drop: this runs on the event loop, and a map that arrived
        oversize would otherwise cost O(n) per evicted entry.

        An evicted conversation's identity row goes with it, so the two maps cannot
        disagree about which conversations are still known.
        """
        surplus = len(self._urls) - _MAX_ENTRIES
        if surplus <= 0:
            return
        oldest = sorted(self._urls, key=lambda key: self._seen.get(key, 0.0))[:surplus]
        dropped = set(oldest)
        for key in oldest:
            self._urls.pop(key, None)
            self._seen.pop(key, None)
        for identity, conversation_id in list(self._by_identity.items()):
            if conversation_id in dropped:
                self._by_identity.pop(identity, None)

    async def flush(self) -> None:
        """Persist the map if it changed, off-loop. Never raises."""
        async with self._lock:
            if not self._dirty:
                return
            payload = {
                "conversations": {
                    conversation_id: {
                        "service_url": url,
                        "seen_at": self._seen.get(conversation_id, 0.0),
                    }
                    for conversation_id, url in self._urls.items()
                },
                "identities": dict(self._by_identity),
            }
            # Cleared BEFORE the write so a ``remember`` landing during it re-marks
            # the map and its change is not swallowed by this flush's success.
            self._dirty = False
            try:
                await asyncio.to_thread(self._write, payload)
            except Exception:
                # RE-MARK, because a transient failure must stay retryable. Leaving it
                # clear ends the retries entirely: ``remember`` only marks the map when
                # something CHANGES, and a conversation's serviceUrl is stable for its
                # lifetime -- so after one failed write the common case never marks it
                # again, nothing ever flushes, and the restart this store exists to
                # survive loses every proactive destination anyway.
                #
                # Setting it True cannot lose a concurrent change: it only asserts
                # "there is unpersisted state", which is true either way.
                self._dirty = True
                # A read-only or full home must not stop message delivery; the cost is a
                # cold proactive path if no later flush succeeds before the restart.
                logger.debug("Teams: serviceUrl store write failed", exc_info=True)

    # -- filesystem (worker-thread only) ------------------------------------
    def _read(self) -> tuple[dict[str, tuple[str, float]], dict[str, str]]:
        path = self.path()
        empty: tuple[dict[str, tuple[str, float]], dict[str, str]] = ({}, {})
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty
        except (OSError, UnicodeDecodeError):
            logger.debug("Teams: serviceUrl store unreadable at %s", path)
            return empty
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Teams: serviceUrl store is not valid JSON; ignoring it")
            return empty
        conversations = data.get("conversations") if isinstance(data, dict) else None
        if not isinstance(conversations, dict):
            return empty
        identities = data.get("identities") if isinstance(data, dict) else None
        by_identity = {
            key.lower(): value
            for key, value in (identities or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        out: dict[str, tuple[str, float]] = {}
        for conversation_id, row in conversations.items():
            if not isinstance(conversation_id, str) or not isinstance(row, dict):
                continue
            url = row.get("service_url")
            # Only a Bot Framework CONNECTOR host survives a reload, not merely an
            # https one. This file lives under the data home, so an injected agent
            # with write access could otherwise put `https://attacker.example` here,
            # wait for a restart, and collect the app bearer token from the first
            # proactive send -- the inbound path's binding of serviceUrl to the JWT's
            # own `serviceurl` claim is what stops a REPLAY, and it does not survive
            # persistence. The send refuses such a host too (that is the chokepoint);
            # refusing it here as well means a poisoned row never even advertises as
            # a reachable target.
            if not isinstance(url, str) or not connector_host_allowed(url):
                continue
            seen = row.get("seen_at")
            out[conversation_id] = (url, float(seen) if isinstance(seen, (int, float)) else 0.0)
        if len(out) > _MAX_ENTRIES:
            # Adopt only the newest rows. A file that arrived oversize would
            # otherwise be loaded whole and then evicted down on the event loop.
            newest = sorted(out, key=lambda key: out[key][1], reverse=True)[:_MAX_ENTRIES]
            keep = set(newest)
            out = {key: out[key] for key in newest}
            by_identity = {
                identity: conversation_id
                for identity, conversation_id in by_identity.items()
                if conversation_id in keep
            }
        return out, by_identity

    def _write(self, payload: dict[str, Any]) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only: the file maps allow-listed operator UPNs to conversations.
        # restrict_on_error="warn" keeps the honest degradation of this module --
        # a filesystem that cannot express the mode (some Windows volumes) must
        # not turn a routing hint into a delivery failure.
        atomic_write(
            path,
            json.dumps(payload, indent=2, sort_keys=True),
            restrict_to_owner=True,
            restrict_on_error="warn",
        )
