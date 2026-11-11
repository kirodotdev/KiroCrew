"""A crewmate's own harness pin: ``agents.<name>.acp_backend``.

A crewmate can name the agent harness its own chat and the worker sessions it opens
run on, beside the model it already pins. The value is spelled as
``agent.acp_backend`` is in ``config.json`` (``kiro``, ``kas``, ``claude``, ...);
``""`` pins nothing and the Crew bot inherits the loaded ``agent.member_acp_backend``:
a stored (legacy) value, else ``agent.acp_backend``, else KAS when Crew bots cannot
run on that harness (``config.loader_builders._read_backend_choice``). One more
case runs a Crew bot on KAS on its own: it inherits a harness that cannot carry its
worker spec (:func:`inherited_falls_back`).
:func:`crewmate_harness_source` says which of those decided it.

The pin is one input to the backend-selection gate
(``members.select_provider_backend``), never a second gate. What this module adds
is how a session finds its crewmate -- from the session key alone, so every reader
of the gate answers the same way -- and the one rule the pin does
NOT share with the other tiers: a pinned harness this gateway cannot start refuses
the session (:class:`CrewmateHarnessUnavailable`) instead of degrading to kiro-cli,
because running the crewmate on a harness it was moved away from is what the pin
exists to prevent.

That refusal is a pre-gate check (harness-parity H17), not an outcome of the gate:
:func:`crewmate_backend_pin` checks the pin and raises, and hands the gate only a
:class:`StartableCrewPin` it has already confirmed, so the gate itself never raises
and keeps its degrade-with-a-logged-reason contract (H3) for every other tier.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.agent_sdk import backends as _backends

logger = logging.getLogger(__name__)

#: The machine code a refused pin carries, on the error row and in API replies.
HARNESS_UNAVAILABLE_CODE = "crewmate_harness_unavailable"


def display_stored_harness(stored: str) -> str:
    """A stored spelling safe for output, never for selection.

    Hand-edited pins may contain arbitrary text, including credentials. Only
    empty (inherit) and known harness spellings may leave the selection boundary.
    Selectability is deliberately irrelevant: a known but denied pin stays named.
    """
    if not stored or any(
        stored == _backends.backend_to_stored(backend) for backend in _backends.ACP_BACKENDS_KNOWN
    ):
        return stored
    return "unknown"


class CrewmateHarnessUnavailable(RuntimeError):
    """A crewmate's pinned harness is one this gateway cannot start.

    Raised before any process is spawned. The message names the crewmate, a
    known harness (never an arbitrary stored value) and the fix; safe for logs
    and the chat's error row. ``stored`` retains the original input internally.
    """

    code = HARNESS_UNAVAILABLE_CODE

    def __init__(self, crew: str, stored: str) -> None:
        self.crew = crew
        self.stored = stored
        runtime = _backends.backend_from_stored(display_stored_harness(stored))
        host_reason = (
            _backends.host_unservable_reason(runtime) if isinstance(runtime, str) else None
        )
        why = f" ({host_reason})" if host_reason else ""
        super().__init__(
            f"{HARNESS_UNAVAILABLE_CODE}: {crew} is set to run on {harness_label(stored)}, "
            f"which this gateway cannot start{why}. Pick another harness in {crew}'s "
            "settings, or clear it to inherit the default. Nothing runs on another "
            "harness in its place."
        )


class KasUnavailable(CrewmateHarnessUnavailable):
    """An LTS session would run on KAS, and KAS cannot start on this host.

    RFC D6: KAS is the LTS floor and nothing falls back to the Kiro CLI
    harness. The gateway started because its main sessions do not run KAS (a
    policy-allowed pick of another harness); this session does (a crewmate DM or worker, a
    ``spawn_run(backend=...)`` override, a main backend switched to KAS), so it is
    refused before any process exists (``lts_boot.refuse_unstartable_kas``).
    Same ``code`` as an unstartable pin (H17), so every caller that handles that
    refusal handles this one. *failed* and *fix* come from
    ``lts_boot.kas_refusal``.
    """

    def __init__(self, failed: str, fix: str) -> None:
        self.crew = ""
        self.stored = _backends.backend_to_stored(_backends.ACP_BACKEND_KAS)
        RuntimeError.__init__(
            self,
            f"{HARNESS_UNAVAILABLE_CODE}: this session runs on KAS, and {failed}. "
            f"{fix} Nothing runs on another harness in its place.",
        )


#: The two harnesses ``tool_gate.label_for`` answers with their raw id; every
#: other known harness, Kiro CLI included, takes its name from that one map.
_LABELS = {"kas": "Kiro Agent", "claude": "Claude Code"}


def harness_label(stored: str) -> str:
    """A readable name for the harness *stored* names (config spelling), for messages.

    The dashboard's own names for Kiro Agent and Claude Code, the operator-facing
    labels ``tool_gate.label_for`` already holds for the rest. Unknown stored
    text is never echoed: it may be a pasted credential rather than a harness.
    """
    stored = display_stored_harness(stored)
    if stored in _LABELS:
        return _LABELS[stored]
    from kiro_crew.agent_sdk.tool_gate import label_for

    runtime = _backends.backend_from_stored(stored)
    if isinstance(runtime, str) and runtime in _backends.ACP_BACKENDS_KNOWN:
        return label_for(runtime)
    return "an unknown harness"


@dataclass(frozen=True)
class CrewBackendPin:
    """One crewmate's harness pin: its ``config.agents`` key and the value as stored.

    *kas_bundle* is ``agent.kas_bundle`` from the same config: on the bundle route
    a stored Kiro CLI pin keeps the Kiro CLI launch (see :meth:`resolve`).
    """

    crew: str
    stored: str = field(repr=False)
    kas_bundle: bool = False

    @property
    def display_stored(self) -> str:
        """The pin's known spelling, or a fixed placeholder; never a selection input."""
        return display_stored_harness(self.stored)

    def resolve(self) -> str:
        """The runtime backend id the pin runs on, or raise when it cannot start here.

        The pre-gate check (H17): it reads ``selectable_backends()`` now, so a
        harness that has since been denied or marked unservable refuses.

        A stored Kiro CLI pin (``kiro``) reads as a stored ``agent.acp_backend``
        does in ``config.loader_builders._read_backend_choice``: off the KAS bundle
        route, ``kiro_cli_pick_backend()`` decides, which answers KAS once the
        host's kiro-cli is confirmed to offer v3 and while KAS is selectable
        (the decision record ``docs/decisions/*-a-kiro-cli-choice-runs-on-kas.md``).
        This is a stored-value read, so the selection gate never moves a Kiro CLI id itself.
        """
        runtime = _backends.backend_from_stored(self.stored)
        if isinstance(runtime, str) and runtime in _backends.selectable_backends():
            if runtime == _backends.ACP_BACKEND_KIRO and not self.kas_bundle:
                return _backends.kiro_cli_pick_backend()
            return runtime
        raise CrewmateHarnessUnavailable(self.crew, self.stored)

    def checked(self) -> StartableCrewPin:
        """This pin, confirmed startable, for the selection gate; raises when it is not.

        A startable pin to a harness Bots cannot run on (:func:`bots_cannot_run_on`:
        goose, opencode, ...) runs the Bot on KAS through the gate (the floor when a
        policy denies KAS), logged once and rung in the bell
        (``crewmate_harness_notice``), with ``harness_fallback`` set.
        A pin that cannot start at all still raises (H17).
        """
        runtime = self.resolve()
        if bots_cannot_run_on(runtime):
            # KAS still crosses the gate (H17): a policy that denies it lands on the
            # floor, and the log and the bell name that floor.
            landed = _backends.resolve_selected_backend(_backends.ACP_BACKEND_KAS)
            _note_pin_fallback(self.crew, runtime, landed)
            return StartableCrewPin(self.crew, self.stored, landed, harness_fallback=True)
        return StartableCrewPin(self.crew, self.stored, runtime)

    def effective(self) -> str:
        """The runtime backend the Bot runs on with this pin; raises like :meth:`checked`."""
        return self.checked().backend


