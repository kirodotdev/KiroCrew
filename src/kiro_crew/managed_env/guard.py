"""Guard for the managed-environment client: its memory of what it may touch and how often.

The List is the user's own environments from every client (the Kiro web app, the IDE,
another Crew install and this one), so an id this client did not create is listed and
left alone. What the guard keeps and checks:

* **own-ids ledger** (``sandbox-ledger.json``): the id of every environment this client's
  ``CreateSandbox`` returned. It is the key for Delete; ``displayName`` is never a key (a
  name match could delete another client's environment).
* **the accepted set** (memory only): the ids of the current sign-in's latest full List
  whose ``displayName`` is a Crew launch's marker (:func:`is_crew_marker`), plus the ones
  the user's Set up Crew adopted. Get / Stop / Resume / UpdateSandbox / StartSshSession take
  the ledger or this set, and only while the sign-in is the one the set was filled under.
* **hold after an unconfirmed create** (``sandbox-unconfirmed.json``): a ``CreateSandbox``
  whose answer was lost (no answer, one that can't be read, or a 200 with no id) may have
  made an environment with no ledger row. The guard records the attempt, and new launches
  wait :data:`HOLD_AFTER_UNCONFIRMED_S` after it. A record that can't be read holds from the
  file's time.
* **stop rules**: a 401, 403, 429 or 5XX answer stops every later call until the process
  ends, kept in memory only. Not on the account's cap (a CreateSandbox 429 or 402
  ``ServiceQuotaExceededException``), which only a delete clears, so the user can still
  delete; a throttling 429 still stops. Not on a 403 ``ForbiddenException`` on a per-box
  action (:data:`POLICY_OPS`: the organization's policy doesn't allow managed environments
  for the account): that refuses the action only, and a box whose StartSshSession got it is
  asked again only after :data:`POLICY_HOLD_S`, sooner once a call for the box succeeds, a
  new sign-in comes, or a List shows the box :data:`POLICY_LIST_ENDS_HOLD_S` after the
  refusal.

The service's own cap on live environments governs; the client adds none.

State directory 0700, files 0600. No secret is stored: no bearer, no ssh session.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from kiro_crew.managed_env.call_log import GUARD_PRODUCT

log = logging.getLogger(__name__)

#: How long new launches wait after a create whose answer was lost.
HOLD_AFTER_UNCONFIRMED_S = 600.0
#: The account's cap on CreateSandbox: the service answers 402 with this kind, or 429.
#: Nothing is created, and only a DeleteSandbox frees a place.
QUOTA_KIND = "ServiceQuotaExceededException"
QUOTA_STATUSES = (402, 429)
#: The service's 403 when the organization's policy doesn't allow managed environments for the
#: signed-in account. It refuses that one box's action; the List, GetUserInfo and every other
#: box keep working.
POLICY_KIND = "ForbiddenException"
POLICY_STATUS = 403
POLICY_OPS = ("StartSshSession", "CreateSandbox", "ResumeSandbox", "StartEnvironment")
POLICY_TEXT = (
    "Your organization's policy doesn't allow this for your account yet. "
    "Ask your Kiro admin to allow managed environments."
)
#: The refusal's key: the hold's GuardRefused reason, the engine's code and the ssh-session
#: route's answer kind, which the mint (mint_and_attach) prints as the plain line alone.
POLICY_KIND_WORD = "policy_denied"
#: After a box's StartSshSession got the policy 403, its ssh asks are refused from memory (no
#: call) for this long, so the tunnel's token refresh can't turn one refusal into a call loop.
POLICY_HOLD_S = 300.0
#: A List that shows the box ends its hold once the refusal is this old, so a box the admin has
#: allowed since is asked again at its next ssh ask (one call), not after POLICY_HOLD_S. The
#: floor keeps the 3 s List of a moving row from undoing the hold. The line stays: the List says
#: nothing of the policy, only a call for the box that succeeds clears it.
POLICY_LIST_ENDS_HOLD_S = 30.0
#: The call log's ``guard`` word.
MODE_PRODUCT = GUARD_PRODUCT
LEDGER_NAME = "sandbox-ledger.json"
UNCONFIRMED_NAME = "sandbox-unconfirmed.json"
#: The ids whose Remote Crew row the lane wrote from the List, and those a user removed by
#: hand, so a removed row stays removed across sign-ins. Ids only. ``adopted``: boxes with no
#: record where the user's Set up Crew found Crew's own gateway.
LIST_ROWS_NAME = "sandbox-list-rows.json"
#: The Remote Crew rows (by instance id) the lane turned off while their environment read
#: STOPPING or STOPPED, so RUNNING turns on only those, after a restart too. Ids only.
STOPPED_OFF_NAME = "sandbox-stopped-off.json"
#: What a user-asked id (a listed box with no record, on the user's click) may do.
ASKED_OPS = ("resume", "get", "read")
#: A read ticket armed longer ago than this is refused.
READ_TICKET_S = 60.0

#: The Crew launch's own displayName: its tag ``kc-<6 hex>`` (core cloud/launch_job.py, the
#: launch tag), or ``<crew name>-kc-<6 hex>``, the crew name the user typed first and one
#: hyphen. The crew part is a valid crew name (2 to 32 lowercase letters, digits or single
#: hyphens; the engine's rule).
CREW_MARKER_RE = re.compile(r"(?:(?P<crew>[a-z][a-z0-9-]{0,30}[a-z0-9])-)?(?P<tag>kc-[0-9a-f]{6})")


def _crew_marker(display_name: object) -> Optional[re.Match]:
    if not isinstance(display_name, str):
        return None
    m = CREW_MARKER_RE.fullmatch(display_name)
    if m is None or "--" in (m.group("crew") or ""):
        return None
    return m


def is_crew_marker(display_name: object) -> bool:
    """True for the launch's tag, alone or after a crew name (a full match, case-sensitive)."""
    return _crew_marker(display_name) is not None


