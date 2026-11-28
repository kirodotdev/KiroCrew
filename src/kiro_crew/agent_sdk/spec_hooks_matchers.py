"""Spec hooks Crew leaves out because it cannot read their matcher.

A tool-event spec hook whose matcher is outside the kinds Crew's conversion reads
(:func:`kiro_crew.agent_sdk.spec_hooks._matcher_ok`) is dropped whole: running it
with no matcher would widen it to every tool. The object form drops it in
``spec_hooks._hook``, a hook document earlier in ``normalize_spec_hooks``. Both
write a gateway warning and an SEL row; :func:`invalid_matcher_spec_hooks` names
the same hooks for the session-start notice, so the drop is not silent in chat.

This lives here, not in :mod:`kiro_crew.agent_sdk.spec_hooks`, only to keep that
module under its line cap. Its names are imported lazily, so this module has no
import-time dependency on its parent.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple


class InvalidMatcherHook(NamedTuple):
    """A spec hook left out because Crew cannot read its matcher.

    ``event`` is the event or trigger as the spec spells it, ``matcher`` the matcher
    as written (a value that is not a string is shown as its JSON).
    """

    event: str
    matcher: str


def _shown(matcher: object) -> str:
    if isinstance(matcher, str):
        return matcher
    return json.dumps(matcher, ensure_ascii=False, default=str)


def _any_matcher(_matcher: object) -> bool:
    return True


def _invalid_matchers(spec: dict[str, Any]) -> list[InvalidMatcherHook]:
    """The spec's hooks that are left out only because of their matcher.

    Reads either shape, in the order the conversion reads it. A hook its conversion
    drops for another reason first (an unknown event, no usable or a too-long
    command, a document that fails another check, is disabled or asks to be
    confirmed) is left out, and so is every entry past the first ``_MAX_SPEC_HOOKS``
    hooks that run, which the conversion does not read. The notice never blames the
    matcher for a hook that a readable matcher would not make run.
    """
    # circular import: agent imports hooks, which spec_hooks imports at load time.
    from kiro_crew.agent import (
        _MAX_SPEC_HOOK_DOCUMENTS,
        _event_for_hook_trigger,
        normalize_spec_hooks,
    )
    from kiro_crew.agent_materialization.kiro_hooks import _document_audit_muted
    from kiro_crew.agent_sdk.spec_hooks import (
        _MAX_COMMAND_LEN,
        _MAX_SPEC_HOOKS,
        _OBJECT_EVENT_TO_HOOK_EVENT,
        _TOOL_EVENTS,
        _matcher_ok,
    )

    def _command_ok(command: object) -> bool:
        # ``_hook`` rejects these for their command before it reads the matcher.
        return (
            isinstance(command, str) and bool(command.strip()) and len(command) <= _MAX_COMMAND_LEN
        )

    def _document_runs(doc: dict) -> bool:
        # ``_from_documents``'s checks on a normalized document, matcher aside.
        action = doc.get("action")
        return (
            doc.get("enabled") is not False
            and doc.get("confirm") is not True
            and isinstance(action, dict)
            and action.get("type") == "command"
            and _OBJECT_EVENT_TO_HOOK_EVENT.get(_event_for_hook_trigger(doc.get("trigger")) or "")
            is not None
            and _command_ok(action.get("command"))
        )

    value = spec.get("hooks")
    found: list[InvalidMatcherHook] = []
    kept = 0
    if isinstance(value, dict):
        for key, entries in value.items():
            event = _OBJECT_EVENT_TO_HOOK_EVENT.get(key) if isinstance(key, str) else None
            if event is None or not isinstance(entries, list):
                continue
            for entry in entries:
                if kept >= _MAX_SPEC_HOOKS:
                    return found
                if not isinstance(entry, dict) or not _command_ok(entry.get("command")):
                    continue
                # ``_hook`` reads a matcher on a tool event only.
                matcher = entry.get("matcher") if event in _TOOL_EVENTS else None
                if matcher is not None and not _matcher_ok(matcher):
                    found.append(InvalidMatcherHook(str(key), _shown(matcher)))
                else:
                    kept += 1
    elif isinstance(value, list):
        # The normalizer itself, once with Crew's matcher rule and once with none:
        # a document only the first drops is dropped for its matcher alone. Muted,
        # as the Hooks page's read is, since the load already warned and audited.
        token = _document_audit_muted.set(True)
        try:
            for doc in value[:_MAX_SPEC_HOOK_DOCUMENTS]:
                if kept >= _MAX_SPEC_HOOKS:
                    break
                if not isinstance(doc, dict):
                    continue
                strict = normalize_spec_hooks([doc], matcher_ok=_matcher_ok)
                if strict:
                    kept += _document_runs(strict[0])
                    continue
                if doc.get("matcher") is None:
                    continue
                loose = normalize_spec_hooks([doc], matcher_ok=_any_matcher)
                if loose and _document_runs(loose[0]):
                    found.append(
                        InvalidMatcherHook(str(doc.get("trigger")), _shown(doc["matcher"]))
                    )
        finally:
            _document_audit_muted.reset(token)
    return found


def invalid_matcher_spec_hooks(
    agent_id: str,
    project_dir: str | None = None,
    *,
    session_key: str = "",
    kas_checkout: str | None = None,
) -> list[InvalidMatcherHook]:
    """The active spec's hooks that do not run because Crew cannot read their matcher.

    For the session-start notice. Reads the spec
    :func:`~kiro_crew.agent_sdk.spec_hooks.overlong_spec_hook_commands` reads,
    *kas_checkout* included; raises when it cannot be read.
    """
    # circular import: see _invalid_matchers.
    from kiro_crew.agent_sdk.spec_hooks import _agent_spec

    return _invalid_matchers(
        _agent_spec(agent_id, project_dir, session_key, kas_checkout=kas_checkout)
    )
