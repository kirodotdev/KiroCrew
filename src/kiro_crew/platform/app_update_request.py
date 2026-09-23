"""An agent's request that a packaged desktop install be updated.

A packaged install (``dmg``/``appimage``/``deb``/``rpm``/``nsis``) is updated by
the desktop app's own updater, and the only thing that can start that install is
a human click in Settings › About inside the app. This module records that a
request was MADE, so that click can be offered with context — which version,
who asked, when — and nothing more.

The record is deliberately not an authority. It carries no nonce, no token,
nothing an approval could present: an agent that arms it gains exactly the
ability to have the About panel say "an agent requested an update". There is
no gateway endpoint that turns this record into an install, so an agent that
can read its own request (it can — the record is not secret) can do nothing
with what it reads. That is the whole design: the approval IS the click, in the
renderer, by a person who is present.

The person this exists for is ABSENT when the request is made — on Slack, on
WeCom, behind a cron job — and reaches Settings › About hours later, following
the notification. So the record has to still be there when they arrive. It is
persisted to the data home and lives for a day: long enough to outlast a
working day and a gateway restart, short enough that a request nobody acted on
does not become a standing prompt. Declining or installing ends it early.

Each request carries an id. A decline names the id the panel SHOWED, and only
that request is removed: a click on a card rendering request A must not erase a
request B an agent made after the render — B is a decision the user has not
seen yet.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import make_owner_only_dir

logger = logging.getLogger(__name__)

#: How long a request stays visible. Sized for the user who is not at the
#: machine when it is made: a day outlasts a working shift and a gateway
#: restart, and the notification remains the durable pointer meanwhile.
REQUEST_TTL_SECS = 24 * 60 * 60

#: Not under ``trust/`` — that directory is the sensitive-leaf keystone. This
#: record is not sensitive: it holds nothing an approval could present, and an
#: agent reading it learns only what the About panel would show it anyway.
_FILENAME = "pending-app-update-request.json"


@dataclass(frozen=True)
class AppUpdateRequest:
    #: Fresh per request. What a decline names, so a stale click cannot remove a
    #: request the user has not seen.
    request_id: str
    #: The version the requester named, or ``""`` for "whatever the feed offers".
    #: Display-only: the app's updater decides what actually downloads, and the
    #: About panel says so when the two differ.
    target_version: str
    #: Who asked — a session key, a user, or ``"dashboard"``. Display-only.
    requested_by: str
    #: Wall-clock seconds, for the panel's "asked at" and the expiry countdown.
    armed_at: float
    expires_at: float

    def expires_in(self, now: float) -> int:
        return max(0, int(self.expires_at - now))

    def to_public(self, now: float) -> dict[str, object]:
        return {
            "armed": True,
            "managed_by": "electron",
            "request_id": self.request_id,
            "version": self.target_version,
            "requested_by": self.requested_by,
            "armed_at": self.armed_at,
            "expires_in": self.expires_in(now),
        }

    def same_ask_as(self, other: "AppUpdateRequest | None") -> bool:
        """Is *other* the same request in every way a user would notice?

        The re-notify guard reads this: an agent turn that loops on ``arm`` must
        not ring the bell once per iteration for one ask.
        """
        return (
            other is not None
            and other.target_version == self.target_version
            and other.requested_by == self.requested_by
        )


def _path() -> Path:
    return data_home() / _FILENAME


class AppUpdateRequests:
    """Holds at most one request, on disk. Last writer wins; arming grants nothing."""

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        path: Callable[[], Path] | None = None,
    ) -> None:
        self._now = now or time.time
        self._path = path or _path
        self._lock = threading.Lock()

    # ── storage ──────────────────────────────────────────────────────────────

    def _read(self) -> AppUpdateRequest | None:
        """The record on disk, or ``None`` for anything this module cannot vouch for.

        The file is not secret, and it is not fenced from the agent either — an
        agent can already create it legitimately through the arm endpoint, so
        hiding it would defend nothing. What the file must not be is TRUSTED:
        every field is re-validated here as if it came off the wire. A
        non-finite or far-future ``expires_at`` (``json.loads`` accepts
        ``Infinity``) would otherwise never lapse and would overflow
        ``expires_in``; a string longer than the arm endpoint permits would
        reach the panel unbounded. Anything out of shape reads as "no request".
        """
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            request_id = raw.get("request_id")
            target_version = raw.get("target_version", "")
            requested_by = raw.get("requested_by", "")
            armed_at = raw.get("armed_at")
            expires_at = raw.get("expires_at")
            if not isinstance(request_id, str) or not (1 <= len(request_id) <= 32):
                return None
            if not isinstance(target_version, str) or len(target_version) > 64:
                return None
            if not isinstance(requested_by, str) or len(requested_by) > 64:
                return None
            if isinstance(armed_at, bool) or not isinstance(armed_at, (int, float)):
                return None
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                return None
            if not math.isfinite(armed_at) or not math.isfinite(expires_at):
                return None
            now = self._now()
            # The arm endpoint is the only writer that matters, and it stamps
            # `armed_at = now` and `expires_at = now + TTL`. A record claiming to
            # have been armed in the future, or to last longer than the TTL
            # permits, did not come from it.
            if armed_at > now + 60 or expires_at > now + REQUEST_TTL_SECS + 60:
                return None
            return AppUpdateRequest(
                request_id=request_id,
                target_version=target_version,
                requested_by=requested_by,
                armed_at=float(armed_at),
                expires_at=float(expires_at),
            )
        except (OSError, ValueError, KeyError, TypeError):
            # Absent, unreadable or malformed all read as "no request".
            return None

    def _write(self, req: AppUpdateRequest) -> None:
        path = self._path()
        make_owner_only_dir(path.parent)
        tmp = path.with_name(f"{path.name}.{req.request_id}.tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "request_id": req.request_id,
                        "target_version": req.target_version,
                        "requested_by": req.requested_by,
                        "armed_at": req.armed_at,
                        "expires_at": req.expires_at,
                    },
                    handle,
                )
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _unlink(self) -> None:
        try:
            self._path().unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove the app update request", exc_info=True)

    # ── api ──────────────────────────────────────────────────────────────────

    def arm(self, *, target_version: str, requested_by: str) -> tuple[AppUpdateRequest, bool]:
        """Record a request. Returns ``(request, is_new_ask)``.

        ``is_new_ask`` is False when a live request for the same version from
        the same requester already existed — the caller uses it to skip the
        notification, so a looping agent turn rings the bell once.
        """
        now = self._now()
        req = AppUpdateRequest(
            request_id=secrets.token_hex(8),
            target_version=str(target_version or "")[:64],
            requested_by=str(requested_by or "dashboard")[:64],
            armed_at=now,
            expires_at=now + REQUEST_TTL_SECS,
        )
        with self._lock:
            live = self._live_locked()
            if req.same_ask_as(live):
                # Same ask, already live: keep the existing record (and its id,
                # which a rendered card may be holding) rather than replacing it.
                assert live is not None
                return live, False
            self._write(req)
        return req, True

    def _live_locked(self) -> AppUpdateRequest | None:
        req = self._read()
        if req is None:
            return None
        if self._now() >= req.expires_at:
            self._unlink()
            return None
        return req

    def current(self) -> AppUpdateRequest | None:
        """The live request, or ``None`` when absent or lapsed."""
        with self._lock:
            return self._live_locked()

    def decline(self, request_id: str) -> bool:
        """Remove the request *iff* it is the one named. ``True`` if removed.

        Read-compare-remove under one lock, so a decline that read A cannot
        remove a B that an arm swapped in between. A mismatch leaves the file
        alone: the newer request is genuine, it is just not what this click
        declined.
        """
        with self._lock:
            live = self._live_locked()
            if live is None or live.request_id != request_id:
                return False
            self._unlink()
            return True

    def clear(self) -> None:
        """Drop whatever is there. A test seam and the install's own cleanup."""
        with self._lock:
            self._unlink()


_requests: AppUpdateRequests | None = None
_requests_lock = threading.Lock()


def get_app_update_requests() -> AppUpdateRequests:
    global _requests
    with _requests_lock:
        if _requests is None:
            _requests = AppUpdateRequests()
        return _requests


__all__ = [
    "REQUEST_TTL_SECS",
    "AppUpdateRequest",
    "AppUpdateRequests",
    "get_app_update_requests",
]
