"""What the managed-environment client may touch, and when it holds off.

* **Own-ids ledger** (``ledger.json``): the id of every environment this Crew's
  ``CreateSandbox`` returned. Every id-taking call needs an id in it, so an
  environment another client made (the Kiro web app, the IDE, another Crew) is listed
  and left alone. ``displayName`` is never a key: a name match could reach another
  client's environment.
* **Pause after a lost Create reply** (``create-unconfirmed.json``): a ``CreateSandbox``
  whose reply was lost may have made an environment with no ledger row, so every
  attempt is recorded before it is sent and cleared only once its outcome is known
  (its id in the ledger, or a reply that made nothing). While a record stands, new
  creates wait :data:`HOLD_AFTER_UNCONFIRMED_S` from its time.
* **Backoff**: a 401, 403, 429 or 5XX reply holds every call for 1, 2, then 5 minutes
  (:data:`BACKOFF_STEPS_S`) as such replies repeat. A good reply ends it, and so does a
  new sign-in. It is never permanent. The account's cap on ``CreateSandbox`` holds
  nothing: only a Delete frees a place, and Delete must still run.

State directory 0700, files 0600. No secret is stored.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import mkdirs_owner_only

#: How long new creates wait after a create whose reply was lost.
HOLD_AFTER_UNCONFIRMED_S = 600.0
#: The hold after each failing reply in a row; the last step repeats.
BACKOFF_STEPS_S = (60.0, 120.0, 300.0)
#: Replies that start or extend the backoff.
BACKOFF_STATUSES = (401, 403, 429)
#: The account's cap on CreateSandbox: 402 or 429 with this kind. No backoff.
QUOTA_KIND = "ServiceQuotaExceededException"
QUOTA_STATUSES = (402, 429)
LEDGER_NAME = "ledger.json"
UNCONFIRMED_NAME = "create-unconfirmed.json"


class GuardRefused(Exception):
    """A call the guard refused before anything was sent. ``reason`` is a stable word."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def is_quota_refusal(op: str, status: int | None, kind: str) -> bool:
    return op == "CreateSandbox" and status in QUOTA_STATUSES and kind == QUOTA_KIND


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


