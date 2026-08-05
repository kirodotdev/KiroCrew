"""On-call rotation from a committed schedule file — no rotation service required.

A team that has PagerDuty already has a rotation API. This adapter is for everyone
else: the owner's ask was "assuming we do not have oncall service we just can keep
oncall schedule in that repository and for auth we can use github logins". So the
schedule is a YAML file that lives in the SAME git repo the knowledge ledger syncs
through (``ledger_sync``), and identity is the operator's GitHub login.

Why a file in git rather than a service:

- **It is already synced.** The ledger repo is the team's shared memory; a rotation
  is the same kind of small, slow-changing, human-edited fact. Reusing that transport
  means no second integration, no second credential, and the schedule arrives on the
  same pull that brings teammates' lessons.
- **It is reviewable.** A shift swap is a diff with an author and a timestamp. That is
  a better audit trail than most rotation UIs give you, and it is the same reason the
  ledger is JSONL in git rather than rows in a private DB.
- **It fails CLOSED, which is the opposite of what a rotation API should do.** Every
  indeterminate case — missing file, malformed YAML, unresolvable login, clock outside
  every window — resolves to ``on_shift=False, unknown=True`` under ``strict_gating``
  (default on). For an API, "cannot tell" means the network is down and arming is right.
  For a file every instance reads, "cannot tell" means the SCHEDULE is wrong, and arming
  makes the whole team pick up the same alarm. ``unknown`` survives so the UI can explain
  WHY; only ``on_shift`` gates work.

**This adapter never decides authority, only tier arming.** Being on shift arms the
``on_shift`` cron tier; it does not raise the autonomy mode. ``effective = min(app_mode,
rule_mode)`` still governs every action, so a schedule file — which any teammate can
edit and push — cannot escalate what the agent may DO. That separation is deliberate:
the schedule is shared, mutable, and only as trustworthy as the repo's write access, so
it is wired to the cheap decision (when to look) and not the expensive one (what to do).

Schedule format (``rotation.yaml`` at the repo root)::

    leader: octocat                   # optional; runs nightly ledger hygiene ALONE
    timezone: America/Los_Angeles     # optional; UTC when absent
    shifts:
      - from: 2026-08-01
        to: 2026-08-08
        who: octocat                  # a GitHub login
      - from: 2026-08-08T09:00
        to: 2026-08-15T09:00
        who: [octocat, hubot]         # co-primary is allowed

See ``docs/system-specs/modules/ops-mission-control.md`` § Rotation.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, policy_store
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv

logger = logging.getLogger(__name__)

PROVIDER_ID = "schedule-file"

#: Filename inside the synced ledger repo. Fixed rather than configurable: the whole
#: point is that every teammate's install reads the SAME file, and a per-install
#: filename would let two members disagree about where the rotation lives.
SCHEDULE_FILENAME = "rotation.yaml"

#: Keystone key holding this operator's GitHub login. Optional — when absent we resolve
#: it from the local ``gh`` CLI, which is where the owner wanted identity to come from.
#:
#: It lives on the FENCED floor, not in ``data/config.json``, because it is an input to an
#: authorization decision: see ``policy_store.OPERATOR_ONLY_KEYS`` for the forgery it allowed.
_LOGIN_KEY = policy_store.SCHEDULE_LOGIN_KEY

#: Cap on the schedule file we will parse. A rotation is tens of lines; anything
#: megabyte-scale is a mistake or a hostile push, and YAML parsing is not free.
MAX_SCHEDULE_BYTES = 256 * 1024

#: Cap on shift entries considered. A year of daily shifts is 365; 5000 is far above
#: any real rotation and bounds the scan a pushed file can cost us.
MAX_SHIFTS = 5000

_GH_TIMEOUT_SECS = 10.0

#: Cached GitHub login. Resolving shells out to ``gh``, and ``on_shift`` runs on every
#: rotation-check cron tick — an unbounded re-shell per tick is a needless process
#: spawn. The login does not change within a gateway lifetime in any realistic flow.
_login_cache: str | None = None


def schedule_path() -> Path:
    """Where the schedule lives: beside the ledger, inside the synced repo."""
    return ledger.ledger_path().parent / SCHEDULE_FILENAME


def _resolve_login_sync() -> str:
    """This operator's GitHub login, from config or the local ``gh`` CLI.

    Routed through ``sandboxed_spawn_argv`` + ``resource_limit_preexec`` like every
    other agent-reachable spawn in this app — the ``test/test_spawn_audit.py`` gate
    requires that chokepoint, and a rotation check is reachable from a cron an agent
    can trigger. Returns "" on any failure; the caller turns that into ``unknown``.
    """
    configured = str(policy_store.get(_LOGIN_KEY) or "").strip()
    if configured:
        return configured

    global _login_cache
    if _login_cache is not None:
        return _login_cache

    argv, env, cleanup = sandboxed_spawn_argv(["gh", "api", "user", "--jq", ".login"])
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, sandbox-routed
            argv,
            capture_output=True,
            timeout=_GH_TIMEOUT_SECS,
            env=env,
            preexec_fn=resource_limit_preexec(),
        )
        login = proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        logger.debug("ops-mission-control: could not resolve a GitHub login via gh", exc_info=True)
        login = ""
    finally:
        # A temp-profile PATH, not a callable — the same shape ledger_sync documents.
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)

    # Cache the miss too: a machine with no gh must not re-shell every tick.
    _login_cache = login
    return login


def reset_login_cache() -> None:
    """Forget the resolved login. For tests and for an operator who just ran ``gh auth``."""
    global _login_cache
    _login_cache = None


def _tzinfo(name: str) -> timezone | Any:
    """Resolve an IANA name, falling back to UTC.

    A bad or unavailable timezone must not fail the whole rotation check — UTC gives a
    defined answer, and the shift windows are usually day-granular anyway. Windows ships
    no system tz database, which is why ``tzdata`` is a declared Windows dependency;
    this still degrades rather than raising if the lookup fails.
    """
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown zone, missing tzdata, bad type
        logger.debug("ops-mission-control: unknown rotation timezone %r; using UTC", name)
        return timezone.utc


def _parse_moment(raw: Any, tz: Any, *, end: bool) -> datetime | None:
    """Parse a ``from``/``to`` value into an aware datetime.

    Accepts ``YYYY-MM-DD`` and ``YYYY-MM-DDTHH:MM``. A DATE-only ``to`` is treated as
    the END of that day, not midnight at its start: a human writing ``to: 2026-08-08``
    means "through the 8th", and reading it as 00:00 would silently drop the last day of
    every shift written that way. That off-by-one-day is the single most likely way this
    file gets misread, so it is handled here rather than left to the operator.
    """
    if isinstance(raw, datetime):
        moment = raw
    elif isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            logger.debug("ops-mission-control: unparseable rotation moment %r", raw)
            return None
        if end and len(text) == 10:  # bare YYYY-MM-DD
            moment = moment + timedelta(days=1)
    else:
        # PyYAML parses a bare date into datetime.date; accept it via isoformat.
        iso = getattr(raw, "isoformat", None)
        if not callable(iso):
            return None
        return _parse_moment(iso(), tz, end=end)

    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def _whos(entry: dict[str, Any]) -> list[str]:
    """The logins on a shift. Accepts a scalar or a list (co-primary is legitimate)."""
    raw = entry.get("who", entry.get("login", entry.get("logins")))
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def read_schedule() -> tuple[list[dict[str, Any]], str, str]:
    """Parse the schedule file. Returns ``(shifts, timezone_name, error)``.

    Never raises: a malformed schedule that a teammate pushed must degrade to
    ``unknown`` (tier armed), not crash the rotation-check cron for everyone.
    """
    path = schedule_path()
    try:
        if not path.exists():
            return [], "", f"no {SCHEDULE_FILENAME} in the synced ledger repo"
        if path.stat().st_size > MAX_SCHEDULE_BYTES:
            return [], "", f"{SCHEDULE_FILENAME} exceeds {MAX_SCHEDULE_BYTES} bytes"
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], "", f"could not read {SCHEDULE_FILENAME}: {exc}"

    try:
        import yaml  # type: ignore[import-untyped]

        # safe_load, never load: this file arrives over `git pull` from a shared repo,
        # so it is untrusted input by construction and must not be able to construct
        # arbitrary Python objects.
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 — any YAML fault degrades to unknown
        return [], "", f"{SCHEDULE_FILENAME} is not valid YAML: {str(exc)[:200]}"

    if not isinstance(data, dict):
        return [], "", f"{SCHEDULE_FILENAME} must be a YAML mapping"

    tz_name = str(data.get("timezone", "") or "")
    shifts = data.get("shifts")
    if not isinstance(shifts, list):
        return [], tz_name, f"{SCHEDULE_FILENAME} has no 'shifts' list"

    entries = [s for s in shifts[:MAX_SHIFTS] if isinstance(s, dict)]
    if len(shifts) > MAX_SHIFTS:
        # Say so rather than silently scanning a prefix — a truncated rotation would
        # look like "nobody is on shift" for everyone past the cut.
        logger.warning(
            "ops-mission-control: %s lists %d shifts; only the first %d are considered",
            SCHEDULE_FILENAME,
            len(shifts),
            MAX_SHIFTS,
        )
    return entries, tz_name, ""


#: Config key for STRICT gating. Default TRUE for a committed schedule, which is the
#: opposite of the fail-open default a rotation *API* needs — and the difference is the
#: whole point of this source.
#:
#: With a remote API, "cannot tell" means the network is down: arming is right, because
#: wrongly disarming one instance loses incident response and the API is not something
#: the team controls.
#:
#: With a schedule file every instance reads, "cannot tell" means the SCHEDULE is wrong
#: (expired, unparseable, or this operator's login is missing). Arming then makes *every*
#: instance in the mesh pick up the same work — the exact double-claim the shared schedule
#: exists to prevent. So the safe direction inverts: when the file cannot say you are on
#: call, you are not.
#:
#: Set false to restore fail-open for a single-instance install that would rather over-fire
#: than miss an alarm.
#: On the FENCED floor with the login, for the same reason: this is the other way to defeat
#: the off-shift refusal. Setting it false makes an indeterminate schedule report
#: ``on_shift=True``, and an agent that can write `config.json` can also make the schedule
#: indeterminate (the file lives in a repo it can edit) — so the pair "break the file, then
#: disable strict gating" reproduces the forgery's effect without needing any login at all.
_STRICT_KEY = policy_store.SCHEDULE_STRICT_KEY


def strict_gating() -> bool:
    """Whether an indeterminate schedule DISARMS this instance (default: yes)."""
    raw = policy_store.get(_STRICT_KEY)
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _indeterminate(reason: str) -> ShiftStatus:
    """The schedule cannot say whether this operator is on call.

    Under strict gating (the default) this DISARMS: ``on_shift=False`` with
    ``unknown=True`` so the UI can still distinguish "the file says someone else" from
    "the file cannot tell". ``rotation.tier_states`` reads ``on_shift`` for arming, so the
    tier goes down while the reason stays visible.
    """
    logger.debug("ops-mission-control: rotation indeterminate (%s)", reason)
    if strict_gating():
        return ShiftStatus(on_shift=False, unknown=True)
    return ShiftStatus(on_shift=True, unknown=True)


def resolve_now(now: datetime | None = None) -> ShiftStatus:
    """Who is on shift right now, per the committed schedule.

    Synchronous and injectable (``now``) so the window arithmetic is testable without
    freezing the clock globally.
    """
    shifts, tz_name, error = read_schedule()
    if error:
        return _indeterminate(error)

    tz = _tzinfo(tz_name)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    me = _resolve_login_sync()
    if not me:
        return _indeterminate("no GitHub login resolved for this instance")

    for entry in shifts:
        start = _parse_moment(entry.get("from"), tz, end=False)
        end = _parse_moment(entry.get("to"), tz, end=True)
        if start is None or end is None or end <= start:
            continue
        if not (start <= moment < end):
            continue
        logins = _whos(entry)
        # Case-insensitive: GitHub logins are case-insensitive, and a schedule written
        # "Octocat" against a `gh` login of "octocat" must not read as off-shift.
        if any(login.lower() == me.lower() for login in logins):
            return ShiftStatus(on_shift=True, who=me, until=end.isoformat())
        # A window that covers now and names someone ELSE is a definitive answer: this
        # operator is off shift. Returning here rather than continuing is what makes
        # the file able to say "not you" at all.
        if logins:
            return ShiftStatus(on_shift=False, who=", ".join(logins), until=end.isoformat())

    # No window covers now: an expired rotation or a not-yet-filled week. Under strict
    # gating nobody picks up work, which is the correct behavior for a team schedule — an
    # unfilled slot means the team has not assigned the shift, not that everyone owns it.
    return _indeterminate("no shift window covers the current time")


class ScheduleFileRotationSource:
    """``RotationSource`` backed by ``rotation.yaml`` in the synced ledger repo."""

    id = PROVIDER_ID
    display_name = "Schedule file (git)"
    detail = (
        "Reads rotation.yaml from the synced knowledge repo and matches your GitHub "
        "login. No rotation service required."
    )
    #: EMPTY on purpose. The login is not a provider-config field: `PUT
    #: /provider/<id>/config` writes `config.json`, which is exactly the writer this key was
    #: moved off. Advertising it here would re-open the forgery through the generic provider
    #: route even with the read fenced. Its sole writer is the authenticated `PUT /settings`.
    config_fields: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        # Configured means "there is a schedule to read". The login is optional (we
        # fall back to `gh`), so requiring it here would make the common case — a
        # committed file plus an already-authenticated gh — look unconfigured.
        return schedule_path().exists()

    async def on_shift(self) -> ShiftStatus:
        if not self.configured():
            return ShiftStatus(on_shift=True, unknown=True)
        # File read, YAML parse, and a possible `gh` spawn are all synchronous.
        return await asyncio.to_thread(self._on_shift_sync)

    def _on_shift_sync(self) -> ShiftStatus:
        """The synchronous core, as a METHOD.

        ``rotation._definitely_off_shift`` is sync by design (its caller already dispatches it
        through ``to_thread``) and finds each source's sync core by attribute lookup. This
        logic lived only in the module-level ``resolve_now``, so that lookup found nothing on
        this class and the source ABSTAINED — which silently disabled off-shift detection for
        the committed schedule, the exact guard the write gate exists to apply. Declaring the
        method makes the contract explicit instead of leaving it to be guessed at.
        """
        if not self.configured():
            return ShiftStatus(on_shift=True, unknown=True)
        return resolve_now()


def resolve_login() -> str:
    """This instance's GitHub login, or "". Public wrapper over the cached resolver.

    Exists so callers outside this module (``rotation.is_primary``) do not reach for a
    private name; the caching and the ``gh`` fallback are the same.
    """
    return _resolve_login_sync()


def leader() -> str:
    """The team's ``leader:`` login from the committed schedule, or "".

    One optional top-level key:

    ```yaml
    leader: alice
    shifts: [...]
    ```

    A committed team file is the natural home for this, carrying the same field for
    the same reason: nightly consolidation must run ONCE over shared knowledge. Putting it
    in the file everyone reads is what makes that true by construction, instead of relying
    on N instances each having been configured not to.

    Empty when absent, so a schedule that names no leader leaves ``primary_instance`` in
    charge and a solo install is unaffected.
    """
    shifts, _tz, error = read_schedule()
    del shifts  # only the top-level key is needed here
    if error:
        return ""
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(schedule_path().read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — read_schedule already validated; be safe anyway
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("leader", "") or "").strip()


def roster(now: datetime | None = None) -> dict[str, Any]:
    """The whole team and its shift order — not just whoever holds the pager now.

    Exists because "who is on call" alone cannot answer the questions an operator
    actually has: is my instance idle because someone else has it, or because the file is
    broken? Who is up next? Am I even ON this rotation? A single ``who`` string makes all
    three indistinguishable, and an instance that has quietly stopped picking up work
    looks identical to one that is simply off shift.

    Returns members in first-shift order (stable, so the UI does not reshuffle between
    polls), each with their shift count and whether they hold the current window. ``me``
    is this instance's resolved login so the UI can mark "you" without a second call.
    """
    shifts, tz_name, error = read_schedule()
    tz = _tzinfo(tz_name)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    members: dict[str, dict[str, Any]] = {}
    windows: list[dict[str, Any]] = []
    for entry in shifts:
        start = _parse_moment(entry.get("from"), tz, end=False)
        end = _parse_moment(entry.get("to"), tz, end=True)
        logins = _whos(entry)
        if start is None or end is None or end <= start or not logins:
            # Same skip the resolver applies. Counting a malformed row would inflate a
            # member's shift count and imply coverage that does not exist.
            continue
        current = start <= moment < end
        windows.append(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "who": logins,
                "current": current,
            }
        )
        for login in logins:
            slot = members.setdefault(
                login.lower(), {"login": login, "shifts": 0, "on_call_now": False}
            )
            slot["shifts"] += 1
            if current:
                slot["on_call_now"] = True

    windows.sort(key=lambda w: str(w["from"]))
    # First-appearance order, derived from the sorted windows so it is deterministic.
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for window in windows:
        for login in window["who"]:
            key = login.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(members[key])

    me = _resolve_login_sync()
    return {
        "members": ordered,
        "windows": windows,
        "timezone": tz_name or "UTC",
        "me": me,
        # Distinguishes "you are not on the rotation at all" from "you are, but not now" —
        # the former is a setup mistake, the latter is normal.
        "me_on_roster": bool(me) and me.lower() in seen,
        "strict_gating": strict_gating(),
        # Who runs nightly consolidation. Surfaced so the panel can mark the leader and an
        # operator can see at a glance that exactly one instance owns ledger hygiene.
        "leader": leader(),
        "error": error,
    }


def status() -> dict[str, Any]:
    """Settings-panel status: is there a schedule, and what does it currently say?"""
    shifts, tz_name, error = read_schedule()
    shift = resolve_now()
    return {
        "provider": PROVIDER_ID,
        "path": str(schedule_path()),
        "present": schedule_path().exists(),
        "shifts": len(shifts),
        "timezone": tz_name or "UTC",
        "login": _resolve_login_sync(),
        "on_shift": shift.on_shift,
        "who": shift.who,
        "until": shift.until,
        "unknown": shift.unknown,
        "detail": error or ("on shift" if shift.on_shift and not shift.unknown else ""),
    }