@dataclass(frozen=True)
class StartableCrewPin:
    """A crewmate's harness pin the pre-gate check (H17) confirmed this gateway can start.

    The only pin ``members.select_provider_backend`` takes. *backend* is the runtime
    id the pin resolved to; the gate returns it as is and has no refusal of its own.
    """

    crew: str
    stored: str
    backend: str
    #: ``True`` for no stored pin: the Crew bot runs on KAS because the harness it
    #: inherits cannot carry its worker spec (:func:`inherited_falls_back`).
    worker_spec_fallback: bool = False
    #: ``True`` when the pinned harness is one Bots cannot run on, so KAS runs.
    harness_fallback: bool = False


def bots_cannot_run_on(backend: str) -> bool:
    """Whether a Bot cannot run on *backend*: no saved spec, dispatch tools or worker kit.

    The sets ``agent_sdk.backends.crewmate_inherited_backend`` reads. Kiro CLI is
    not judged here: a Kiro CLI pin keeps its own launch rule (:meth:`CrewBackendPin.resolve`).
    """
    if backend == _backends.ACP_BACKEND_KIRO:
        return False
    return _backends.crewmate_inherited_backend(backend)[1]


#: ``(crew, pinned backend)`` pairs whose pin fallback was logged in this process.
_pin_fallback_logged: set[tuple[str, str]] = set()


def bots_may_run_on_kas(config: Any) -> bool:
    """Whether a Bot here can start on KAS though neither harness field names it. Blocking.

    An inherited ``member_acp_backend`` (a Bot then falls back to KAS when its
    worker spec needs it, :func:`inherited_falls_back`), or a pin that runs on KAS:
    a stored KAS pin, or one to a harness Bots cannot run on (:meth:`CrewBackendPin.checked`).
    The kiro-cli floor (``platform.bootstrap.apply_kas_cli_floor``) probes for these
    too, so such a Bot never starts KAS on a kiro-cli that cannot serve it.
    """
    from kiro_crew.config.loader_builders import MEMBER_BACKEND_INHERITED_ATTR

    if getattr(getattr(config, "agent", None), MEMBER_BACKEND_INHERITED_ATTR, False):
        return True
    for crew in list(getattr(config, "agents", {}) or {}):
        pin = crewmate_pin_for(config, str(crew))
        if pin is None:
            continue
        try:
            runtime = pin.resolve()
        except CrewmateHarnessUnavailable:
            continue
        if runtime == _backends.ACP_BACKEND_KAS or bots_cannot_run_on(runtime):
            return True
    return False


