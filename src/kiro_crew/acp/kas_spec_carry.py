"""What of a kiro-cli agent spec KAS can carry: ``resources`` knowledge bases and ``toolAliases``.

Split out of :mod:`kiro_crew.acp.kas_agents`, which sits at the file-line cap.

* A ``{"type": "knowledgeBase", "source": "file://..."}`` entry in ``resources``
  has a slot on KAS's wire (``KnowledgeBaseResourceSchema`` in KAS's
  ``acp-type-covenant/session/agent-resources.ts``), so a valid one is sent with
  only the schema's keys. One KAS's schema would refuse drops the whole injected
  agent, so every entry is checked here first and a bad one is dropped and named.
* ``toolAliases`` has no slot, so a session runs without it and is told. The pairs
  Crew's own Connections pass wrote are not named: Crew put them there, so the
  user has nothing to fix (:mod:`kiro_crew.connections.alias_record`).
* ``use_aws`` in ``tools`` or ``allowedTools`` names no KAS tool, so the session
  is told AWS calls go through the shell (:func:`use_aws_notice`), and its
  ``toolsSettings`` denies become shell denies (:func:`use_aws_shell_denies`).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew.acp.kas_permissions import merge_user_permissions
from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path

_FILE_SCHEME = "file://"
_KNOWLEDGE_BASE = "knowledgeBase"
_INDEX_TYPES = ("fast", "best")
_STRING_KEYS = ("name", "description")
_LIST_KEYS = ("include", "exclude")


def _source(raw: object) -> str | None:
    """*raw* as a source KAS may index, or ``None``.

    ``~`` and ``$VAR`` are expanded here, as kiro-cli expands them; the engine is
    not relied on to. An absolute source must not be a credential or governance
    location (:func:`~kiro_crew.acp.kas_prompt_paths.is_unsafe_prompt_path`). A
    relative one stays relative, so the engine anchors it to the session's folder
    as kiro-cli does, and may not climb out of it with ``..``.
    """
    if not isinstance(raw, str) or not raw.startswith(_FILE_SCHEME):
        return None
    ref = raw[len(_FILE_SCHEME) :].strip()
    if not ref:
        return None
    if ref.startswith("~") and not (ref == "~" or ref.startswith(("~/", "~\\"))):
        # ``~user``: POSIX raises for an unknown user and Windows guesses a sibling
        # of the home folder, so neither names a folder the user meant.
        return None
    try:
        path = Path(os.path.expandvars(ref)).expanduser()
        # ``anchor``, not ``is_absolute``: on Windows ``/x`` is rooted with no drive.
        if path.anchor and is_unsafe_prompt_path(path.resolve()):
            return None
    except (OSError, RuntimeError):
        # ``~nosuchuser`` cannot be expanded, a symlink loop cannot be resolved.
        return None
    if path.anchor:
        return _FILE_SCHEME + path.as_posix()
    if ".." in PurePosixPath(path.as_posix()).parts:
        return None
    return _FILE_SCHEME + path.as_posix()


def knowledge_base_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    """*raw* as KAS's knowledge-base resource, or ``None`` when it would be refused.

    Only the schema's keys are kept, and each must have the schema's type: an
    unknown key is dropped, and a wrong type rejects the entry.
    """
    if raw.get("type") != _KNOWLEDGE_BASE:
        return None
    source = _source(raw.get("source"))
    if source is None:
        return None
    out: dict[str, Any] = {"type": _KNOWLEDGE_BASE, "source": source}
    for key in _STRING_KEYS:
        if key in raw:
            if not isinstance(raw[key], str):
                return None
            out[key] = raw[key]
    if "indexType" in raw:
        if raw["indexType"] not in _INDEX_TYPES:
            return None
        out["indexType"] = raw["indexType"]
    for key in _LIST_KEYS:
        if key in raw:
            value = raw[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return None
            out[key] = value
    if "autoUpdate" in raw:
        if not isinstance(raw["autoUpdate"], bool):
            return None
        out["autoUpdate"] = raw["autoUpdate"]
    return out


def projected_resources(resources: object) -> tuple[list[Any], list[str]]:
    """The ``resources`` KAS is sent, and the dropped knowledge bases' labels.

    Strings pass as before. A label is ``resources[<index>]``, plus the entry's
    ``name`` when it is a string; it names no path.
    """
    if not isinstance(resources, list):
        return [], []
    kept: list[Any] = []
    dropped: list[str] = []
    for index, entry in enumerate(resources):
        if isinstance(entry, str):
            if entry:
                kept.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        projected = knowledge_base_entry(entry)
        if projected is not None:
            kept.append(projected)
            continue
        name = entry.get("name")
        dropped.append(f"resources[{index}]" + (f" ({name})" if isinstance(name, str) else ""))
    return kept, dropped


def user_tool_aliases(aliases: object) -> bool:
    """Whether *aliases* holds a pair Crew's Connections pass did not write.

    Only a pair the alias record proves Crew wrote, for the map exactly as it is,
    is left out; any other non-empty map counts as the user's.
    """
    if not isinstance(aliases, dict) or not aliases:
        return False
    # circular import: connections reaches the agent layer, which imports this package.
    from kiro_crew.connections.alias_record import (
        is_recorded_emission,
        load_claimed,
        spec_fingerprint,
    )

    claimed = load_claimed(spec_fingerprint(aliases))
    return any(not is_recorded_emission(claimed, ref, alias) for ref, alias in aliases.items())


_AWS_SERVICE_FIRST = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_AWS_SERVICE_CHARS = _AWS_SERVICE_FIRST | {"-"}
_MAX_SERVICE_LENGTH = 128


def _is_aws_service(name: str) -> bool:
    """An AWS CLI service name (``s3``, ``ec2-instance-connect``): lower-case ASCII
    letters, digits and ``-``, not starting with ``-``. Anything else is not turned
    into a shell rule."""
    return (
        bool(name)
        and name[0] in _AWS_SERVICE_FIRST
        and all(ch in _AWS_SERVICE_CHARS for ch in name)
    )


def use_aws_shell_denies(tools_settings: Any) -> dict[str, Any] | None:
    """``toolsSettings.use_aws.deniedServices`` as a KAS ``permissions`` block of shell denies.

    KAS has no AWS tool, so an AWS call is an ``aws`` shell command there. Only
    denies travel: an allow would widen, since kiro-cli allows by an exact
    operation table and a shell rule matches text. The key stays named as not
    carried, because ``aws --profile x <service>`` slips the deny. *tools_settings*
    is canonical (one ``use_aws`` key per kiro-cli alias group).
    """
    settings = tools_settings.get("use_aws") if isinstance(tools_settings, dict) else None
    services = settings.get("deniedServices") if isinstance(settings, dict) else None
    if not isinstance(services, list):
        return None
    match = [
        f"aws {s.strip()} *"
        for s in services
        # An over-long pattern would make KAS's schema refuse the whole block.
        if isinstance(s, str) and len(s) <= _MAX_SERVICE_LENGTH and _is_aws_service(s.strip())
    ]
    return {"rules": [{"capability": "shell", "match": match, "effect": "deny"}]} if match else None


def with_use_aws_denies(
    permissions: dict[str, Any] | None,
    tools_settings: Any,
    agent_id: str,
    audit: Callable[[str, str, str, str], None],
    ceiling_permits: Callable[[str], bool],
) -> dict[str, Any] | None:
    """*permissions* with :func:`use_aws_shell_denies` merged in, each decision audited.

    The caller merges this after its grant readers take their copy of the policy,
    so the denies never withhold the shell grant: KAS refuses the service itself.
    """
    block = use_aws_shell_denies(tools_settings)
    if block is None:
        return permissions
    return merge_user_permissions(
        permissions,
        block,
        ceiling_permits=ceiling_permits,
        audit_decision=lambda refs, outcome, reason: audit(refs, outcome, reason, agent_id),
        agent_id=agent_id,
        source="translated `toolsSettings.use_aws`",
    )


#: kiro-cli's names for its AWS tool in ``tools`` and ``allowedTools``.
_USE_AWS_NAMES = frozenset({"use_aws", "aws", "@builtin/use_aws", "@builtin/aws"})


def use_aws_notice(agent: str, spec: dict[str, Any]) -> str:
    """The session-start line for a spec that lists kiro-cli's ``use_aws``, else ``""``.

    Not mapped to the shell: that would hand an agent allowed only AWS calls
    every command. ``toolsSettings.use_aws`` denies travel as shell denies
    (:func:`use_aws_shell_denies`).
    """
    for key in ("tools", "allowedTools"):
        names = spec.get(key)
        if isinstance(names, list) and any(
            n in _USE_AWS_NAMES for n in names if isinstance(n, str)
        ):
            return (
                f"ℹ Agent {agent} lists use_aws. KAS has no AWS tool, so AWS calls run "
                "as aws commands in the shell. They need the shell tool and follow its "
                "approval rules."
            )
    return ""
