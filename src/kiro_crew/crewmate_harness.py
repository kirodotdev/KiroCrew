"""A crewmate's own harness pin: ``agents.<name>.acp_backend``.

A crewmate can name the agent harness its own chat and the worker sessions it opens
run on, beside the model it already pins. The value is spelled as
``agent.acp_backend`` is in ``config.json`` (``kiro``, ``kas``, ``claude``, ...);
``""`` pins nothing and the crewmate inherits ``agent.member_acp_backend`` exactly
as before the field existed.

The pin is one input to the backend-selection gate
(``members.select_provider_backend``), never a second gate. What this module adds
is how a session finds its crewmate -- from the session key alone, so every reader
of the gate answers the same way with no file read -- and the one rule the pin does
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

from dataclasses import dataclass, field
from typing import Any

from kiro_crew.agent_sdk import backends as _backends

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
        """This pin, confirmed startable, for the selection gate; raises when it is not."""
        return StartableCrewPin(self.crew, self.stored, self.resolve())


@dataclass(frozen=True)
class StartableCrewPin:
    """A crewmate's harness pin the pre-gate check (H17) confirmed this gateway can start.

    The only pin ``members.select_provider_backend`` takes. *backend* is the runtime
    id the pin resolved to; the gate returns it as is and has no refusal of its own.
    """

    crew: str
    stored: str
    backend: str


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


def crewmate_backend_pin(config: Any, session_key: str | None) -> StartableCrewPin | None:
    """The pre-gate check (H17): *session_key*'s crewmate pin, confirmed startable.

    What every caller of ``members.select_provider_backend`` passes as its
    ``crew_backend``, evaluated in that call's own arguments, so the check runs
    before the gate does. ``None`` when the session pins nothing. A pin this
    gateway cannot start raises :class:`CrewmateHarnessUnavailable` HERE, so the
    crewmate's session is refused before the gate and never falls back to another
    harness, while the gate keeps its never-raises contract (H3).
    """
    pin = crewmate_stored_pin(config, session_key)
    return pin.checked() if pin is not None else None


def pinned_backend(config: Any, crew: str | None, fallback: str) -> str:
    """The runtime backend crew *crew*'s sessions run on: its pin, else *fallback*.

    Raises :class:`CrewmateHarnessUnavailable` for a pin that cannot start: the
    same pre-gate check (H17) :func:`crewmate_backend_pin` runs.
    """
    pin = crewmate_pin_for(config, crew)
    return pin.checked().backend if pin is not None else fallback


def selectable_harnesses() -> list[str]:
    """Every harness a crewmate may be pinned to, in the stored spelling, sorted."""
    return sorted(_backends.backend_to_stored(b) for b in _backends.selectable_backends())


def worker_harnesses() -> list[str]:
    """The selectable harnesses a crewmate's worker can run on, stored spelling, sorted."""
    return sorted(
        _backends.backend_to_stored(b)
        for b in _backends.selectable_backends()
        if b in _backends.ACP_BACKENDS_MATE_WORKER_KIT
    )


def harness_pin_rejected(value: object) -> str | None:
    """Why *value* cannot be stored as a crewmate's harness pin, or ``None``.

    ``""`` clears the pin. Anything else must be a harness this gateway can
    start right now, so a save can never store a pin that would refuse every
    session.
    """
    if not isinstance(value, str):
        return "acp_backend must be a string"
    stored = value.strip()
    if not stored or stored in selectable_harnesses():
        return None
    return "acp_backend must be one of: " + ", ".join(["(empty)", *selectable_harnesses()])


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
    pin = (
        crewmate_pin_for(config, crew)
        if acp_backend is None
        else (
            CrewBackendPin(crew, acp_backend.strip(), _kas_bundle(config))
            if acp_backend.strip()
            else None
        )
    )
    if pin is not None:
        return pin.resolve()
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


def kiro_spelling_applies(backend: str | None, harness_pinned: bool) -> bool:
    """Whether a model pin's kiro-cli spelling check applies on *backend*.

    Only a harness that reads kiro-cli's ids has a "wrong" spelling. A crewmate
    *harness_pinned* to another harness (claude, codex, ...) takes its own wire
    ids as written; every unpinned crew keeps the check exactly as before.
    """
    from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_for

    if not harness_pinned or backend is None:
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
    harness names its own route: the bundle only for KAS with ``agent.kas_bundle``
    on, whatever the installation runs. ``None`` for a harness that is not
    selectable. Blocking (the bundle predicate reads config).
    """
    asked = query["backend"] if "backend" in query else None
    if asked is None:
        from kiro_crew.agent_sdk import kas_bundle

        return getattr(config.agent, "acp_backend", ""), kas_bundle.kas_bundle_route()
    backend = harness_for_query(asked)
    if backend is None:
        return None
    on_bundle = backend == _backends.ACP_BACKEND_KAS and bool(
        getattr(config.agent, "kas_bundle", False)
    )
    return backend, on_bundle