def kas_runs_here(config: Any) -> bool:
    """Whether a session here runs on KAS, and so signs in with the Kiro sign-in. In memory.

    The main harness, the Bot harness as loaded once some Bot has no pin and so
    inherits it (an inherited Kiro CLI and a stored ``kiro`` both load as KAS), a
    Bot pin that runs on KAS (a KAS pin, or one to a
    harness Bots cannot run on), or a Bot with no pin whose worker spec moves it
    onto KAS (:func:`worker_spec_forces_kas`, the held answer on the event loop).
    Unlike :func:`bots_may_run_on_kas`, an inherited Bot harness alone is not
    enough: the dashboard shows the Kiro sign-in on this answer, and a gateway
    where nothing runs on KAS has no use for it. A ``spawn_run(backend=...)``
    override is a per-spawn choice, not config, so it is not read here.
    """
    kas = _backends.ACP_BACKEND_KAS
    if kas not in _backends.selectable_backends():
        # A Bot pinned to a harness Bots cannot run on lands on the floor instead.
        return False
    agent = getattr(config, "agent", None)
    if getattr(agent, "acp_backend", "") == kas:
        return True
    # The Bot harness is only a harness something runs on once a Bot inherits it:
    # it loads as KAS on a Kiro CLI gateway with no Bots at all.
    bots_inherit_kas = getattr(agent, "member_acp_backend", "") == kas
    for crew in list(getattr(config, "agents", {}) or {}):
        pin = crewmate_pin_for(config, str(crew))
        if pin is None:
            if bots_inherit_kas or worker_spec_forces_kas(config, str(crew)):
                return True
            continue
        try:
            runtime = pin.resolve()
        except CrewmateHarnessUnavailable:
            continue
        if runtime == kas or bots_cannot_run_on(runtime):
            return True
    return False


def _note_pin_fallback(crew: str, pinned: str, landed: str) -> None:
    """Say once per process per Bot and harness where its pin runs instead (H3, H17)."""
    with _VERDICT_LOCK:
        if (crew, pinned) in _pin_fallback_logged:
            return
        _pin_fallback_logged.add((crew, pinned))
    logger.error(
        "Bot %r is pinned to %r, which bots cannot run on; its chat and workers run "
        "on %r instead. Pin a harness bots can run on to choose another.",
        crew,
        _backends.backend_to_stored(pinned),
        _backends.backend_to_stored(landed),
    )


def crewmate_pin_for(config: Any, crew: str | None) -> CrewBackendPin | None:
    """Crew *crew*'s harness pin, or ``None`` when it pins none (or is no crew)."""
    record = config.agents.get(crew) if crew else None
    stored = getattr(record, "acp_backend", "") if record is not None else ""
    if not isinstance(stored, str) or not stored.strip():
        return None
    return CrewBackendPin(str(crew), stored.strip(), _kas_bundle(config))


def _kas_bundle(config: Any) -> bool:
    """``agent.kas_bundle`` as *config* holds it; ``False`` when the section lacks it."""
    return bool(getattr(getattr(config, "agent", None), "kas_bundle", False))


def crew_for_member_id(config: Any, member_id: str) -> str:
    """The ``config.agents`` key whose persisted ``member_id`` is *member_id*, or ``""``."""
    for name, record in config.agents.items():
        if member_id and getattr(record, "member_id", "") == member_id:
            return str(name)
    return ""


def _crew_for_dm_slug(config: Any, slug: str) -> str:
    """The crew whose DM slot slug is *slug*: its persisted id, else a legacy slug."""
    from kiro_crew import members

    by_id = crew_for_member_id(config, slug)
    if by_id:
        return by_id
    for name in config.agents:
        try:
            if members.member_slug(name, config) == slug:
                return str(name)
        except (members.MemberSlugError, ValueError):
            continue
    return ""


def crewmate_for_session(config: Any, session_key: str | None) -> str:
    """The ``config.agents`` key of the crewmate *session_key* runs as, or ``""``.

    Two kinds of session answer: a crewmate's pinned DM thread (``member-<slug>``,
    any layer prefix, any ``.memory-<store>`` suffix) and a crewmate's own worker
    (known from its execution record, ``members.crewmate_worker_member_id``). Every
    other session -- a template chat, a sub-agent, a cron turn not on the worker --
    names no crewmate here. In-memory only, so it is safe on the event loop.
    """
    from kiro_crew import members

    if not session_key:
        return ""
    if members.is_member_session_key(session_key):
        slot = members._slot_part(session_key)
        slug = members.slug_from_dm_slot_key(slot)
        return _crew_for_dm_slug(config, slug) if slug else ""
    member_id = members.crewmate_worker_member_id(session_key)
    return crew_for_member_id(config, member_id) if member_id else ""


def crewmate_stored_pin(config: Any, session_key: str | None) -> CrewBackendPin | None:
    """The harness pin of the crewmate *session_key* runs as, as stored, or ``None``.

    Never raises, and checks no startability: a display reads ``display_stored``
    to name a known pin even when it cannot start, without echoing unknown text.
    Nothing may start a session from this unchecked pin.
    """
    return crewmate_pin_for(config, crewmate_for_session(config, session_key))


def route_pin(config: Any, session_key: str | None, backend_override: str | None) -> str:
    """The Bot pin KAS's route rule 2 judges *session_key* by, as stored, or ``""``.

    In the selection's order: an explicit *backend_override* outranks the pin,
    so an overridden session carries none and is judged by the installation's
    pick.
    """
    if backend_override is not None:
        return ""
    pin = crewmate_stored_pin(config, session_key)
    return pin.stored if pin is not None else ""


def crewmate_backend_pin(config: Any, session_key: str | None) -> StartableCrewPin | None:
    """The pre-gate check (H17): *session_key*'s crewmate pin, confirmed startable.

    What every caller of ``members.select_provider_backend`` passes as its
    ``crew_backend``, evaluated in that call's own arguments, so the check runs
    before the gate does. ``None`` when the session pins nothing. A pin this
    gateway cannot start raises :class:`CrewmateHarnessUnavailable` HERE, so the
    crewmate's session is refused before the gate and never falls back to another
    harness, while the gate keeps its never-raises contract (H3).

    A Crew bot with no pin that :func:`inherited_falls_back` moves onto KAS gets a
    KAS pin here (``worker_spec_fallback``), so its chat and its workers take KAS
    through the same gate.
    """
    crew = crewmate_for_session(config, session_key)
    pin = crewmate_pin_for(config, crew)
    if pin is not None:
        return pin.checked()
    if inherited_falls_back(config, crew):
        return StartableCrewPin(crew, "", _backends.ACP_BACKEND_KAS, worker_spec_fallback=True)
    return None


