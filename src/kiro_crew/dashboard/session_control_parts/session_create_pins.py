"""The inherited model-pin check for a session ``session_create`` opens.

Split from :mod:`.session_create` (the file-line cap); that module re-exports
the name, so it stays the import path callers use.
"""

from __future__ import annotations

from kiro_crew import model_scope
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import DEFAULT_MODEL


def _inherited_pin_foreign_to_child(
    model_name: str,
    cfg: KiroCrewConfig,
    child_backend: str,
    *,
    validated_for_worker: bool,
    crew: str = "",
) -> str | None:
    """Why a crewmate's model pin cannot follow its worker onto the child's harness, or ``None``.

    A crewmate's own thread runs on its harness pin when *crew* has one, else
    on ``agent.member_acp_backend``, and its own ``model`` was validated against
    THAT harness's catalog (``_pin_entitlement_backend``). A child that inherits is never a crewmate
    itself: it runs on *child_backend* -- the crewmate's own backend for its
    worker, ``agent.acp_backend`` for any other child, as
    ``select_provider_backend`` picks them -- and an id one harness serves is
    refused on the wire by the other. Two reads, both keyed on
    the child's backend: the two harnesses disagree on model-id namespace (a
    claude or codex member pin cannot ride a kiro worker, and vice versa), or the
    child's namespace has advertised a list the pin is not on while another
    harness claims it (:func:`model_scope.pin_applies`). ``"auto"`` is every
    harness's own word and always passes.

    ``validated_for_worker`` names the profile's explicit ``worker_model``,
    which ``PUT /api/agents/{name}`` validates against the WORKER's harness
    (``mate_worker.crewmate_worker_backend``), so the namespace read does not apply to it -- the
    person pinned it for exactly this harness -- and only the advertised-list
    read remains. Only consulted for an INHERITED pin: an explicit model on the
    call is the caller's own choice and keeps today's guard alone.
    """
    if model_name == DEFAULT_MODEL:
        return None
    from kiro_crew.crewmate_harness import (
        CrewmateHarnessUnavailable,
        crewmate_pin_for,
        pinned_backend,
    )

    child_namespace = capabilities_for(child_backend).model_id_namespace
    try:
        own_backend = pinned_backend(cfg, crew, cfg.agent.member_acp_backend)
    except CrewmateHarnessUnavailable:
        # The crewmate's own harness stopped being startable (a policy narrowed
        # it): the model it chose there is stale, dropped like any stale pin.
        return "the Bot's model pin was chosen for a harness this gateway cannot start"
    member_namespace = capabilities_for(own_backend).model_id_namespace
    if not validated_for_worker and child_namespace != member_namespace:
        pin = crewmate_pin_for(cfg, crew)
        setting = (
            pin.display_stored if pin is not None else (cfg.agent.member_acp_backend or "default")
        )
        return (
            f"the Bot's model pin was chosen for its own harness ({setting}), "
            f"and the worker runs on {child_backend or 'the default backend'}"
        )
    if not model_scope.pin_applies(model_name, child_namespace):
        return "the Bot's model pin is not served by the harness the worker runs on"
    return None