def crew_name_of(display_name: object) -> str:
    """The crew name a Crew-launched environment's displayName carries, or ``""`` (a bare
    tag, or not a Crew launch)."""
    m = _crew_marker(display_name)
    return (m.group("crew") or "") if m else ""


def crew_tag_of(display_name: object) -> str:
    """The launch tag in a Crew-launched environment's displayName, or ``""``."""
    m = _crew_marker(display_name)
    return m.group("tag") if m else ""


#: The service's two answers to a rename it doesn't apply: its rename switch is off (a 400),
#: or the switch can't be read (a 503 that is not an outage: "the caller may try again").
#: These are the service's exact words, matched, never shown (the page shows RENAME_TEXT).
RENAME_OFF_TEXT = "Renaming a persistent sandbox isn't available yet."
RENAME_SWITCH_UNREADABLE_TEXT = (
    "Renaming a persistent sandbox can't be checked right now. Try again."
)
RENAME_SWITCH_UNREADABLE_KIND = "ServiceUnavailableException"


def is_rename_switch_unreadable(op: str, status: Optional[int], kind: str, message: str) -> bool:
    """The service's own 503 for an unreadable rename switch: UpdateSandbox, 503, its kind and
    its exact message. The client doesn't stop on it for a rename; any other 5XX, this kind
    with another message included, still stops it."""
    return (
        op == "UpdateSandbox"
        and status == 503
        and kind == RENAME_SWITCH_UNREADABLE_KIND
        and message == RENAME_SWITCH_UNREADABLE_TEXT
    )


SOURCE = "kiro-crew"


class GuardRefused(Exception):
    """The guard says no. ``reason`` is a short stable key, the message is for people."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class ClientStopped(GuardRefused):
    """A stop rule fired earlier in this process; no further call is made. ``by_service``:
    the stop came from the service's own answer (a 5XX or a 429), not a rule about the user's
    environments, so a row can say plainly that the service can't be reached."""

    def __init__(self, reason: str, message: str, by_service: bool = False):
        super().__init__(reason, message)
        self.by_service = by_service


#: The plain lines a row (or the Launch form) shows once the guard has stopped the client.
#: The guard's own text stays in the guard and the log; no row shows it. ``unreachable``: the
#: stop came from the service's answer (a 5XX or 429); ``stopped``: any other stop rule;
#: ``retrying``: a List outage, retried on a capped backoff.
SERVICE_TEXT = {
    "unreachable": "Can't reach the sandbox service right now. Restart the dashboard to try again.",
    "stopped": "Crew stopped calling the sandbox service to keep your sandboxes safe. "
    "Restart the dashboard to try again.",
    "retrying": "Can't reach the sandbox service right now; retrying.",
    # a List outage inside Create's preflight (Create itself never retries)
    "try_later": "Can't reach the sandbox service right now. Try again in a minute.",
}