class SandboxGuard:
    """One guard per gateway. ``now`` is injected by tests."""

    def __init__(self, state_dir: str | os.PathLike[str], *, now: Callable[[], float] = time.time):
        self._dir = Path(state_dir)
        self._now = now
        self._lock = threading.Lock()
        self._failures = 0
        self._hold_until = 0.0
        self._hold_sign_in = ""
        try:
            mkdirs_owner_only(self._dir)
        except OSError as e:
            raise GuardRefused(
                "state_dir", f"cannot prepare {self._dir}: {type(e).__name__}"
            ) from None

    # -- state files ---------------------------------------------------------------

    def _read(self, name: str, default: Any) -> Any:
        try:
            raw = (self._dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        except OSError as e:
            raise GuardRefused(
                "state_unreadable", f"cannot read {name}: {type(e).__name__}"
            ) from None
        try:
            return json.loads(raw)
        except ValueError:
            # A damaged ledger must not silently reset what the client may touch.
            raise GuardRefused("state_corrupt", f"{name} is not valid JSON") from None

    def _write(self, name: str, data: Any) -> None:
        atomic_write(self._dir / name, json.dumps(data, indent=1), fsync=True, mode=0o600)

    # -- ledger --------------------------------------------------------------------

    def ledger(self) -> list[dict]:
        data = self._read(LEDGER_NAME, {"sandboxes": []})
        rows = data.get("sandboxes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise GuardRefused("state_corrupt", f"{LEDGER_NAME} has no sandboxes list")
        return [r for r in rows if isinstance(r, dict)]

    def own_ids(self, *, deleted: bool = False) -> set[str]:
        """Ledger ids; a deleted row counts only with ``deleted=True``."""
        return {
            r["execEnvironmentId"]
            for r in self.ledger()
            if isinstance(r.get("execEnvironmentId"), str)
            and r["execEnvironmentId"]
            and (deleted or "deletedAt" not in r)
        }

    def require_own(self, exec_environment_id: str, *, deleted: bool = False) -> None:
        """Refuse an id not in the ledger. Only Delete passes ``deleted=True``, so a
        repeated Delete still reads its 404 as gone."""
        if not exec_environment_id or exec_environment_id not in self.own_ids(deleted=deleted):
            raise GuardRefused("not_own_id", "Crew did not create that environment.")

    def record_created(self, reply: dict, display_name: str) -> dict:
        """Add the id a CreateSandbox reply returned. A reply with no id counts as lost."""
        sid = reply.get("execEnvironmentId")
        if not isinstance(sid, str) or not sid:
            # The attempt's record (begin_create) stays, so new creates pause.
            raise GuardRefused(
                "create_no_id",
                f"The service made no clear reply for {display_name!r}. Crew can create again "
                f"in {_minutes(HOLD_AFTER_UNCONFIRMED_S)} minutes.",
            )
        row = {"execEnvironmentId": sid, "displayName": display_name, "createdAt": self._now()}
        with self._lock:
            rows = self.ledger()
            if all(r.get("execEnvironmentId") != sid for r in rows):
                rows.append(row)
                self._write(LEDGER_NAME, {"sandboxes": rows})
        return row

    def record_deleted(self, exec_environment_id: str) -> None:
        """Mark a ledger row deleted; it is kept so a repeated Delete reads 404 as gone."""
        with self._lock:
            rows = self.ledger()
            for r in rows:
                if r.get("execEnvironmentId") == exec_environment_id:
                    r.setdefault("deletedAt", self._now())
            self._write(LEDGER_NAME, {"sandboxes": rows})

    # -- the pause after a lost Create reply -----------------------------------------

    def begin_create(self, display_name: str) -> None:
        """Record a CreateSandbox before it is sent. A record that can't be written
        refuses the create: without it, a lost reply could leave an environment Crew
        neither knows nor pauses for."""
        try:
            with self._lock:
                self._write(UNCONFIRMED_NAME, {"at": self._now(), "displayName": display_name})
        except OSError as e:
            raise GuardRefused(
                "state_unwritable", f"cannot write {UNCONFIRMED_NAME}: {type(e).__name__}"
            ) from None

    def end_create(self) -> None:
        """The create's outcome is known (its id is in the ledger, or nothing was made)."""
        with self._lock:
            (self._dir / UNCONFIRMED_NAME).unlink(missing_ok=True)

    def check_unconfirmed(self) -> None:
        """Before a CreateSandbox: refuse inside the pause after a lost reply."""
        path = self._dir / UNCONFIRMED_NAME
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        except OSError as e:
            raise GuardRefused(
                "state_unreadable", f"cannot read {UNCONFIRMED_NAME}: {type(e).__name__}"
            ) from None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        at = data.get("at") if isinstance(data, dict) else None
        # A damaged record holds from the file's time: it can't end the pause early.
        if (
            not isinstance(at, (int, float))
            or isinstance(at, bool)
            or not math.isfinite(at)
            or at > self._now()
        ):
            at = mtime
        left = HOLD_AFTER_UNCONFIRMED_S - (self._now() - float(at))
        if left > 0:
            raise GuardRefused(
                "create_unconfirmed",
                "Crew's last create got no clear reply, so an environment may exist that Crew "
                f"can't see. Crew can create again in {_minutes(left)} minutes.",
            )

    # -- backoff -------------------------------------------------------------------

    def check_backoff(self, sign_in: str) -> None:
        """Refuse a call inside the hold. A different sign-in than the one held ends it."""
        with self._lock:
            if self._hold_until and sign_in != self._hold_sign_in:
                self._failures, self._hold_until = 0, 0.0
            left = self._hold_until - self._now()
        if left > 0:
            raise GuardRefused(
                "backoff",
                f"The service is not answering right now. Crew tries again in {_minutes(left)} "
                "minutes, or sooner after you sign in again.",
            )

    def note_reply(self, op: str, status: int, kind: str, sign_in: str) -> None:
        """Count a reply: a failing one extends the hold, a good one ends it."""
        failing = (status in BACKOFF_STATUSES or status >= 500) and not is_quota_refusal(
            op, status, kind
        )
        with self._lock:
            if not failing:
                if status < 400:
                    self._failures, self._hold_until = 0, 0.0
                return
            step = BACKOFF_STEPS_S[min(self._failures, len(BACKOFF_STEPS_S) - 1)]
            self._failures += 1
            self._hold_until = self._now() + step
            self._hold_sign_in = sign_in
