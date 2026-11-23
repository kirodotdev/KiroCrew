"""Hub placement: ``session_create(work_item=)`` with no ``crew`` picks one (wave 5).

RFC §2b option (C): the hub ranks crews from fresh headroom snapshots minus its
reservations (open remote bindings plus creates in flight here, one estimate each),
and the peer gate (wave 3) alone admits. ``crew_busy`` tries the next crew, and so
does a 409 ``duplicate_item`` (that crew already holds a worker for the item, so it
made nothing; the reply names it under ``skipped``); any other refusal stops, since
that create may have reached the peer. The rules are spelled
out in ``docs/system-specs/modules/session-control.md`` ("Wave 5"). Reached only
while ``work_ledger.remote_dispatch`` is on.

An optional ``requires: {"tags": [...], "logins": [...]}`` filters crews before
they are ranked: a crew qualifies only when its headroom lists every tag and
reports ``valid`` for every login. When none qualifies the create is refused
``crew_requirements_unmet`` naming what each crew lacked; there is no blind pick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from kiro_crew.dashboard.handlers.remote_dispatch_routes import CrewCallError
from kiro_crew.dashboard.remote_dispatch_headroom import (
    CPU_LOAD_THRESHOLD,
    NO_LOGINS_QUERY,
    WITH_LOGINS_QUERY,
    remote_dispatch_settings,
)
from kiro_crew.remote_dispatch import LOGIN_EXPIRED, LOGIN_UNKNOWN, LOGIN_VALID
from kiro_crew.work_vocab import DISPATCH_TAG_RE, MAX_DISPATCH_TAGS

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The refusal when no connected crew has room for the item.
CREW_NONE_AVAILABLE = "crew_none_available"

#: The refusal when a ``requires`` is malformed.
REQUIRES_INVALID = "requires_invalid"

#: The refusal when connected crews answered but none meets ``requires``.
CREW_REQUIREMENTS_UNMET = "crew_requirements_unmet"

#: A peer's 409 when it already holds a worker for the item: the store's
#: ``CODE_DUPLICATE_ITEM``, spelled here because this module may not import the store
#: (``test_work_ledger.py::test_only_the_phase_2_seams_import_the_module``; the
#: ``remote_ingest`` family is on that allowlist, placement is not). Pinned equal by
#: ``test_the_code_matches_the_store``.
CODE_DUPLICATE_ITEM = "duplicate_item"

#: :func:`_candidate`'s answer for a crew that meets ``requires`` but has no room.
_NO_ROOM = "no_room"

#: ``TunnelState.CONNECTED.value``.
_CONNECTED = "connected"

#: Per gateway state: creates in flight per crew, and the lock the choice takes.
_INFLIGHT: "weakref.WeakKeyDictionary[Any, dict[str, int]]" = weakref.WeakKeyDictionary()
_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class Candidate:
    instance_id: str
    name: str
    mem_free_gb: float
    disk_free_gb: float


@dataclass(frozen=True)
class Requires:
    """What a crew must advertise to take an item: every tag, a ``valid`` login per tool."""

    tags: tuple[str, ...] = ()
    logins: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tags or self.logins)


@dataclass(frozen=True)
class Unmet:
    """A crew that answered its headroom but lacks part of ``requires``."""

    name: str
    missing: tuple[str, ...]


def parse_requires(raw: object) -> Requires:
    """*raw* (the create's ``requires``) as :class:`Requires`; raises ``requires_invalid``.

    Absent or ``{}`` requires nothing. Unknown keys, a non-list, a name that is
    not a tag-shaped word, a login tool this hub does not know, or more than
    :data:`MAX_DISPATCH_TAGS` names are refused rather than dropped, so a typo
    never widens placement.
    """
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    if raw is None:
        return Requires()
    if not isinstance(raw, dict):
        raise routes.CrewCallError(400, REQUIRES_INVALID, "requires must be an object")
    extra = sorted(str(k) for k in raw if k not in ("tags", "logins"))
    if extra:
        raise routes.CrewCallError(
            400, REQUIRES_INVALID, f"requires takes only tags and logins, not {', '.join(extra)}"
        )
    from kiro_crew.remote_session import git_gateway

    # The tools a crew can report: the ones its git proxy readiness checks.
    tools = sorted({t for h in git_gateway.HOSTS if (t := git_gateway.readiness_tool(h))})
    out: dict[str, tuple[str, ...]] = {}
    for key in ("tags", "logins"):
        names = raw.get(key, [])
        if not isinstance(names, list) or len(names) > MAX_DISPATCH_TAGS:
            raise routes.CrewCallError(
                400,
                REQUIRES_INVALID,
                f"requires.{key} must be a list of at most {MAX_DISPATCH_TAGS}",
            )
        for name in names:
            if not isinstance(name, str) or not DISPATCH_TAG_RE.fullmatch(name):
                raise routes.CrewCallError(
                    400,
                    REQUIRES_INVALID,
                    f"requires.{key} names must be 1-32 characters of a-z, 0-9, '_' or '-' "
                    "with at least one letter",
                )
            if key == "logins" and name not in tools:
                raise routes.CrewCallError(
                    400,
                    REQUIRES_INVALID,
                    f"requires.logins names a tool crews do not report; use {', '.join(tools)}",
                )
        out[key] = tuple(dict.fromkeys(names))
    return Requires(tags=out["tags"], logins=out["logins"])


def missing(requires: Requires, payload: dict[str, Any]) -> tuple[str, ...]:
    """What of *requires* the headroom *payload* lacks, as ``tag X`` / ``login X`` words.

    The payload is a peer's and untrusted: only a list of strings counts as tags
    and only the exact word ``valid`` counts as a login. A peer older than
    ``tags``/``logins`` lacks every name it is asked for.
    """
    tags = payload.get("tags")
    have_tags = {t for t in tags if isinstance(t, str)} if isinstance(tags, list) else set()
    logins = payload.get("logins")
    logins = logins if isinstance(logins, dict) else {}
    lacks = [f"tag {t}" for t in requires.tags if t not in have_tags]
    for tool in requires.logins:
        raw_word = logins.get(tool)
        word = raw_word if isinstance(raw_word, str) else ""
        state = {LOGIN_EXPIRED: "expired", LOGIN_UNKNOWN: "still checking; retry shortly"}.get(
            word, "not reported"
        )
        if word != LOGIN_VALID:
            lacks.append(f"login {tool} ({state})")
    return tuple(lacks)


def _lacks(unmet: list[Unmet]) -> str:
    return "; ".join(f"{u.name} lacks {', '.join(u.missing)}" for u in unmet)


def unmet_message(unmet: list[Unmet]) -> str:
    """The ``crew_requirements_unmet`` prose: each crew and what it lacked."""
    shown = _lacks(unmet)
    return (
        f"no connected crew meets this item's requires ({shown}); it stays open and "
        "unbound. Sign in or tag a crew, or drop the requirement"
    )


#: Seconds one crew's version and headroom reads may take. Crews are probed at once,
#: so a stalled crew costs this much, well inside the tool's 30 s call budget.
PROBE_TIMEOUT_S = 5.0

#: Best effort: once this many seconds have passed since placement began, no new
#: crew is tried. A create already started (its config load, board lock and peer
#: POST included) runs to the end, so one slow create can outlast the tool's 30 s
#: wait and bind after the caller gave up; a chain of ``crew_busy`` replies cannot.
PLACEMENT_BUDGET_S = 15.0

#: The clock the budget reads; a test replaces it rather than ``time.monotonic``.
_clock = time.monotonic


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer past float range from a hostile peer
        return None
    return number if math.isfinite(number) and number >= 0 else None


class DuplicateItemError(CrewCallError):
    """The peer answered 409 ``duplicate_item`` to a bound create: it already holds
    a worker for the item (a create whose reply was lost) and made nothing for this
    one. Raised by ``remote_ingest.create_bound``; placement tries the next crew."""

    def __init__(self) -> None:
        super().__init__(409, CODE_DUPLICATE_ITEM, "that crew already holds a worker for this item")


def _connected(mgr: Any, instance_id: str) -> bool:
    try:
        status = mgr.status(instance_id)
    except Exception:  # noqa: BLE001 - a status read must not fail a placement
        return False
    return getattr(getattr(status, "state", None), "value", None) == _CONNECTED


def _headroom_params(requires: Requires) -> dict[str, str]:
    """The headroom read's query: ask for logins only when *requires* names one,
    so an item that needs no login never waits on the peer's login checks."""
    return dict(WITH_LOGINS_QUERY if requires.logins else NO_LOGINS_QUERY)


async def _candidate(
    state: "DashboardState",
    mgr: Any,
    row: Any,
    mem_gb: float,
    disk_gb: float,
    requires: Requires = Requires(),
) -> Candidate | Unmet | str | None:
    """*row* as a :class:`Candidate`; :class:`Unmet` when its headroom lacks part of
    *requires*; :data:`_NO_ROOM` when it meets *requires* but is short; ``None``
    when it is not connected or its headroom cannot be read."""
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    instance_id = str(getattr(row, "id", "") or "")
    if not instance_id or not _connected(mgr, instance_id):
        return None

    async def _probe() -> tuple[int, Any]:
        await routes.check_version(mgr, instance_id)
        return await routes.peer_call(
            mgr, instance_id, "GET", "api/chat/headroom", params=_headroom_params(requires)
        )

    try:
        status, payload = await asyncio.wait_for(_probe(), PROBE_TIMEOUT_S)
    except routes.CrewCallError as exc:
        logger.info("placement skips crew %s (%s)", instance_id, exc.code)
        return None
    except asyncio.TimeoutError:
        logger.info("placement skips crew %s (headroom read timed out)", instance_id)
        return None
    except Exception:  # noqa: BLE001 - one crew's fault must not fail the placement
        logger.warning("placement skips crew %s (probe failed)", instance_id, exc_info=True)
        return None
    if not 200 <= status < 300 or not isinstance(payload, dict):
        return None
    name = str(getattr(row, "name", "") or instance_id)
    lacks = missing(requires, payload)
    if lacks:
        # Filtered before it is ranked: a crew without the capability is never
        # picked, however much room it has.
        return Unmet(name, lacks)
    mem, disk = (
        _number(payload.get("mem_free_gb")),
        _number(payload.get("disk_free_gb")),
    )
    load = _number(payload.get("load_per_cpu"))
    if mem is None or disk is None or mem < mem_gb or disk < disk_gb:
        return _NO_ROOM
    if load is not None and load >= CPU_LOAD_THRESHOLD:
        return _NO_ROOM
    return Candidate(instance_id, name, mem, disk)


async def check_crew_requires(mgr: Any, instance_id: str, name: str, raw: object) -> None:
    """For an explicit ``crew=`` create: refuse when that crew lacks *raw*'s ``requires``.

    No ``requires`` reads nothing. A crew whose headroom cannot be read is refused
    too: an unchecked requirement is never treated as met.
    """
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    requires = parse_requires(raw)
    if not requires:
        return
    try:
        status, payload = await asyncio.wait_for(
            routes.peer_call(
                mgr, instance_id, "GET", "api/chat/headroom", params=_headroom_params(requires)
            ),
            PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        status, payload = 0, None
    if not 200 <= status < 300 or not isinstance(payload, dict):
        raise routes.CrewCallError(
            503,
            CREW_REQUIREMENTS_UNMET,
            f"{name} did not answer its headroom, so this item's requires cannot be checked",
        )
    lacks = missing(requires, payload)
    if lacks:
        raise routes.CrewCallError(
            503, CREW_REQUIREMENTS_UNMET, unmet_message([Unmet(name, lacks)])
        )


def _score(c: Candidate, reserved: int, mem_gb: float, disk_gb: float) -> float:
    """The scarcer of memory and disk after reservations, counted in estimates."""
    return min(
        (c.mem_free_gb - reserved * mem_gb) / mem_gb,
        (c.disk_free_gb - reserved * disk_gb) / disk_gb,
    )


async def _reserve_best(
    state: "DashboardState", pool: list[Candidate], mem_gb: float, disk_gb: float
) -> Candidate:
    """Pick the best of *pool* and count one create in flight on it, atomically."""
    from kiro_crew.dashboard.handlers.remote_ingest import open_remote_bindings

    async with _LOCKS.setdefault(state, asyncio.Lock()):
        bound = await asyncio.to_thread(open_remote_bindings)
        inflight = _INFLIGHT.setdefault(state, {})
        # ``max`` keeps the first of equal scores, so sorting by id breaks ties.
        best = max(
            sorted(pool, key=lambda c: c.instance_id),
            key=lambda c: _score(
                c,
                bound.get(c.instance_id, 0) + inflight.get(c.instance_id, 0),
                mem_gb,
                disk_gb,
            ),
        )
        inflight[best.instance_id] = inflight.get(best.instance_id, 0) + 1
        return best


def _release(state: "DashboardState", instance_id: str) -> None:
    inflight = _INFLIGHT.setdefault(state, {})
    inflight[instance_id] -= 1
    if not inflight[instance_id]:
        del inflight[instance_id]


@contextlib.asynccontextmanager
async def holding(state: "DashboardState", instance_id: str) -> AsyncIterator[None]:
    """Count one work-item create to *instance_id* in flight while it runs.

    For an explicit ``crew=`` create, so a placement that runs meanwhile counts
    it before its binding is recorded. Placement's own creates are counted by
    :func:`_reserve_best` and never pass through here.
    """
    async with _LOCKS.setdefault(state, asyncio.Lock()):
        inflight = _INFLIGHT.setdefault(state, {})
        inflight[instance_id] = inflight.get(instance_id, 0) + 1
    try:
        yield
    finally:
        _release(state, instance_id)


@contextlib.asynccontextmanager
async def creates_held(state: "DashboardState") -> AsyncIterator[bool]:
    """Keep any work-item create from starting while the body runs; yields whether
    none is in flight now.

    Every create counts itself in flight under this lock (:func:`_reserve_best`,
    :func:`holding`), so while it is held the answer cannot go stale.
    """
    async with _LOCKS.setdefault(state, asyncio.Lock()):
        yield not any(_INFLIGHT.get(state, {}).values())


async def place(
    state: "DashboardState", mgr: Any, caller: str, body: dict[str, Any], *, board: str
) -> tuple[dict[str, Any], str]:
    """Create the bound worker on the best crew: ``(create result, crew name)``, or raise."""
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    if not isinstance(body.get("work_item"), str) or not body["work_item"].strip():
        # Placement is for a work item only; a plain crew session names its crew.
        raise routes.CrewCallError(400, "work_item_invalid", "work_item must be an item id")
    deadline = _clock() + PLACEMENT_BUDGET_S
    settings = await asyncio.to_thread(remote_dispatch_settings)
    if not settings.enabled:
        # Turned off between the route's check and this read: place nothing.
        from kiro_crew.remote_dispatch import (
            REMOTE_DISPATCH_DISABLED,
            REMOTE_DISPATCH_DISABLED_MESSAGE,
        )

        raise routes.CrewCallError(403, REMOTE_DISPATCH_DISABLED, REMOTE_DISPATCH_DISABLED_MESSAGE)
    requires = parse_requires(body.get("requires"))
    mem_gb, disk_gb = settings.memory_gb, settings.disk_gb
    from kiro_crew.dashboard.handlers import remote_ingest

    # Refuse a bad item before any crew is probed, with the bound create's codes.
    item = await remote_ingest.open_unbound_item(board, body["work_item"])
    try:
        rows = await asyncio.to_thread(routes._instances, state)
    except Exception:
        logger.debug("crew registry unreadable", exc_info=True)
        rows = []
    # Wave 8: a crew this item was retired from is never placed on again (RFC §5b).
    skipped = remote_ingest.retired_crews(item)
    rows = [r for r in rows if str(getattr(r, "id", "") or "") not in skipped]
    found = await asyncio.gather(
        *(_candidate(state, mgr, r, mem_gb, disk_gb, requires) for r in rows)
    )
    pool = [c for c in found if isinstance(c, Candidate)]
    unmet = [c for c in found if isinstance(c, Unmet)]
    if unmet and not pool and _NO_ROOM not in found:
        # No crew that answered has the capability: name what each lacks, and
        # never fall back to a crew without it.
        raise routes.CrewCallError(503, CREW_REQUIREMENTS_UNMET, unmet_message(unmet))
    busy: list[str] = []
    duplicate: list[Candidate] = []
    stopped = False
    while pool:
        pick = await _reserve_best(state, pool, mem_gb, disk_gb)
        try:
            # Checked after the reservation, which can wait on the lock: past the
            # budget, no new crew is tried (best effort, see PLACEMENT_BUDGET_S).
            if _clock() >= deadline:
                stopped = True
                break
            result = await routes._create(state, mgr, caller, pick.instance_id, body, board=board)
        except routes.CrewBusyError:
            busy.append(pick.name)
            pool = [c for c in pool if c.instance_id != pick.instance_id]
            continue
        except DuplicateItemError:
            # The crew already holds a worker for this item (a create whose reply
            # was lost). It refused before making anything, so the next crew is
            # tried; the next pull that reaches that crew reclaims the orphan
            # (``remote_reclaim``).
            duplicate.append(pick)
            pool = [c for c in pool if c.instance_id != pick.instance_id]
            continue
        finally:
            _release(state, pick.instance_id)
        if item.retired_bindings:
            # The new worker starts from the retired one's last pushed branch and
            # pushes a NEW one; the conductor names both in the seed.
            result["previous_branch"] = item.retired_bindings[-1].get("branch")
        if duplicate:
            result["skipped"] = [
                {"crew": c.instance_id, "crew_name": c.name, "code": CODE_DUPLICATE_ITEM}
                for c in duplicate
            ]
        return result, pick.name
    tried = f" ({', '.join(busy)} answered crew_busy)" if busy else ""
    if duplicate:
        names = ", ".join(c.name for c in duplicate)
        tried += f" ({names} answered {CODE_DUPLICATE_ITEM}: already holds a worker for it)"
    if unmet:
        tried += f"; {_lacks(unmet)}"
    if stopped:
        tried += f"; placement stopped after {PLACEMENT_BUDGET_S:g} s"
    raise routes.CrewCallError(
        503,
        CREW_NONE_AVAILABLE,
        f"no connected crew has room for this item{tried}; it stays open and unbound. "
        "Retry from a turn the owner sends: a patrol wake cannot use crew placement",
    )