def pinned_backend(config: Any, crew: str | None, fallback: str) -> str:
    """The runtime backend crew *crew*'s sessions run on: its pin, else *fallback*.

    Raises :class:`CrewmateHarnessUnavailable` for a pin that cannot start: the
    same pre-gate check (H17) :func:`crewmate_backend_pin` runs. A Crew bot
    :func:`inherited_falls_back` moves onto KAS answers KAS.
    """
    pin = crewmate_pin_for(config, crew)
    if pin is not None:
        return pin.checked().backend
    return _backends.ACP_BACKEND_KAS if inherited_falls_back(config, crew) else fallback


def worker_backend_for_spec(config: Any, crew: str, kas_only: bool) -> str:
    """The backend crew *crew*'s worker runs on once its file holds a new spec.

    *kas_only* says whether that new spec sets ``hooks``, ``permissions`` or
    ``toolsSettings``, which only KAS carries. A pin
    decides as always. Without one, a Bot that follows the Settings harness and
    gains such a field runs on KAS (:func:`worker_spec_kas_fields`), so a write
    is judged on KAS rather than on the harness the file before it allowed.
    Raises :class:`CrewmateHarnessUnavailable` for a pin that cannot start.
    """
    from kiro_crew.config.loader_builders import MEMBER_BACKEND_INHERITED_ATTR
    from kiro_crew.mate_worker import crewmate_worker_backend

    if crewmate_pin_for(config, crew) is None and kas_only:
        agent = getattr(config, "agent", None)
        if (
            getattr(agent, MEMBER_BACKEND_INHERITED_ATTR, False)
            and _backends.ACP_BACKEND_KAS in _backends.selectable_backends()
        ):
            return _backends.ACP_BACKEND_KAS
    return crewmate_worker_backend(config, crew)


def inherited_falls_back(config: Any, crew: str | None) -> bool:
    """Whether crew *crew*, with no pin, runs on KAS instead of what it inherits.

    See :func:`worker_spec_forces_kas`, which this applies to a Crew bot with no pin
    of its own: a pin is the person's choice and is never moved, and one that
    cannot carry the Crew bot's worker spec still refuses (H17). Logged once per
    Crew bot and harness (H3).
    """
    if not crew or crewmate_pin_for(config, crew) is not None:
        return False
    fields = worker_spec_kas_fields(config, str(crew))
    if fields:
        _note_worker_spec_fallback(str(crew), _inherited_backend(config), fields)
    return bool(fields)


def worker_spec_forces_kas(config: Any, crew: str) -> bool:
    """Whether crew *crew*'s inherited harness cannot run its workers, so it runs on KAS.

    :func:`worker_spec_kas_fields` is non-empty. Whether a pin exists is not asked
    here: the readout uses this to say what Inherited gives.
    """
    return bool(worker_spec_kas_fields(config, crew))


def _inherited_backend(config: Any) -> str:
    agent = getattr(config, "agent", None)
    return _backends.resolve_selected_backend(getattr(agent, "member_acp_backend", ""))


def worker_spec_kas_fields(config: Any, crew: str) -> tuple[str, ...]:
    """The field kinds of :func:`worker_spec_kas_detail`."""
    return worker_spec_kas_detail(config, crew)[0]


#: ``(group, name)`` per exact entry to change; *name* is the Bot's file's own text.
KasItems = tuple[tuple[str, str], ...]


def worker_spec_kas_detail(config: Any, crew: str) -> tuple[tuple[str, ...], KasItems]:
    """What in crew *crew*'s worker spec only KAS carries, when that moves it to KAS.

    Non-empty when the Crew bot follows the Settings harness (no stored
    ``agent.member_acp_backend``: the loader marks that, see
    ``config.loader_builders.member_backend_inherited``), KAS is selectable, and its
    own file sets something the inherited harness cannot carry for its workers
    (``mate_worker.kas_only_worker_fields``: worker tool limits, ``hooks``,
    ``permissions``, ``toolsSettings``), or its saved Capabilities spec switches
    single tools off for its own chat (``chat tool limits``, outside
    ``ACP_BACKENDS_MEMBER_TOOL_LIMITS``). Its chat or its workers, which run on the
    same backend, would be refused, so the Crew bot runs on KAS, chat and workers,
    and is rung in the bell (``crewmate_harness_notice``).

    Returns those field kinds and, from the same read, each exact entry behind
    them (``mate_worker.kas_only_worker_detail``; ``chat tool limits`` gives the
    Capabilities spec's ``server/tool`` names), so the bell can say what to change.

    The answer about the Crew bot's file is held in memory (:func:`refresh_worker_spec_verdicts`).
    On the event loop this only reads that memory: a missing or outdated entry
    answers what is held (nothing at first) and refreshes in a background thread,
    so no folder walk or file read runs on the loop. Off the loop it reads the
    file when the memory is outdated.
    """
    from kiro_crew.config.loader_builders import MEMBER_BACKEND_INHERITED_ATTR

    agent = getattr(config, "agent", None)
    if not crew or not getattr(agent, MEMBER_BACKEND_INHERITED_ATTR, False):
        return (), ()
    inherited = _inherited_backend(config)
    if inherited == _backends.ACP_BACKEND_KAS:
        return (), ()
    if _backends.ACP_BACKEND_KAS not in _backends.selectable_backends():
        return (), ()
    record = config.agents.get(crew) if hasattr(config, "agents") else None
    member_id = str(getattr(record, "member_id", "") or "")
    if not member_id:
        return (), ()
    key = (crew, member_id, str(getattr(record, "kiro_agent", "") or ""), inherited)
    if _on_event_loop():
        return _held_verdict(key)
    return _fresh_verdict(key)


