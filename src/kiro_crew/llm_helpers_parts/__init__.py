"""The owner modules :mod:`kiro_crew.llm_helpers` is composed from.

``kiro_crew.llm_helpers`` is the shared LLM turn helpers' import path and their patch
surface: callers import its names and tests patch them there. Each module here owns one
responsibility; the facade resolves reads lowest layer first (``_OWNERS``), and a module
imports only the ones before it, never the facade:

* ``backend_errors`` -- which backend failures a turn retries, and how long it waits;
* ``turn_usage`` -- what a turn cost, which backend served it, the interaction event;
* ``model_fallback`` -- the fallback chain and switching a live session's model;
* ``tool_input_denials`` -- the deny checks a tool call's title and input run through;
* ``tool_approval`` -- the approval policies and resolving a permission request;
* ``turn_results`` -- parsing JSON out of model text, saving a turn to history;
* ``turn_stream`` -- running one prompt to its answer (``stream_and_collect``);
* ``background_turns`` -- unattended one-shot and background turns.

What an owner reads from the ACP layer comes from
:mod:`kiro_crew.agent_sdk.drivers.acp_turn_vocab`, so no owner names ``kiro_crew.acp``
or ``kiro_crew.providers`` (``scripts/check_agent_sdk_boundary.py``). The facade serves
every name these modules define, and its mirror (:mod:`kiro_crew._facade_mirror`) carries
a patch of ``kiro_crew.llm_helpers.<name>`` to every module that binds it, so the moved
code reads its own globals unchanged.
"""