def stopped_line(by_service: bool) -> str:
    return SERVICE_TEXT["unreachable" if by_service else "stopped"]


def is_quota_refusal(op: str, status: int | None, kind: str) -> bool:
    """A CreateSandbox refused at the account's cap: 402 or 429 ``ServiceQuotaExceeded``."""
    return op == "CreateSandbox" and status in QUOTA_STATUSES and kind == QUOTA_KIND


def is_policy_refusal(op: str, status: int | None, kind: str) -> bool:
    """A per-box action refused by the organization's policy: 403 ``ForbiddenException`` on
    one of :data:`POLICY_OPS` (by its kind, not its status alone)."""
    return op in POLICY_OPS and status == POLICY_STATUS and kind == POLICY_KIND


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


def unconfirmed_text(display_name: str, seconds_left: float) -> str:
    """The ``create_unconfirmed`` refusal: names the lost attempt and the wait."""
    m = _minutes(seconds_left)
    name = display_name or "it"
    tag = f" ({display_name})" if display_name else ""
    return (
        f"Crew's last launch{tag} got no clear answer from the sandbox service, so a sandbox may "
        "have been made without Crew knowing it. "
        f"If {name} is listed under Sandboxes without a crew (made outside this Crew), "
        f"delete it in the Kiro web app. Crew can launch again in {m} "
        f"minute{'' if m == 1 else 's'}."
    )