#: ``(crew, member_id, kiro_agent, inherited backend) -> (stamp, (fields, items))``; the stamp
#: is ``(spec generation, Crew bot folder revision, agents folder revision)``.
_VerdictKey = tuple[str, str, str, str]
_Verdict = tuple[tuple[str, ...], KasItems]
_VERDICTS: dict[_VerdictKey, tuple[tuple[int, object, object], _Verdict]] = {}
_VERDICT_LOCK = threading.Lock()
_REFRESHING: set[_VerdictKey] = set()


def _on_event_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _held_verdict(key: _VerdictKey) -> _Verdict:
    """The held answer for *key*, refreshed in the background when outdated. In memory."""
    from kiro_crew.agent_discovery import spec_cache_generation

    with _VERDICT_LOCK:
        hit = _VERDICTS.get(key)
        outdated = hit is None or hit[0][0] != spec_cache_generation()
        start = outdated and key not in _REFRESHING
        if start:
            _REFRESHING.add(key)
    if start:
        threading.Thread(
            target=_refresh_in_background,
            args=(key,),
            name="crewmate-worker-spec-verdict",
            daemon=True,
        ).start()
    return hit[1] if hit is not None else ((), ())


def _refresh_in_background(key: _VerdictKey) -> None:
    try:
        _fresh_verdict(key)
    except Exception:  # noqa: BLE001 - a failed refresh keeps the held answer
        logger.debug("could not refresh the Crew bot worker-spec verdict", exc_info=True)
    finally:
        with _VERDICT_LOCK:
            _REFRESHING.discard(key)


def _fresh_verdict(key: _VerdictKey) -> _Verdict:
    """Blocking: the answer for *key*, read from the file when the memory is outdated."""
    from kiro_crew.agent_discovery import agents_dir_revision, spec_cache_generation
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.mate_worker import kas_only_worker_detail

    crew, member_id, _agent, inherited = key
    stamp = (
        spec_cache_generation(),
        agents_dir_revision(paths_mod.mate_agents_dir()),
        agents_dir_revision(paths_mod.kiro_agents_dir()),
    )
    with _VERDICT_LOCK:
        hit = _VERDICTS.get(key)
    if None not in stamp and hit is not None and hit[0] == stamp:
        return hit[1]
    fields, items = kas_only_worker_detail(member_id, inherited)
    chat = _chat_tool_limits(crew, inherited)
    if chat:
        fields = ("chat tool limits", *fields)
        items = (*(("turned off in Capabilities", name) for name in chat), *items)
    verdict = (fields, items)
    with _VERDICT_LOCK:
        # An unprovable revision is still held for the loop, never trusted off it.
        _VERDICTS[key] = (stamp, verdict)
    return verdict


def _chat_tool_limits(crew: str, inherited: str) -> tuple[str, ...]:
    """The tools crew *crew*'s saved Capabilities spec switches off that its chat can't.

    ``server/tool`` each (``agent_capabilities.disabled_tool_names``); empty when none.

    ``agent_capabilities.prepare_member_capabilities`` is the read the chat start
    refuses on (``capability_tool_limits_unsupported``). It is asked with no project,
    so a spec saved under a project, which it refuses here, is read directly: the
    private copy's ``tool_limited_servers``. A spec neither read finds answers
    nothing: the start judges it and says why. Blocking.
    """
    if inherited in _backends.ACP_BACKENDS_MEMBER_TOOL_LIMITS:
        return ()
    from kiro_crew import agent_capabilities

    try:
        prepared = agent_capabilities.prepare_member_capabilities(crew)
    except Exception:  # noqa: BLE001 - read the private copy directly instead
        return _private_copy_tool_limits(crew)
    if not prepared.get("tool_limited_servers"):
        return ()
    return tuple(prepared.get("disabled_tools") or prepared["tool_limited_servers"])


def _private_copy_tool_limits(crew: str) -> tuple[str, ...]:
    """The tools crew *crew*'s saved private copy switches off, ``server/tool`` each. Blocking."""
    from kiro_crew import agent_capabilities, agent_state
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        binding = KiroCrewConfig.load().agents.get(crew)
        if binding is None or agent_state.get_capabilities(binding.kiro_agent) is None:
            return ()
        _path, spec, _own = agent_capabilities._source(binding.kiro_agent, "", allow_private=True)
    except Exception:  # noqa: BLE001 - the chat start judges an unreadable spec
        return ()
    return agent_capabilities.disabled_tool_names(spec)


async def refresh_crew_verdict(crew: str) -> None:
    """Read crew *crew*'s worker-spec answer into memory, off the loop. Never raises.

    Awaited before a model save is judged (``crew_update``), so the save and the
    next start agree even after a file edit made outside Crew.
    """
    import asyncio

    def _read() -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
        # Pinned or not: a save may clear the pin, and then Inherited decides.
        if crew in config.agents:
            worker_spec_kas_fields(config, crew)

    try:
        await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001 - the save's own checks still judge the pin
        logger.debug("could not refresh the bot worker-spec verdict", exc_info=True)


