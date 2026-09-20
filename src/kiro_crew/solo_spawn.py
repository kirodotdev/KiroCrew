"""Model-only solo admission: concrete reasons or a different configured worker.

The MCP process knows task count; the gateway compares parent identity. Legacy
bulk_data/fresh_context and named-worker payloads keep their compatibility.
New reasons require an evidence description. These checks validate structure and
runtime capability, never semantic value, independence or user authorization.
Programmatic callers without the solo marker retain the existing API contract.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.model_registry import canonical_key

# The closed vocabulary. ``""`` is "not given", matching the EFFORT_VALUES
# convention so the validator accepts an absent/empty field.
SOLO_SPAWN_REASONS = frozenset(
    {"", "bulk_data", "fresh_context", "parent_parallel", "specialist", "user_requested"}
)

# What each reason licenses, in the words the tool description uses. Read by
# the schema builder so the parameter description and this module agree.
SOLO_SPAWN_REASON_GLOSS: dict[str, str] = {
    "parent_parallel": (
        "the parent retains a separate ready workstream; describe that work and its "
        "non-overlapping ownership in solo_details; asynchronous spawn_run only"
    ),
    "bulk_data": (
        "the step would flood YOUR context with bulk output (a huge log, a "
        "wide search, many large files) and only the distilled result is needed"
    ),
    "fresh_context": (
        "the RESULT would be wrong if the run saw this session's context (a blind "
        "review, a clean-slate repro) -- not for investigation, research or "
        "saving context"
    ),
    "specialist": (
        "a needed capability or independent verification; name the capability, "
        "inputs and verifiable output in solo_details, not merely a different model"
    ),
    "user_requested": (
        "the user explicitly requested delegation; quote that request in solo_details; "
        "this is a model claim, not verified authorization or a permission override"
    ),
}


def delegation_refusal(reason: str, details: str, *, blocking: bool = False) -> str | None:
    """Validate evidence shape, not the claimed value or independence of work.

    Legacy reasons remain valid without details. A blocking tool cannot express
    simultaneous parent work, regardless of how many children it launches.
    """
    if reason == "parent_parallel" and blocking:
        return "Error: parent_parallel needs asynchronous spawn_run; this tool blocks the parent."
    if reason in {"parent_parallel", "specialist", "user_requested"} and not details.strip():
        return f"Error: {reason} requires concrete solo_details. Do the work directly otherwise."
    return None


def parent_work_supported(state: Any, parent_session: str) -> bool:
    """Only dashboard-owned turns have the verified busy-turn completion queue.

    Channel-only, nested and background callers retain their yield boundary.
    A channel linked to a dashboard slot uses the same queue as dashboard chat.
    """
    if not parent_session or parent_session.startswith(("subagent:", "cron:", "hook:")):
        return False
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slots = getattr(state, "_slots", None)
    return isinstance(slots, dict) and any(
        effective_session_key(slot) == parent_session for slot in slots.values()
    )


# Error code the gateway returns when the roster check refuses a solo spawn.
SOLO_SPAWN_REFUSED_CODE = "solo_spawn_unjustified"

# Sentinels a dashboard slot stores for "no model chosen"; neither is a model.
_UNKNOWN_MODEL = frozenset({"", "auto"})


def solo_spawn_question(*, tool: str = "spawn_run") -> str:
    """The refusal names the decision, never the passing token; the enum lives
    only in the schema.

    Shared by the tool-side refusal and the gateway's roster refusal so the
    caller reads ONE wording wherever the gate fired. Prefixed ``Error:`` for
    the SEL / caller convention that a spawn result which started nothing opens
    with that word.
    """
    if tool == "spawn_sub_agents":
        differs = "an agent_or_mode that differs from your own"
    else:
        differs = "a model, agent or crew that differs from your own"
    return (
        "Error: solo spawn refused -- one task, no solo_reason, and nothing "
        "that differs from this session. Can you do this task yourself, here, "
        "now? If yes, do it. Delegate only for concrete value. If not, call again "
        "with a solo_reason you can defend -- the closed "
        "list and what each reason licenses are in this tool's solo_reason "
        f"parameter description -- or name {differs}. Nothing was spawned."
    )


def solo_spawn_refusal(
    task_count: int,
    solo_reason: str,
    *,
    model: str = "",
    agent: str = "",
    crew: str = "",
    tool: str = "spawn_run",
) -> str | None:
    """Tool-side gate: the refusal text, or ``None`` when the spawn may proceed.

    Refuses exactly the call that is one task, gives no reason, and names no
    model / agent / crew at all. A NAMED one passes here because this process
    cannot tell it from the caller's own; :func:`solo_spawn_difference` on
    the gateway can, and does.
    """
    if task_count != 1:
        return None
    if solo_reason:
        return None
    if _named_model(model) or agent or crew:
        return None
    return solo_spawn_question(tool=tool)


def solo_spawn_note(solo_reason: str) -> str:
    """The solo-spawn result line: the reason, or a pointer to the gateway audit.

    Only the process that computed the ground prints it; this tool process
    cannot know which named value the gateway found to differ.
    """
    if solo_reason:
        return f"Solo spawn -- reason: {solo_reason}."
    return (
        "Solo spawn -- not refused: the gateway's roster check did not find "
        "everything it names to be your own; the ground is in the spawn.solo audit."
    )


def _named_model(model: str) -> str:
    """*model* as a NAMED model, or ``""``: the ``"auto"`` sentinel (and empty)
    mean "no model chosen" and must not count as naming one, or a lone task
    could pass the gate with ``model="auto"`` and no justification at all."""
    return "" if not isinstance(model, str) or model.strip().lower() in _UNKNOWN_MODEL else model


def _canonical_model(name: str) -> str:
    """Registry canonical for *name*, else the lower-cased name itself."""
    return canonical_key(name) or name.strip().lower()


def parent_slot_model(state: Any, parent_session: str) -> str:
    """The model a dashboard parent slot is pinned to, or ``""`` when unknown.

    Match the slot's effective session key, including channel-linked slots.
    An absent slot, an empty model and the ``"auto"`` sentinel are "unknown".
    """
    slots = getattr(state, "_slots", None) or {}
    try:
        # circular import: chat_utils imports dashboard.state, which reaches
        # validation.py, which imports this module.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = next(
            (slot for slot in slots.values() if effective_session_key(slot) == parent_session),
            None,
        )
    except Exception:  # noqa: BLE001 - identity check is best-effort
        return ""
    if slot is None:
        return ""
    model = getattr(slot, "model", "") or ""
    return "" if not isinstance(model, str) or model in _UNKNOWN_MODEL else model


def solo_spawn_difference(
    state: Any,
    parent_session: str,
    *,
    agent: str = "",
    model: str = "",
    crew: str = "",
) -> str:
    """Gateway roster check: on what ground is the requested agent / model /
    crew NOT the parent's own?

    Returns the ground -- ``"crew"``, ``"agent"`` or ``"model"``, suffixed with
    ``" (parent unknown)"`` when the parent fact could not be compared and the
    check fails OPEN -- so the gateway can audit WHY a lone spawn was let
    through. Returns ``""`` only when every value the caller named is the
    parent's own, which is the case the tool-side check cannot see and the
    reason this half exists.

    ``agent`` is a provider template id and is compared against the parent's
    RESOLVED template (``sessions.get_agent``), never against its member
    alias: a member session ``coder`` running on ``kirocrew-worker`` that names
    ``kirocrew-worker`` has named its own agent. The member alias is what
    ``crew`` compares against.
    """
    kind, own = "", ""
    own_agent = ""
    model = _named_model(model)
    if parent_session:
        try:
            sel = state.sessions.get_agent_selection(parent_session)
            if isinstance(sel, tuple) and len(sel) == 2:
                kind, own = str(sel[0]), str(sel[1] or "")
        except Exception:  # noqa: BLE001 - unknown parent, compare nothing
            kind, own = "", ""
        try:
            got = state.sessions.get_agent(parent_session)
            own_agent = got if isinstance(got, str) else ""
        except Exception:  # noqa: BLE001 - unknown parent, compare nothing
            own_agent = ""
    if crew:
        # A crew is another member's memory silo -- unless it is the parent's
        # own: a private member is REQUIRED to name itself to delegate, and a
        # plain template session already runs on the ``default`` crew's store.
        own_crew = (kind == "member" and own and crew == own) or (
            kind == "template" and crew == "default"
        )
        if not own_crew:
            return "crew" if kind else "crew (parent unknown)"
    if agent:
        if not own_agent:
            return "agent (parent unknown)"
        if agent != own_agent:
            return "agent"
    if model:
        parent_model = parent_slot_model(state, parent_session)
        if not parent_model:
            return "model (parent unknown)"
        if _canonical_model(model) != _canonical_model(parent_model):
            return "model"
    return ""
