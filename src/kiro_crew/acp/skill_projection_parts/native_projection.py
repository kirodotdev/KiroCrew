"""The prepared :class:`NativeSkillProjection`, and the registry of live projections.

A projection translates transport identities between Crew's agent names and the
published aliases; the registry holds the aliases a prune must keep while their
projection is live.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.acp.skill_projection_parts.side_files import _LEGACY_ALIAS_NAME_RE
from kiro_crew.acp.skill_projection_parts.view_sources import (
    RetiredSkillView,
    _admissible_source_agent,
    is_skill_view_name,
    remembered_view_source,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


# How many earlier aliases one projection keeps translating inbound. A process
# publishes a new alias only when an agent's view changes, so this is far above
# any real run; it bounds a process whose specs are edited without end.
_RECOGNISED_ALIASES_MAX = 1024


@dataclass
class NativeSkillProjection:
    """Translate transport identities while Crew keeps the authored agent name."""

    aliases: dict[str, str]
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    search_agents: set[str] = field(default_factory=set)
    #: The agent a DIRECT CLIENT (one process / one session) was launched as,
    #: recorded by that spawn caller after preparation. The process is already
    #: running as it, so its first ``session/set_mode`` activation must be
    #: tolerated even with no prepared view -- refusing it would strand a valid
    #: startup. ``request()`` tolerates that FIRST activation and then clears this,
    #: so a later mid-session switch back to the launch agent takes the strict
    #: resolver and fails closed if its view has vanished. The SHARED RUNTIME does
    #: NOT set this: it activates the launched agent through
    #: ``_activate_mode_bracketed``, which allows ``self._agent`` at every session
    #: start explicitly, and setting this would make ``request()`` tolerate the
    #: launch agent on a mid-session switch too, reactivating a cached unprojected
    #: spec. Every OTHER modeId is strict. Empty until set, which keeps the strict
    #: answer for a projection no spawn claimed.
    spawn_agent_name: str = ""
    #: The launch identity's DECLARED name, recorded by EVERY spawn caller (both
    #: the direct client and the shared runtime). ``frame`` keeps this agent's
    #: mode in projected ``availableModes`` so the start -- which reads
    #: ``availableModes`` before sending ``set_mode`` -- finds an activatable mode
    #: even when the launch agent has no prepared view. This is kept SEPARATE from
    #: ``spawn_agent_name`` on purpose: advertising the launch mode is safe for
    #: both runtimes (it only decides what the start can see at session open),
    #: whereas ``request()``'s mid-session tolerance must stay direct-client-only
    #: (the shared runtime keeps strict resolution on a mid-session switch so a
    #: vanished view fails closed). Never consumed; it describes a fixed launch
    #: fact, not a one-shot exemption. Empty until a spawn claims the projection.
    advertised_launch_name: str = ""
    _lease_finalizer: Any = field(default=None, repr=False, compare=False)
    # Aliases an EARLIER projection of this process published, alias -> agent. The
    # host may still hold them (every alias it loaded at spawn, say), so inbound
    # frames keep translating them back even after this projection renamed the
    # agent; only outbound requests use ``aliases``.
    _recognised: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    _recognition_overflow_warned: bool = field(default=False, repr=False, compare=False)

    def recognise(self, earlier: "NativeSkillProjection") -> None:
        """Keep translating the aliases *earlier* knew in inbound frames, up to a bound.

        Called with the spawn projection FIRST, so its aliases -- the ones the host
        is guaranteed to hold -- are the last to be refused once the bound is met.
        Only alias-shaped names mapped to admissible agent names are kept, so the
        count bound bounds the memory too.
        """
        # Carry the launch identity's ADVERTISING across a projection refresh: the
        # shared runtime adopts a fresh projection mid-session, and frame() runs on
        # that fresh object, so without this a no-view launch agent's mode would be
        # dropped from availableModes again after the refresh. This is SAFE to carry
        # (advertising only decides what a session open can see) and is kept
        # distinct from ``spawn_agent_name``, which is deliberately NOT propagated
        # here -- carrying that would make request() tolerate the launch agent on a
        # mid-session switch on the shared runtime, reactivating a cached spec the
        # strict resolver exists to refuse. A fresh projection that already recorded
        # its own launch name keeps it.
        if not self.advertised_launch_name and earlier.advertised_launch_name:
            self.advertised_launch_name = earlier.advertised_launch_name
        dropped = 0
        for alias, name in (
            *((a, n) for n, a in earlier.aliases.items()),
            *earlier._recognised.items(),
        ):
            if alias in self._recognised:
                continue
            if len(self._recognised) >= _RECOGNISED_ALIASES_MAX:
                dropped += 1
                continue
            # The registered agent-name grammar bounds each retained name and admits
            # no control character; an alias is a fixed-length digest name.
            if not _admissible_source_agent(name) or not _LEGACY_ALIAS_NAME_RE.fullmatch(alias):
                continue
            self._recognised.setdefault(alias, name)
        if dropped and not self._recognition_overflow_warned:
            # Once per projection: the bound is a backstop, and past it the oldest
            # aliases this process published stop translating in inbound frames.
            self._recognition_overflow_warned = True
            logger.warning(
                "skill projection: %d earlier skill-view alias(es) not recognised past the "
                "%d-alias bound; a mode the host still lists only under one of them will "
                "not be offered until the gateway restarts",
                dropped,
                _RECOGNISED_ALIASES_MAX,
            )

    def agent(self, name: str) -> str:
        if is_skill_view_name(name):
            if name in self.aliases.values():
                return name
            source = remembered_view_source(name)
            if source is None:
                raise RetiredSkillView(name)
            name = source
        if name not in self.aliases:
            if name in self.errors:
                raise ValueError(f"Agent {name!r}: {self.errors[name]}")
            raise ValueError(f"Agent {name!r} has no prepared skill discovery view")
        return self.aliases[name]

    def spawn_agent(self, name: str) -> str:
        """Resolve the ``--agent`` transport name for a spawn, tolerating no view.

        The strict :meth:`agent` guards ``session/set_mode``: a mid-session
        switch to a mode this projection never prepared must be rejected, so an
        agent cannot escape the scope it was launched under. Spawn selection asks
        a softer question. An agent whose spec an authored restriction refused --
        a ``kirocrew-core`` exclusion, a disabled ``skill_search`` -- is a
        user-facing spawn refusal that still raises (the callers wrap it as
        ``AcpRuntimeError`` and the startup paths translate the sentence). An
        agent that simply has no prepared view -- its spec is not among the
        projected agents, as in a work_dir that carries no such spec -- keeps its
        authored transport name, the same answer a ``None`` projection gives: the
        agent spawns under its own name rather than aborting an otherwise valid
        spawn over a skill view it never asked for.
        """
        if is_skill_view_name(name) or name in self.aliases or name in self.errors:
            return self.agent(name)
        # The name is in no projected map: its spec is genuinely absent, so this is
        # the no-view case and the agent spawns under its own authored name rather
        # than aborting a spawn over a skill view it never asked for.
        return name

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/set_mode":
            mode_id = str(params.get("modeId", ""))
            # The launched agent's own activation is tolerated even with no
            # prepared view: the process is already running as it, so the initial
            # ``set_mode`` that activates it must not be refused. ``spawn_agent``
            # gives that name back unchanged; every other modeId takes the strict
            # ``agent``, so a mid-session switch to a mode this projection never
            # prepared is still rejected and an agent cannot escape its scope.
            tolerated = bool(mode_id) and mode_id == self.spawn_agent_name
            resolve = self.spawn_agent if tolerated else self.agent
            translated = {**params, "modeId": resolve(mode_id)}
            if tolerated:
                # CONSUME the exemption once spent: it covers only the launched
                # agent's FIRST activation. Cleared, a later set_mode back to the
                # same agent takes the strict ``agent`` again -- so a view that has
                # since vanished fails closed instead of reactivating a cached spec
                # the strict resolver existed to refuse.
                self.spawn_agent_name = ""
            return translated
        if method == "_kiro.dev/commands/execute":
            command = params.get("command", "")
            if isinstance(command, dict):
                name = str(command.get("command", "")).lstrip("/")
                args = command.get("args") or {}
                value = str(args.get("value", "")) if isinstance(args, dict) else ""
            else:
                words = str(command).strip().lstrip("/").split(None, 1)
                name = words[0] if words else ""
                value = words[1] if len(words) > 1 else ""
            if name == "agent" and value.strip() not in {"list", "schema"}:
                raise ValueError(
                    "Use Crew's agent selector to change agents so its skill scope stays in sync."
                )
        return params

    def frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        reverse = {**self._recognised, **{alias: name for name, alias in self.aliases.items()}}

        def visit(value: Any, field: str = "") -> Any:
            if isinstance(value, dict):
                return {key: visit(item, key) for key, item in value.items()}
            if isinstance(value, list):
                if field == "availableModes":
                    # A mode stays listed when the host advertises ANY name this
                    # process knows the agent by: one of its aliases, or the
                    # authored id of a projected agent (listed while the host has
                    # not loaded the alias, so the start reaches set_mode and its
                    # actionable refusal instead of "not installed"). Each agent is
                    # listed once; unprojected host agents stay hidden.
                    kept: list[Any] = []
                    listed: set[str] = set()
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        mode_id = item.get("id")
                        name = reverse.get(mode_id) if isinstance(mode_id, str) else None
                        if name is None and mode_id in self.aliases:
                            name = mode_id
                        # The launched agent's own mode stays listed even with no
                        # prepared view: the start reads ``availableModes`` before
                        # sending ``set_mode``, so dropping it would advertise no
                        # mode the process could activate and fail the session open.
                        # Keyed on ``advertised_launch_name`` -- set by BOTH the
                        # direct client and the shared runtime -- not on
                        # ``spawn_agent_name``, which the shared runtime leaves empty
                        # to keep ``request()`` strict on a mid-session switch:
                        # advertising the launch mode at open is safe for both,
                        # whereas tolerating a mid-session re-activation is not.
                        if (
                            name is None
                            and bool(mode_id)
                            and mode_id == self.advertised_launch_name
                        ):
                            name = mode_id
                        if name is None or name in listed:
                            continue
                        listed.add(name)
                        kept.append(item)
                    value = kept
                return [visit(item) for item in value]
            if field in {"id", "name", "agentName", "modeId", "currentModeId"} and isinstance(
                value, str
            ):
                if value in reverse:
                    return reverse[value]
                if is_skill_view_name(value):
                    # A view an earlier projection of this process published:
                    # report its agent, so no session stores a view name.
                    return remembered_view_source(value) or value
            return value

        return visit(frame)

    def release(self) -> None:
        """End this projection's hold on its views: drop the lease, prune eagerly.

        The views stay translatable (``aliases``, ``frame``) -- a frame still in
        flight may name one -- but from here on nothing in this process claims
        them on disk: the projection leaves the active set, its lease is
        released, and this home's unleased views are pruned at once rather than
        on the next spawn (see :func:`_end_projection`). Idempotent; blocking
        (bounded by the publication lock's own ceiling), so an event-loop caller
        offloads it. A projection that is simply dropped gets the same treatment
        from its finalizer; this is the explicit form for a session end that
        should not wait for garbage collection.
        """
        finalizer = self._lease_finalizer
        self._lease_finalizer = None
        with _ACTIVE_PROJECTIONS_LOCK:
            _ACTIVE_PROJECTIONS.pop(id(self), None)
        if finalizer is not None and finalizer.alive:
            # Runs ``_end_projection`` once, now, and detaches it from GC.
            finalizer()


_ACTIVE_PROJECTIONS: weakref.WeakValueDictionary[int, NativeSkillProjection] = (
    weakref.WeakValueDictionary()
)
_ACTIVE_PROJECTIONS_LOCK = threading.Lock()


def _register_active_projection(projection: NativeSkillProjection) -> None:
    with _ACTIVE_PROJECTIONS_LOCK:
        _ACTIVE_PROJECTIONS[id(projection)] = projection


def _active_aliases() -> set[str]:
    with _ACTIVE_PROJECTIONS_LOCK:
        projections = tuple(_ACTIVE_PROJECTIONS.values())
    return {alias for projection in projections for alias in projection.aliases.values()}