async def refresh_session_verdict(session_key: str) -> None:
    """Read *session_key*'s Bot worker-spec answer into memory, off the loop.

    Awaited before every Bot cold start (``get_or_create``), so the provider
    factory's in-memory read is the answer for the files on disk now, including
    a file edited outside Crew. The held answer stays only a hint for readouts.
    Never raises: a failed read leaves the held answer, and the start's own
    worker checks still refuse what cannot be carried.
    """
    import asyncio

    try:
        moved, config, crew = await asyncio.to_thread(
            _refresh_session_verdict_blocking, session_key
        )
    except Exception:  # noqa: BLE001 - the start's own checks still fail closed
        logger.debug("could not refresh the bot worker-spec verdict", exc_info=True)
        return
    if not crew:
        return
    from kiro_crew import crewmate_harness_notice
    from kiro_crew.slack.handler import get_dashboard_state

    state = get_dashboard_state()
    if state is None:
        return
    if moved:
        # The bell says so too, once per fact (``crewmate_harness_notice``).
        await crewmate_harness_notice.deliver(state)
    else:
        # The first Bot start after this Bot left KAS for the Settings harness:
        # told once per install (RFC section 6).
        await crewmate_harness_notice.deliver_moved(state, config, crew)


def _refresh_session_verdict_blocking(session_key: str) -> tuple[bool, Any, str]:
    """Blocking: refresh *session_key*'s Bot answer.

    Returns whether it runs the bot on KAS, the config read and the bot's name
    (``""`` for no bot, or a pinned one, which neither note concerns).
    """
    from kiro_crew.config.loader import KiroCrewConfig

    config = KiroCrewConfig.load()
    crew = crewmate_for_session(config, session_key)
    if not crew or crewmate_pin_for(config, crew) is not None:
        return False, config, ""
    return bool(worker_spec_kas_fields(config, crew)), config, crew


def refresh_worker_spec_verdicts(config: Any) -> None:
    """Blocking: read every Crew bot's worker-spec answer into memory.

    Run off the loop at gateway start and on every harness or Crew bot config
    change (``crewmate_harness_notice.deliver``), so the event loop reads a held
    answer from the first session on.
    """
    for crew in list(getattr(config, "agents", {}) or {}):
        if _on_event_loop():  # pragma: no cover - callers run this in a thread
            return
        worker_spec_kas_fields(config, str(crew))


#: ``(crew, inherited backend)`` pairs whose fallback was logged in this process.
_fallback_logged: set[tuple[str, str]] = set()


def _note_worker_spec_fallback(crew: str, inherited: str, fields: tuple[str, ...]) -> None:
    """Say once per process per Crew bot and harness that it runs on KAS (H3)."""
    with _VERDICT_LOCK:
        if (crew, inherited) in _fallback_logged:
            return
        _fallback_logged.add((crew, inherited))
    logger.warning(
        "Crew bot %r sets %s for its workers, which %r cannot carry; its chat and "
        "workers run on %r instead of the inherited harness. Pin a harness for it or "
        "remove those settings to choose another.",
        crew,
        ", ".join(fields),
        _backends.backend_to_stored(inherited),
        _backends.backend_to_stored(_backends.ACP_BACKEND_KAS),
    )


def selectable_harnesses() -> list[str]:
    """Every harness a crewmate may be pinned to, in the stored spelling, sorted."""
    return sorted(_backends.backend_to_stored(b) for b in _backends.selectable_backends())


#: What a full Crew bot needs from its harness (RFC O6): the saved member spec
#: (capabilities), the session-control tools it dispatches workers with, and the
#: worker kit its workers run on. The same sets :func:`bots_cannot_run_on` reads,
#: so a pin the picker offers never lands on KAS.
CREWMATE_CAPABLE_BACKENDS = (
    _backends.ACP_BACKENDS_MEMBER_CAPABILITIES
    & _backends.ACP_BACKENDS_MEMBER_DISPATCH
    & _backends.ACP_BACKENDS_MATE_WORKER_KIT
)


def _harnesses_whose_runtime_is_in(config: Any, runtimes: frozenset[str]) -> list[str]:
    """The selectable harnesses whose pin starts on a runtime in *runtimes*, stored spelling, sorted.

    Each harness is judged by the runtime its pin starts on (:meth:`CrewBackendPin.resolve`),
    the same runtime a crewmate's chat and its workers check. A pin that resolves to
    KAS is left out when boot recorded that KAS cannot start here
    (:func:`kiro_crew.lts_boot.refuse_unstartable_kas`), because that session
    would be refused at its start.
    """
    from kiro_crew.lts_boot import refuse_unstartable_kas

    out: list[str] = []
    for backend in _backends.selectable_backends():
        stored = _backends.backend_to_stored(backend)
        try:
            runtime = CrewBackendPin("", stored, _kas_bundle(config)).resolve()
            if runtime == _backends.ACP_BACKEND_KAS:
                refuse_unstartable_kas()
        except CrewmateHarnessUnavailable:
            continue
        if runtime in runtimes:
            out.append(stored)
    return sorted(out)


def crewmate_harnesses(config: Any) -> list[str]:
    """The harnesses a crewmate may be pinned to here, in the stored spelling, sorted.

    The one list the harness picker offers and a save accepts: selectable on this
    gateway now (``selectable_backends()``, which the ``agent_backend`` governance
    scope already narrows), able to start here, AND crewmate-capable
    (:data:`CREWMATE_CAPABLE_BACKENDS`: KAS, Claude Code, Codex today; never pi or
    deepseek), judged on the runtime the pin starts on.
    So a Kiro CLI pin counts as KAS where kiro-cli's v3 engine takes it, and as
    kiro-cli, which is not crewmate-capable, everywhere else.
    """
    return _harnesses_whose_runtime_is_in(config, frozenset(CREWMATE_CAPABLE_BACKENDS))


