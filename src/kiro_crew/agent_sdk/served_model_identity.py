"""Which harnesses need Kiro Crew to tell the model which model serves the session.

A model asked "what model are you?" can only answer from something in its context;
without a fact there it answers from training priors, confidently and wrongly.
kiro-cli's own system prompt states the fact ("The current model is <id>."). goose's
does not -- its ``crates/goose/src/prompts/system.md`` names the agent and its
extensions and never the model -- so without this a goose session names a model
and runtime from training priors while the dashboard's model label, read from the
same session, is right.

So Crew states the id the session reports serving (``served_model``) for exactly
the harnesses named here. Naming is opt-in (harness-parity H6): a harness joins
once its own prompt is shown to state no model, so none receives the fact twice.

Importing it costs only the stdlib, so the context builder can use it without
loading the ACP package; the one ACP-layer read (the label constant) happens when
:func:`crew_states_served_model` is called, as :mod:`kas_tool_names` does for KAS.
"""

from __future__ import annotations

__all__ = ["crew_states_served_model"]


def crew_states_served_model(provider_type: str | None) -> bool:
    """Whether Crew's context must name the model serving a *provider_type* session.

    *provider_type* is the session's provider label (``context_provider_type``). A
    positive comparison against the label of each harness whose own system prompt
    states no model (harness-parity H5); another such harness joins here with its
    own label. The label table lives in the ACP layer and is read here, behind the
    agent-SDK boundary, only when asked.
    """
    if not provider_type:
        return False
    from kiro_crew.acp.types import PROVIDER_LABEL_GOOSE

    return provider_type == PROVIDER_LABEL_GOOSE