class SandboxGuard:
    """One guard per process. ``now`` exists for tests."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        now: Callable[[], float] = time.time,
    ):
        self._dir = Path(state_dir)
        self._now = now
        self._stopped: str = ""
        self._stopped_by_service = False  # the stop came from a 5XX or 429 answer
        # The marker ids of the current sign-in's latest full List. In memory only, never in
        # the ledger; cleared on a sign-in change or a stop.
        self._accepted: set[str] = set()
        self._accepted_fp: str = ""
        # Every id of that List (any name), the user-asked ids and their sign-in, and the
        # one-shot read tickets by id (armed at, by the clock). All memory only.
        self._listed: set[str] = set()
        self._asked: set[str] = set()
        self._asked_fp: str = ""
        self._tickets: dict[str, float] = {}
        self._ticket_lock = threading.Lock()
        # the boxes whose StartSshSession got the policy 403, and when (memory only)
        self._policy: dict[str, float] = {}
        self._policy_lock = threading.Lock()
        self._ensure_dir()

    @property
    def mode(self) -> str:
        """The call log's guard word."""
        return MODE_PRODUCT

    # -- state files ---------------------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return self._dir / LEDGER_NAME

    @property
    def unconfirmed_path(self) -> Path:
        return self._dir / UNCONFIRMED_NAME

    def _ensure_dir(self) -> None:
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = self._dir.stat()
            if not stat.S_ISDIR(st.st_mode):
                raise GuardRefused("state_dir", f"{self._dir} is not a directory")
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise GuardRefused("state_dir", f"{self._dir} is not owned by this user")
            if stat.S_IMODE(st.st_mode) != 0o700:
                self._dir.chmod(0o700)
        except OSError as e:
            raise GuardRefused("state_dir", f"cannot prepare {self._dir}: {e}") from None

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl  # POSIX only: the lane is offered on POSIX gateways only

        fd = os.open(self._dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return default
        except OSError as e:
            raise GuardRefused("state_unreadable", f"cannot read {path.name}: {e}") from None
        try:
            return json.loads(raw)
        except ValueError:
            # A damaged ledger must not silently reset what the client may touch.
            raise GuardRefused("state_corrupt", f"{path.name} is not valid JSON") from None

    def _write_json(self, path: Path, data: Any) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=path.name + ".")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- ledger --------------------------------------------------------------------

    def ledger(self) -> list[dict]:
        data = self._read_json(self.ledger_path, {"sandboxes": []})
        rows = data.get("sandboxes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise GuardRefused("state_corrupt", f"{LEDGER_NAME} has no sandboxes list")
        return rows

    def own_ids(self) -> set[str]:
        return {r["execEnvironmentId"] for r in self.ledger() if r.get("execEnvironmentId")}

    def is_own(self, exec_environment_id: str) -> bool:
        return bool(exec_environment_id) and exec_environment_id in self.own_ids()

    def require_own(self, exec_environment_id: str) -> None:
        """Delete goes through here: an id not in the ledger is refused."""
        self.check_not_stopped()
        if not self.is_own(exec_environment_id):
            raise GuardRefused(
                "not_own_id",
                "refused: that sandbox id is not in this client's ledger (own ids only)",
            )

    # -- the accepted set ------------------------------------------------------------

    def accept_listed(self, entries: Iterable[dict], fingerprint: str) -> None:
        """Refill the accepted set from a full List of the sign-in *fingerprint* names: the
        listed ids whose displayName is a Crew launch's (``is_crew_marker``). An empty
        *fingerprint* (the lane off, no sign-in) leaves it empty."""
        if not fingerprint or self._stopped:
            self.clear_accepted()
            return
        entries = list(entries)
        listed = {str(e.get("execEnvironmentId")) for e in entries if e.get("execEnvironmentId")}
        # An adopted box is accepted again on each List that lists it (and after a restart),
        # only while this user's List has it; a removed row wins.
        try:
            _, removed = self.list_rows()
            adopted = self.adopted_ids() - removed
        except GuardRefused:
            log.warning("could not read %s; no adopted box is accepted", LIST_ROWS_NAME)
            adopted = set()
        if fingerprint != self._accepted_fp:
            self.clear_asked()
        self._accepted = {
            str(e.get("execEnvironmentId"))
            for e in entries
            if e.get("execEnvironmentId") and is_crew_marker(e.get("displayName"))
        } | (listed & adopted)
        self._accepted_fp = fingerprint
        self._listed = listed

    def clear_accepted(self) -> None:
        self._accepted = set()
        self._accepted_fp = ""
        self._listed = set()
        self.clear_asked()

    # -- the user-asked set and the read ticket ----------------------------------------

    def ask(self, exec_environment_id: str, fingerprint: str) -> bool:
        """The user's click on Resume or Set up Crew. True, and the id user-asked, only for an id
        in the latest full List of the sign-in *fingerprint* names, with no record (not in the
        ledger, not accepted); never after a stop."""
        sid = exec_environment_id
        if self._stopped or not fingerprint or not sid:
            return False
        if fingerprint != self._accepted_fp or sid not in self._listed:
            return False
        if self.is_actionable(sid):
            return False
        if self._asked_fp != fingerprint:
            self.clear_asked()
        self._asked.add(sid)
        self._asked_fp = fingerprint
        return True

    def is_asked(self, exec_environment_id: str) -> bool:
        return bool(exec_environment_id) and exec_environment_id in self._asked

    def clear_asked(self) -> None:
        self._asked = set()
        self._asked_fp = ""
        with self._ticket_lock:
            self._tickets.clear()

    def require_asked(
        self, exec_environment_id: str, op: str, current: Optional[Callable[[], str]] = None
    ) -> None:
        """A user-asked id may do :data:`ASKED_OPS` only (ResumeSandbox, GetSandbox for its wait,
        and the one read's StartSshSession), and only while *current* (the client's fingerprint,
        read in the call's own thread) names the sign-in it was asked under. A mismatch clears the
        set and refuses."""
        self.check_not_stopped()
        sid = exec_environment_id
        if op in ASKED_OPS and sid and sid in self._asked:
            if current is not None and current() == self._asked_fp:
                return
            self.clear_asked()
        raise GuardRefused(
            "not_own_id",
            "refused: that sandbox id is not one of this user's Crew sandboxes",
        )

    def arm_ticket(self, exec_environment_id: str) -> None:
        """Just before the one read's exec: its ssh may take one StartSshSession."""
        with self._ticket_lock:
            self._tickets[exec_environment_id] = self._now()

    def disarm_ticket(self, exec_environment_id: str) -> None:
        """After the exec, in a ``finally``."""
        with self._ticket_lock:
            self._tickets.pop(exec_environment_id, None)

    def take_ticket(self, exec_environment_id: str) -> bool:
        """The ssh-session route, for a user-asked id: one use, and only within
        :data:`READ_TICKET_S` of the arm."""
        with self._ticket_lock:
            armed = self._tickets.pop(exec_environment_id, None)
        if armed is None or exec_environment_id not in self._asked:
            return False
        return self._now() - armed <= READ_TICKET_S

    def can_adopt(
        self, exec_environment_id: str, current: Optional[Callable[[], str]] = None
    ) -> bool:
        """Read before any write: True only while the id is user-asked, the List's sign-in is
        the one it was asked under, and *current* (the client's fingerprint, read in the call's
        own thread) still names it. A fingerprint mismatch clears the set."""
        sid = exec_environment_id
        if self._stopped or not sid or sid not in self._asked or not self._asked_fp:
            return False
        if self._asked_fp != self._accepted_fp:
            return False
        if current is None or current() != self._asked_fp:
            self.clear_asked()
            return False
        return True

    def adopt(self, exec_environment_id: str) -> None:
        """Found: the user-asked id joins the accepted set now and the ``adopted`` list (never
        the ledger), so it stays actionable on the next List and after a restart."""
        sid = exec_environment_id
        if not sid or sid not in self._asked or self._asked_fp != self._accepted_fp:
            raise GuardRefused("not_own_id", "refused: that sandbox was not asked about here")
        self.note_list_rows(registered=[sid], adopted=[sid])
        self._asked.discard(sid)
        self._accepted.add(sid)

    @property
    def accepted_fingerprint(self) -> str:
        """The sign-in the accepted set was filled under, or ``""``."""
        return self._accepted_fp

    def is_actionable(self, exec_environment_id: str) -> bool:
        """The ledger, or the accepted set (memory only: no fingerprint read, no call)."""
        if not exec_environment_id:
            return False
        return exec_environment_id in self._accepted or self.is_own(exec_environment_id)

    def is_accepted(self, exec_environment_id: str) -> bool:
        return bool(exec_environment_id) and exec_environment_id in self._accepted

    def require_actionable(
        self, exec_environment_id: str, current: Optional[Callable[[], str]] = None
    ) -> None:
        """Get, Stop, Resume, UpdateSandbox and StartSshSession: the ledger or the accepted set.
        Delete keeps :meth:`require_own` (ledger only).

        An accepted (not ledger) id is taken only while *current* (the client's sign-in
        fingerprint, read in the call's own thread) still names the sign-in the set was filled
        under. A sign-in change before the next List clears the set and refuses: the previous
        user's box never gets a call with the new user's bearer."""
        self.check_not_stopped()
        if self.is_own(exec_environment_id):
            return
        if exec_environment_id and exec_environment_id in self._accepted:
            if current is not None and current() == self._accepted_fp:
                return
            self.clear_accepted()
        raise GuardRefused(
            "not_own_id",
            "refused: that sandbox id is not one of this user's Crew sandboxes",
        )

    # -- the rows written from the List --------------------------------------------

    def list_rows(self) -> tuple[set[str], set[str]]:
        """(the ids whose row the lane wrote from the List, the ids a user removed by hand)."""
        data = self._read_json(self._dir / LIST_ROWS_NAME, {})
        data = data if isinstance(data, dict) else {}

        def ids(key: str) -> set[str]:
            got = data.get(key)
            return {str(x) for x in got if x} if isinstance(got, list) else set()

        return ids("registered"), ids("removed")

    def adopted_ids(self) -> set[str]:
        """The ids the user's Set up Crew adopted (Crew's own gateway found)."""
        data = self._read_json(self._dir / LIST_ROWS_NAME, {})
        got = data.get("adopted") if isinstance(data, dict) else None
        return {str(x) for x in got if x} if isinstance(got, list) else set()

    def turned_off_ids(self) -> set[str]:
        """The crew rows the lane turned off for a stopped environment (not the user's own)."""
        data = self._read_json(self._dir / STOPPED_OFF_NAME, {})
        got = data.get("rows") if isinstance(data, dict) else None
        return {str(x) for x in got if x} if isinstance(got, list) else set()

    def note_turned_off(self, *, add: Iterable[str] = (), drop: Iterable[str] = ()) -> None:
        with self._locked():
            rows = (self.turned_off_ids() | set(add)) - set(drop)
            self._write_json(self._dir / STOPPED_OFF_NAME, {"rows": sorted(rows)})

    def row_names(self) -> dict[str, str]:
        """By environment id, the crew name this home last took from the List's displayName
        for the box's row. A List renames the row only when that name changes, so a name the
        user gave on this dashboard alone stays until the shared name changes."""
        data = self._read_json(self._dir / LIST_ROWS_NAME, {})
        got = data.get("names") if isinstance(data, dict) else None
        if not isinstance(got, dict):
            return {}
        return {str(k): str(v) for k, v in got.items() if k and isinstance(v, str)}

    def note_list_rows(
        self,
        *,
        registered: Iterable[str] = (),
        removed: Iterable[str] = (),
        adopted: Iterable[str] = (),
        names: Optional[dict[str, str]] = None,
    ) -> None:
        with self._locked():
            reg, rem = self.list_rows()
            ado = self.adopted_ids()
            nam = self.row_names()
            rem |= set(removed)
            reg = (reg | set(registered)) - rem
            ado = (ado | set(adopted)) - rem  # a removed row wins over adopted too
            nam.update(names or {})
            nam = {k: v for k, v in nam.items() if k not in rem}
            data: dict = {"registered": sorted(reg), "removed": sorted(rem)}
            if ado:
                data["adopted"] = sorted(ado)
            if nam:
                data["names"] = dict(sorted(nam.items()))
            self._write_json(self._dir / LIST_ROWS_NAME, data)

    def record_created(self, summary: dict, display_name: str) -> dict:
        """Add the id a successful CreateSandbox returned. Called before anything else."""
        sid = summary.get("execEnvironmentId")
        if not sid:
            self.note_unconfirmed(display_name)
            raise GuardRefused(
                "create_no_id",
                "CreateSandbox answered 200 without an id: a sandbox named "
                f"{display_name!r} may exist, but there is no id to delete it by (orphan: delete "
                "it in the Kiro web app). The attempt is recorded, and new launches wait "
                f"{_minutes(HOLD_AFTER_UNCONFIRMED_S)} minutes.",
            )
        row = {
            "execEnvironmentId": sid,
            "displayName": summary.get("displayName") or display_name,
            "createdAt": summary.get("createdAt", self._now()),
            "source": SOURCE,
        }
        with self._locked():
            rows = self.ledger()
            if all(r.get("execEnvironmentId") != sid for r in rows):
                rows.append(row)
                self._write_json(self.ledger_path, {"sandboxes": rows})
        return row

    def record_deleted(self, exec_environment_id: str) -> None:
        """Mark a ledger row deleted (kept, so a repeated delete still reads 404 = gone)."""
        with self._locked():
            rows = self.ledger()
            for r in rows:
                if r.get("execEnvironmentId") == exec_environment_id:
                    r["deletedAt"] = self._now()
            self._write_json(self.ledger_path, {"sandboxes": rows})

    # -- before a create -----------------------------------------------------------

    def before_create(self) -> None:
        """Before a CreateSandbox: the stop and the hold after an unconfirmed create. No own
        live cap: the service's cap governs."""
        self.check_not_stopped()
        self._check_unconfirmed()

    def note_unconfirmed(self, display_name: str) -> None:
        """Record a CreateSandbox whose answer was lost, before its error is raised.

        A failed write stops the client for the process instead: without the record a
        second launch could make a second environment Crew can't see."""
        try:
            with self._locked():
                record = {"at": self._now(), "displayName": display_name}
                self._write_json(self.unconfirmed_path, record)
        except OSError as e:
            log.warning("cannot record the unconfirmed create: %s", type(e).__name__)
            self.stop(f"could not record an unconfirmed CreateSandbox ({type(e).__name__})")

    def _unconfirmed(self) -> tuple[float, str] | None:
        """(when, display name) of the last unconfirmed create, or None when there is none.
        A record that can't be read or parsed, or whose time is not a number at or before
        now, holds from the file's mtime: a damaged file can't lift the hold early."""
        path = self.unconfirmed_path
        try:
            mtime = os.stat(path).st_mtime
        except FileNotFoundError:
            return None
        except OSError as e:
            raise GuardRefused("state_unreadable", f"cannot read {UNCONFIRMED_NAME}: {e}") from None
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            data = None
        at = data.get("at") if isinstance(data, dict) else None
        name = data.get("displayName") if isinstance(data, dict) else None
        if (
            not isinstance(at, (int, float))
            or isinstance(at, bool)
            or not math.isfinite(at)
            or at > self._now()
        ):
            at = mtime
        return float(at), name if isinstance(name, str) else ""

    def _check_unconfirmed(self) -> None:
        last = self._unconfirmed()
        if last is None:
            return
        at, name = last
        left = HOLD_AFTER_UNCONFIRMED_S - (self._now() - at)
        if left > 0:
            raise GuardRefused("create_unconfirmed", unconfirmed_text(name, left))

    # -- stop rules ----------------------------------------------------------------

    @property
    def stopped(self) -> str:
        return self._stopped

    @property
    def stopped_by_service(self) -> bool:
        """The stop came from a 5XX or 429 answer (see :class:`ClientStopped`)."""
        return bool(self._stopped) and self._stopped_by_service

    def stop(self, reason: str, *, by_service: bool = False) -> None:
        if not self._stopped:
            self._stopped = reason
            self._stopped_by_service = by_service
        self.clear_accepted()

    def check_not_stopped(self) -> None:
        if self._stopped:
            raise ClientStopped(
                "stopped",
                f"client stopped earlier in this run: {self._stopped}",
                by_service=self._stopped_by_service,
            )

    def note_status(self, op: str, status: int, kind: str = "") -> None:
        """Stop on 401, 403, 429 and any 5XX answer. 404 / 409 / 400 do not stop.

        The account's cap on CreateSandbox (:func:`is_quota_refusal`, by its kind, not its
        status alone) does not stop: only a delete frees a place, and a stopped client could
        not delete. Any other 429, a throttle included, still stops. Nor does the policy 403
        on a per-box action (:func:`is_policy_refusal`): it refuses that action. Any other 403
        still stops."""
        if is_quota_refusal(op, status, kind) or is_policy_refusal(op, status, kind):
            return
        if status in (401, 403, 429) or status >= 500:
            self.stop(f"{op} answered HTTP {status}", by_service=status == 429 or status >= 500)

    # -- a box refused by the organization's policy --------------------------------

    def note_policy(self, exec_environment_id: str) -> None:
        """This box's StartSshSession got the policy 403: hold its ssh asks for a while."""
        with self._policy_lock:
            self._policy[exec_environment_id] = self._now()

    def clear_policy(self, exec_environment_id: Optional[str] = None) -> None:
        """A call for the box that succeeded (a served session, a Resume or a Get) or, with no
        id, a new sign-in: the line goes and the hold ends."""
        with self._policy_lock:
            if exec_environment_id is None:
                self._policy.clear()
            else:
                self._policy.pop(exec_environment_id, None)

    def end_policy_hold(self, exec_environment_id: str) -> None:
        """A List showed the box at least :data:`POLICY_LIST_ENDS_HOLD_S` after its refusal:
        its next ssh ask makes one call. The line stays until a call for the box succeeds."""
        with self._policy_lock:
            at = self._policy.get(exec_environment_id)
            now = self._now()
            if at is not None and now - at >= POLICY_LIST_ENDS_HOLD_S:
                self._policy[exec_environment_id] = min(at, now - POLICY_HOLD_S)

    def policy_line(self, exec_environment_id: str) -> str:
        """The box's plain line while its latest outcome is the policy refusal: its last
        StartSshSession got it, and no call for the box has succeeded since."""
        with self._policy_lock:
            return POLICY_TEXT if exec_environment_id in self._policy else ""

    def check_policy_hold(self, exec_environment_id: str) -> None:
        """Refuse this box's ssh ask, with no call, inside :data:`POLICY_HOLD_S` of its policy
        403. After that, one call asks the service again."""
        with self._policy_lock:
            at = self._policy.get(exec_environment_id)
        if at is not None and self._now() - at < POLICY_HOLD_S:
            raise GuardRefused(POLICY_KIND_WORD, POLICY_TEXT)