def kas_pin_harnesses() -> list[str]:
    """Selectable harnesses a pin to which runs the Bot on KAS, stored spelling, sorted.

    On KAS through the gate (:meth:`CrewBackendPin.checked`): the floor when a
    policy denies KAS.
    """
    return sorted(
        _backends.backend_to_stored(b)
        for b in _backends.selectable_backends()
        if bots_cannot_run_on(b)
    )


def kas_pin_landing() -> str:
    """Where a pin in :func:`kas_pin_harnesses` runs, stored spelling: KAS through the gate.

    The floor (Kiro CLI) when a policy denies KAS (H17).
    """
    return _backends.backend_to_stored(
        _backends.resolve_selected_backend(_backends.ACP_BACKEND_KAS)
    )


def worker_harnesses(config: Any) -> list[str]:
    """The selectable harnesses a crewmate's worker can run on, stored spelling, sorted."""
    return _harnesses_whose_runtime_is_in(config, frozenset(_backends.ACP_BACKENDS_MATE_WORKER_KIT))


def pin_remedy(pin: CrewBackendPin, harnesses: list[str], *, clearing_helps: bool) -> str:
    """The fix for a session refused for what its crewmate's pinned harness lacks.

    The pin outranks ``agent.member_acp_backend`` (``members.select_provider_backend``),
    so a fix naming that setting cannot help. This names the pin and the harnesses
    in *harnesses* (stored spelling) that would carry the session, and clearing the
    pin only when *clearing_helps* (the member route would carry it).
    """
    crew = pin.crew
    fixes = []
    if harnesses:
        able = ", ".join(harness_label(h) for h in harnesses)
        fixes.append(f"in {crew}'s settings pick a harness that can ({able})")
    if clearing_helps:
        fixes.append("clear the pin")
    fix = " or ".join(fixes) or "no harness that can starts on this gateway"
    return f"{crew} is pinned to {harness_label(pin.stored)} (agents.{crew}.acp_backend); {fix}"


def harness_pin_rejected(value: object, config: Any) -> str | None:
    """Why *value* cannot be stored as a crewmate's harness pin, or ``None``.

    ``""`` clears the pin. Anything else must be in :func:`crewmate_harnesses`,
    so a save can never store a pin that would refuse every session or run a
    crewmate on a harness that cannot carry it.
    """
    if not isinstance(value, str):
        return "acp_backend must be a string"
    stored = value.strip()
    allowed = crewmate_harnesses(config)
    if not stored or stored in allowed:
        return None
    return "acp_backend must be one of: " + ", ".join(["(empty)", *allowed])


def pinned_entitlement_backend(
    config: Any, crew: str, acp_backend: str | None, *, for_workers: bool = False
) -> str | None:
    """The harness that judges crew *crew*'s model pins, when its harness pin decides.

    A crewmate that pins a harness runs its chat AND its workers there, so that
    harness judges both ``model`` and ``worker_model``. *acp_backend* is the pin a
    save is about to store (``""`` clears it); ``None`` reads *crew*'s record. A pin
    that cannot start raises :class:`CrewmateHarnessUnavailable`, so no model is
    judged for a harness the crewmate will not run on. With no pin, *for_workers*
    answers the worker's route (``mate_worker.crewmate_worker_backend``) and any
    other caller gets ``None``, keeping its own rule.
    """
    if acp_backend is None:
        pin = crewmate_pin_for(config, crew)
    elif acp_backend.strip():
        pin = CrewBackendPin(crew, acp_backend.strip(), _kas_bundle(config))
    else:
        pin = None
    if pin is not None:
        return pin.effective()
    if worker_spec_forces_kas(config, crew):
        # No pin to store, so the Crew bot runs on KAS (:func:`inherited_falls_back`).
        return _backends.ACP_BACKEND_KAS
    if for_workers:
        from kiro_crew.mate_worker import crewmate_worker_backend

        return crewmate_worker_backend(config)
    return None


def judges_a_model_pin(model: str) -> bool:
    """Whether *model* is a real pin a harness must judge; ``""``/``"auto"`` inherit.

    An inherit value passes every model check whatever the harness, so a save
    that only carries one needs no harness resolved, and a stored pin this
    gateway cannot start does not refuse it.
    """
    return bool(model) and model != "auto"


def judges_a_model_edit(model: str, stored: str, harness_moves: bool) -> bool:
    """Whether a crewmate save must judge *model* against the harness it will hold.

    A harness move judges every pin, kept or changed. Without one, an inherit
    value judges nothing and neither does the value already *stored*: the editor
    re-sends its models with every save, so a stored pin whose harness has since
    become unstartable must not refuse an edit to an unrelated field. A changed
    real pin is always judged.
    """
    return harness_moves or (judges_a_model_pin(model) and model != stored)


def kiro_spelling_applies(backend: str | None) -> bool:
    """Whether a model pin's kiro-cli spelling check applies on *backend*.

    *backend* is the harness the pin will run on (``_pin_entitlement_backend``):
    a pin, else the Crew bot route, inherited or stored. Only a harness that reads
    kiro-cli's ids has a "wrong" spelling; another (claude, codex, ...) takes its
    own wire ids as written. ``None`` (no harness known) keeps the check.
    """
    from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_for

    if backend is None:
        return True
    return capabilities_for(backend).model_id_namespace == MODEL_NAMESPACE_ACP


def harness_for_query(value: str) -> str | None:
    """The runtime backend a ``?backend=`` value names, or ``None`` when not selectable."""
    asked = value.strip()
    if asked not in selectable_harnesses():
        return None
    return str(_backends.backend_from_stored(asked) or "")


def models_route(config: Any, query: Any) -> tuple[str, bool] | None:
    """``GET /api/models``'s ``(runtime backend, KAS bundle route)``, or ``None``.

    *query* is the request's query mapping; only a present ``backend`` key asks.

    With no ``?backend=`` it is the installation's harness and the shared bundle
    predicate (``agent_sdk.kas_bundle.kas_bundle_route``). With one, the asked
    harness names its own route: the bundle only for KAS on the bundle route
    (``agent.kas_bundle`` on, or chosen because kiro-cli's relay cannot run),
    whatever the installation runs. ``None`` for a harness that is not
    selectable. Blocking (the bundle predicate reads config).
    """
    asked = query["backend"] if "backend" in query else None
    if asked is None:
        from kiro_crew.agent_sdk import kas_bundle

        return getattr(config.agent, "acp_backend", ""), kas_bundle.kas_bundle_route()
    backend = harness_for_query(asked)
    if backend is None:
        return None
    on_bundle = False
    if backend == _backends.ACP_BACKEND_KAS:
        from kiro_crew.agent_sdk import kas_bundle

        # The route rule the spawn applies (``acp/kas_route.py``): the switch, or
        # the automatic bundle route when kiro-cli's relay cannot run here.
        on_bundle = kas_bundle.kas_spawn_takes_bundle(config.agent)
    return backend, on_bundle


#: Where the harness a Crew bot runs on comes from, as the Settings page names it.
SOURCE_PIN = "pin"
SOURCE_LEGACY = "legacy"
SOURCE_SETTINGS = "settings"
SOURCE_FALLBACK = "fallback"
#: KAS for one Crew bot: its inherited harness cannot carry its worker spec.
SOURCE_WORKER_SPEC = "worker_spec"


@dataclass(frozen=True)
class CrewmateHarnessSource:
    """The harness a Crew bot runs on and which setting decided it.

    *harness* is the stored spelling of what runs (``""`` when a pin cannot start);
    *source* is one of the ``SOURCE_*`` values: the Crew bot's own pin, a stored
    ``agent.member_acp_backend`` (legacy), ``agent.acp_backend`` (Settings -> Agent
    Harness), KAS because Crew bots cannot run on that Settings harness, or KAS
    for one Crew bot whose worker spec that harness cannot carry;
    *settings_harness* is the stored spelling of ``agent.acp_backend``, which the
    fallback alarm names.
    """

    harness: str
    source: str
    settings_harness: str

    def as_dict(self) -> dict[str, str]:
        return {
            "harness": self.harness,
            "source": self.source,
            "settings_harness": self.settings_harness,
        }


def inherited_harness_source(config: Any) -> CrewmateHarnessSource:
    """What a Crew bot with no pin runs on, and where it comes from. Blocking.

    The harness is the loaded ``agent.member_acp_backend``, which the loader already
    resolved (``config.loader_builders._read_backend_choice``), crossed through the
    same gate every session start applies, so this never disagrees with a start.
    Only the label is worked out here: whether ``config.json`` holds the legacy
    setting (:func:`~kiro_crew.config.loader_document.member_acp_backend_declared`)
    and, if not, whether the Settings harness is one Crew bots can follow
    (:func:`~kiro_crew.agent_sdk.backends.crewmate_inherited_backend`).
    """
    from kiro_crew.config.loader_builders import MEMBER_BACKEND_INHERITED_ATTR
    from kiro_crew.config.loader_document import member_acp_backend_declared

    main = getattr(config.agent, "acp_backend", "")
    runtime = _backends.resolve_selected_backend(config.agent.member_acp_backend)
    # The loader's own mark when it set one; a config built another way asks the file.
    inherited = getattr(config.agent, MEMBER_BACKEND_INHERITED_ATTR, None)
    legacy = (not inherited) if inherited is not None else member_acp_backend_declared()
    if legacy:
        source = SOURCE_LEGACY
    else:
        _inherited, fell_back = _backends.crewmate_inherited_backend(main)
        source = SOURCE_FALLBACK if fell_back else SOURCE_SETTINGS
    return CrewmateHarnessSource(
        _backends.backend_to_stored(runtime), source, _backends.backend_to_stored(main)
    )


def crewmate_inherited_source(config: Any, crew: str | None) -> CrewmateHarnessSource:
    """What crew *crew* runs on with no pin, and why. Blocking.

    :func:`inherited_harness_source`, unless the harness it inherits cannot carry
    its worker spec: then KAS, with source ``worker_spec``
    (:func:`worker_spec_forces_kas`). Whether *crew* has a pin is not asked, so the
    Crew bot editor can say what Inherited gives while a pin is set.
    """
    inherited = inherited_harness_source(config)
    if crew and worker_spec_forces_kas(config, crew):
        return CrewmateHarnessSource(
            _backends.backend_to_stored(_backends.ACP_BACKEND_KAS),
            SOURCE_WORKER_SPEC,
            inherited.settings_harness,
        )
    return inherited


def crewmate_harness_source(config: Any, crew: str | None) -> CrewmateHarnessSource:
    """:func:`crewmate_inherited_source`, or crew *crew*'s own pin when it has one. Blocking.

    Never raises: a pin that cannot start reads as ``harness=""`` with source
    ``pin``, and the start itself still refuses it (H17).
    """
    pin = crewmate_pin_for(config, crew)
    if pin is None:
        return crewmate_inherited_source(config, crew)
    try:
        harness = _backends.backend_to_stored(pin.effective())
    except CrewmateHarnessUnavailable:
        harness = ""
    main = _backends.backend_to_stored(getattr(config.agent, "acp_backend", ""))
    return CrewmateHarnessSource(harness, SOURCE_PIN, main)
