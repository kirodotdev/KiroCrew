"""The spec ``hooks`` field: what kiro-cli is handed, and from which sources.

A spec's hooks arrive in two shapes -- Crew's object of event -> ``[{command,
matcher?}]`` and KAS's array of hook documents -- plus the executable scripts
autoimport finds under ``~/.kiro/hooks``. :func:`_apply_user_kiro_hooks` folds all of
them into the bundled security hooks in ONE :func:`_merge_kiro_hooks` pass, so the
per-event and total caps and the explicit-wins dedup hold across every source, and
every rejection is SEL-audited as the permission decision it is. The audits and the
redacted log rendering go through :mod:`kiro_crew.agent`'s SEL helpers, which is
where the security-posture registry classifies those redaction calls.

The event vocabulary lives here too: :data:`_VALID_HOOK_EVENTS` is the closed set
kiro-cli loads (a spec carrying any other event key does not load at all). The
repair of the one legacy key Kiro Crew once serialized into the specs it owns stays
:func:`kiro_crew.agent.repair_agent_configs`. The bundled defaults and the default
hooks directory are read from :mod:`kiro_crew.agent`, where the test suite's
isolation pins them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from kiro_crew import agent as agent_mod
from kiro_crew import platform_compat
from kiro_crew.hooks import is_unc_shape, unc_probe_allowed

# Allowlist for hook-command paths (config.json is LLM-writable, so this guards
# against indirect command injection). The intent is to reject shell
# metacharacters (; | & $ ` spaces quotes ( ) etc.) — the path is later exec'd as
# an argv element, never through a shell. On Windows an absolute path is
# `D:\Users\...`, so backslash and the drive-letter colon MUST be allowed there or
# EVERY Windows hook path is rejected (autoimport silently loads nothing). `\` and
# `:` are not shell-injection vectors for an argv path, and the is_sensitive_path
# + absolute-path + resolve() checks below still apply. POSIX keeps the original,
# tighter allowlist (no backslash/colon).
if platform_compat.IS_WINDOWS:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9/_.\-\\:]+$")
else:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9/_.\-]+$")
_SAFE_MATCHER_RE = re.compile(r"^[a-zA-Z0-9_.*\-]+$")
_MAX_MATCHER_LEN = 200


def _validate_hook_command(command: str, event: str) -> str | None:
    """Validate a user-supplied hook command path.

    Returns the resolved absolute path if safe, or None on failure.
    Since config.json is LLM-writable, this guards against indirect
    command injection.  Uses an allowlist regex for path characters.
    """
    # A rejected command is quoted in each warning below, and the value came out
    # of an LLM-writable config, so it can carry a credential. ``gateway.log``
    # persists and rotates rather than expires, so every one of them is redacted
    # first — the same rule the SEL writer applies on its own side.
    if not _SAFE_PATH_RE.match(command):
        agent_mod.logger.warning(
            "kiro_hooks[%s]: command contains disallowed characters: %s",
            event,
            agent_mod._hook_diagnostic(command),
        )
        return None
    if not os.path.isabs(command):
        agent_mod.logger.warning(
            "kiro_hooks[%s]: command must be absolute path, got %s",
            event,
            agent_mod._hook_diagnostic(command),
        )
        return None
    # ``resolve`` raises on a symlink loop, and on Python 3.12 — this project's
    # floor — that is a ``RuntimeError`` rather than an ``OSError``. The command
    # comes from an LLM-writable config and this validator runs inside the
    # agent-install pass, whose documented fallback re-enters the same call, so an
    # unguarded raise takes the whole rebuild down. A path that cannot be resolved
    # is simply not a usable hook command.
    try:
        resolved = str(Path(command).resolve())
    except (OSError, ValueError, RuntimeError):
        agent_mod.logger.warning(
            "kiro_hooks[%s]: command cannot be resolved: %s",
            event,
            agent_mod._hook_diagnostic(command),
        )
        return None
    if not _SAFE_PATH_RE.match(resolved):
        agent_mod.logger.warning(
            "kiro_hooks[%s]: resolved path contains disallowed characters: %s",
            event,
            agent_mod._hook_diagnostic(resolved),
        )
        return None
    if agent_mod.is_sensitive_path(resolved):
        agent_mod.logger.warning(
            "kiro_hooks[%s]: command points to sensitive path %s, skipping",
            event,
            agent_mod._hook_diagnostic(command),
        )
        return None
    if not os.path.isfile(resolved):
        agent_mod.logger.warning(
            "kiro_hooks[%s]: command not found: %s", event, agent_mod._hook_diagnostic(command)
        )
        return None
    return resolved


# Kiro Crew-internal hook keys that must NOT appear in generated kiro-cli agent  # brand-ok
# specs (kiro-cli rejects unknown keys). Excluded when deriving _VALID_HOOK_EVENTS
# from bundled defaults below, so an internal key never round-trips as an event.
_INTERNAL_HOOK_KEYS = frozenset(
    {"auto_approve_tools", "auto_deny_tools", "auto_replies", "transforms"}
)

# Valid kiro-cli hook event names — the UNION of the hardcoded baseline (kiro-cli's
# known schema) and any event key present in bundled defaults. Used for generated
# specs and user-input validation; startup repair is ownership-scoped and removes
# only legacy keys Kiro Crew serialized. A new event added to defaults.json is
# automatically accepted without a matching allowlist update.
_VALID_HOOK_EVENTS = frozenset(
    {"preToolUse", "postToolUse", "userPromptSubmit", "agentSpawn", "stop"}
) | frozenset(
    k
    for k in (agent_mod._load_json(agent_mod._BUNDLED_CFG_DIR / "defaults.json") or {}).get(
        "hooks", {}
    )
    if k not in _INTERNAL_HOOK_KEYS
)

# Hook triggers a Kiro Agent session owns, in the camelCase spelling that side
# uses. They are authorable in Kiro Crew (``hooks.HOOK_EVENTS_KAS_ONLY`` carries
# the PascalCase twin the hook store persists) and they are deliberately NOT in
# ``_VALID_HOOK_EVENTS``, because kiro-cli's ``hooks`` map is a CLOSED enum: a
# spec carrying one of these keys does not load at all. Measured against
# kiro-cli 2.23.1, ``agent validate`` answers "data did not match any variant of
# untagged enum Repr" and ``agent list`` refuses the same file, while an unknown
# TOP-LEVEL key and an unknown hook-entry field are both accepted and ignored.
# So the closed set is the event map specifically, and one of these names
# reaching a generated spec would cost the user their whole default agent --
# which is why ``_merge_kiro_hooks`` names them as a distinct refusal below
# rather than letting them read as a typo.
_CREW_ONLY_HOOK_EVENTS = frozenset(
    {
        "preTaskExecution",
        "postTaskExecution",
        "fileCreated",
        "fileEdited",
        "fileDeleted",
        "userTriggered",
    }
)

# Repair is subtractive against the runtime-only key Kiro Crew is known to have
# serialized into its generated specs. Unknown keys may belong to a newer
# kiro-cli schema or to the user.
_LEGACY_KIROCREW_HOOK_KEYS = frozenset({"auto_approve_tools"})


def _kiro_hooks_only(hooks: dict) -> dict:
    """Return only kiro-cli valid hook keys, stripping everything else.

    Used on the generation path (trusted bundled defaults) and for user-supplied
    config validation. On-disk startup repair is deliberately narrower because
    unknown keys may belong to a newer kiro-cli schema or to the user.
    """
    return {k: v for k, v in hooks.items() if k in _VALID_HOOK_EVENTS}


_MAX_USER_HOOKS_PER_EVENT = 10
_MAX_TOTAL_USER_HOOKS = 20

# kiro-cli documents hook events in PascalCase (PreToolUse, PostToolUse, ...).
# The agent config stores them in camelCase (preToolUse, ...).  Script headers
# ("# event: PreToolUse") use kiro-cli's PascalCase convention; this map
# normalizes both casings back to the canonical camelCase form.
#
# It spans the WHOLE authorable vocabulary, kiro-cli's five and the six a Kiro
# Agent session owns, so a recognised name is never reported as unknown. That is
# safe because recognising a name is not emitting it: every autoimported entry
# goes through ``_merge_kiro_hooks``, whose ``_VALID_HOOK_EVENTS`` gate is the one
# place that decides what reaches the generated spec, and it drops the six there
# with their own reason.
_HOOK_EVENT_CANONICAL = {
    "pretooluse": "preToolUse",
    "posttooluse": "postToolUse",
    "userpromptsubmit": "userPromptSubmit",
    "agentspawn": "agentSpawn",
    "stop": "stop",
    "pretaskexecution": "preTaskExecution",
    "posttaskexecution": "postTaskExecution",
    "filecreated": "fileCreated",
    "fileedited": "fileEdited",
    "filedeleted": "fileDeleted",
    "usertriggered": "userTriggered",
}


# A spec's ``hooks`` field accepts two shapes, and each has exactly one reader.
# Crew's own shape is an object keyed by kiro-cli event name, each value a list of
# ``{command, matcher?}`` entries; it is read by ``_merge_kiro_hooks``, which owns
# every rule about a command, a matcher, dedup and the caps. KAS (kiro-agent)
# writes a list of hook documents: ``{name, description?, trigger, matcher?,
# action, timeout?, enabled?, confirm?}``; the ARRAY form normalizes to the
# document list and is then projected onto the object form kiro-cli is handed.
# ``normalize_spec_hooks`` reads the array only, so routing an object form through
# it rejects every entry. The standalone hook FILE wrapper
# ``{"version": "v1", "hooks": [...]}`` is not a spec shape: it is an object, so
# the merge sees ``version``/``hooks`` as unknown event names and rejects it.
#
# Canonical KAS trigger for every spelling a spec may carry, transcribed from
# kiro-agent's own alias table: ``packages/kiro-agent/src/hooks/trigger-names.ts``
# at blob ``2d4a3127e32e5e81e68d5c2ea406a6a5728f6d78``, which is the version this
# table is verified against and the one to re-read when adding a name. It holds
# twelve canonical triggers with their identity rows, the IDE's legacy camelCase
# spellings, the CLI aliases, and one Open Plugins legacy alias.
#
# One deliberate difference: kiro-agent matches the spelling exactly, while the
# keys here are lowercased so a spec's casing does not matter — the same leniency
# ``_HOOK_EVENT_CANONICAL`` applies to a script header. Crew therefore accepts
# every spelling kiro-agent does, plus casings of them.
_KAS_TRIGGER_CANONICAL = {
    # KAS canonical PascalCase, one row per trigger
    "sessionstart": "SessionStart",
    "sessionend": "SessionEnd",
    "stop": "Stop",
    "pretooluse": "PreToolUse",
    "posttooluse": "PostToolUse",
    "pretaskexec": "PreTaskExec",
    "posttaskexec": "PostTaskExec",
    "userpromptsubmit": "UserPromptSubmit",
    "postfilecreate": "PostFileCreate",
    "postfilesave": "PostFileSave",
    "postfiledelete": "PostFileDelete",
    "manual": "Manual",
    # IDE legacy camelCase, as a .kiro.hook ``when.type`` emits it
    "agentstop": "Stop",
    "promptsubmit": "UserPromptSubmit",
    "pretaskexecution": "PreTaskExec",
    "posttaskexecution": "PostTaskExec",
    "fileedited": "PostFileSave",
    "filecreated": "PostFileCreate",
    "filedeleted": "PostFileDelete",
    "usertriggered": "Manual",
    # CLI aliases, as an inline agent-profile hook spells them
    "agentspawn": "SessionStart",
    # Open Plugins legacy alias
    "afterfileedit": "PostFileSave",
}

# The KAS triggers a kiro-cli hook event can express. The other seven
# (``SessionEnd``, ``PreTaskExec``, ``PostTaskExec``, ``PostFileCreate``,
# ``PostFileSave``, ``PostFileDelete``, ``Manual``) have no kiro-cli event name, so
# a document carrying one stays in Crew's stored spec and is left out of the
# kiro-cli emission.
_KAS_TRIGGER_TO_EVENT = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "UserPromptSubmit": "userPromptSubmit",
    "SessionStart": "agentSpawn",
    "Stop": "stop",
}

# Action types in a KAS hook document. Only ``command`` is expressible as a
# kiro-cli hook entry; an ``agent`` action (a prompt handed back to the model)
# has no kiro-cli equivalent and is dropped on the emission path only.
_KAS_ACTION_TYPES = frozenset({"command", "agent"})

# SEL event tag for a document-level rejection. The spec field is the only
# surface these helpers read, so the tag is named once here rather than threaded
# through every helper as an argument with one value.
_HOOK_SPEC_AUDIT_TAG = "kiro_hooks"

# Bound on the documents taken from one ``hooks`` field. ``_merge_kiro_hooks``
# caps what reaches kiro-cli; this caps the work done to get there, so a spec
# carrying a huge list cannot spend the whole install pass on it.
_MAX_SPEC_HOOK_DOCUMENTS = 200

# A document count alone bounds nothing if each document may carry strings of
# any size, so every string a document RETAINS carries its own limit. The
# command still has to resolve to an existing absolute path, and the matcher
# still has :data:`_MAX_MATCHER_LEN`; these cover the fields those checks do not
# reach.
_MAX_HOOK_NAME_LEN = 200
_MAX_HOOK_DESCRIPTION_LEN = 1000
_MAX_HOOK_PAYLOAD_LEN = 4096

# Per-document fields whose type is checked before the document is accepted.
_KAS_DOCUMENT_FIELD_TYPES: tuple[tuple[str, type | tuple[type, ...]], ...] = (
    ("name", str),
    ("description", str),
    ("enabled", bool),
    ("confirm", bool),
)

# Per-document string fields and the length each one is held to.
_KAS_DOCUMENT_FIELD_LIMITS: tuple[tuple[str, int], ...] = (
    ("name", _MAX_HOOK_NAME_LEN),
    ("description", _MAX_HOOK_DESCRIPTION_LEN),
)


def _hook_matcher_ok(matcher: object) -> bool:
    """Whether a present ``matcher`` passes the object form's rules.

    Same three rules ``_merge_kiro_hooks`` applies to an object-form entry: a
    string, within :data:`_MAX_MATCHER_LEN`, and inside
    :data:`_SAFE_MATCHER_RE`.
    """
    return (
        isinstance(matcher, str)
        and len(matcher) <= _MAX_MATCHER_LEN
        and bool(_SAFE_MATCHER_RE.match(matcher))
    )


def _event_for_hook_trigger(trigger: object) -> str | None:
    """kiro-cli event name a document's trigger can be emitted as, or None."""
    if not isinstance(trigger, str):
        return None
    return _KAS_TRIGGER_TO_EVENT.get(trigger)


def _hook_document_action(action: object, *, index: int) -> dict | None:
    """Validate a document's ``action``, returning the normalized copy or None."""
    if not isinstance(action, dict):
        agent_mod.logger.warning("kiro_hooks[%d]: action is not an object, skipping", index)
        agent_mod._sel_hook_rejected(_HOOK_SPEC_AUDIT_TAG, str(action), "action is not an object")
        return None
    action_type = action.get("type")
    # The membership test runs against a frozenset, so an unhashable value
    # (a list, a dict) would raise out of a normalizer whose contract is to
    # warn and skip. Screen the type first.
    if not isinstance(action_type, str) or action_type not in _KAS_ACTION_TYPES:
        agent_mod.logger.warning(
            "kiro_hooks[%d]: unknown action type %s, skipping",
            index,
            agent_mod._hook_diagnostic(action_type),
        )
        agent_mod._sel_hook_rejected(_HOOK_SPEC_AUDIT_TAG, str(action_type), "unknown action type")
        return None
    payload_key = "command" if action_type == "command" else "prompt"
    payload = action.get(payload_key)
    if not isinstance(payload, str) or not payload:
        agent_mod.logger.warning(
            "kiro_hooks[%d]: %s action needs a non-empty %s, skipping",
            index,
            action_type,
            payload_key,
        )
        agent_mod._sel_hook_rejected(
            _HOOK_SPEC_AUDIT_TAG, str(payload), f"{action_type} action without {payload_key}"
        )
        return None
    if len(payload) > _MAX_HOOK_PAYLOAD_LEN:
        agent_mod.logger.warning(
            "kiro_hooks[%d]: %s is longer than %d characters, skipping",
            index,
            payload_key,
            _MAX_HOOK_PAYLOAD_LEN,
        )
        agent_mod._sel_hook_rejected(_HOOK_SPEC_AUDIT_TAG, payload, f"{payload_key} too long")
        return None
    return {"type": action_type, payload_key: payload}


def _hook_document_from_document(entry: object, *, index: int) -> dict | None:
    """Validate one KAS hook document, returning the normalized copy or None."""
    if not isinstance(entry, dict):
        agent_mod.logger.warning("kiro_hooks[%d]: hook document is not an object, skipping", index)
        agent_mod._sel_hook_rejected(
            _HOOK_SPEC_AUDIT_TAG, str(entry), "hook document is not an object"
        )
        return None
    # ``"matcher": null`` is how JSON spells an optional the author left out, so
    # a null reads as absent. Without this it is a present value of the wrong
    # type, and the whole document is lost over a field that says nothing.
    entry = {key: value for key, value in entry.items() if value is not None}
    trigger_raw = entry.get("trigger")
    trigger = (
        _KAS_TRIGGER_CANONICAL.get(trigger_raw.lower()) if isinstance(trigger_raw, str) else None
    )
    if trigger is None:
        agent_mod.logger.warning(
            "kiro_hooks[%d]: unknown trigger %s, skipping",
            index,
            agent_mod._hook_diagnostic(trigger_raw),
        )
        agent_mod._sel_hook_rejected(_HOOK_SPEC_AUDIT_TAG, str(trigger_raw), "unknown trigger")
        return None
    action = _hook_document_action(entry.get("action"), index=index)
    if action is None:
        return None
    for field, expected in _KAS_DOCUMENT_FIELD_TYPES:
        if field in entry and not isinstance(entry[field], expected):
            agent_mod.logger.warning(
                "kiro_hooks[%d]: %s has the wrong type, skipping", index, field
            )
            agent_mod._sel_hook_rejected(
                _HOOK_SPEC_AUDIT_TAG, str(entry.get(field)), f"{field} has the wrong type"
            )
            return None
    if "timeout" in entry and (
        not isinstance(entry["timeout"], int)
        or isinstance(entry["timeout"], bool)
        or entry["timeout"] <= 0
    ):
        agent_mod.logger.warning(
            "kiro_hooks[%d]: timeout must be a positive integer, skipping", index
        )
        agent_mod._sel_hook_rejected(
            _HOOK_SPEC_AUDIT_TAG, str(entry.get("timeout")), "timeout not a positive integer"
        )
        return None
    for field, limit in _KAS_DOCUMENT_FIELD_LIMITS:
        value = entry.get(field)
        if isinstance(value, str) and len(value) > limit:
            agent_mod.logger.warning(
                "kiro_hooks[%d]: %s is longer than %d characters, skipping", index, field, limit
            )
            agent_mod._sel_hook_rejected(_HOOK_SPEC_AUDIT_TAG, value, f"{field} too long")
            return None
    name = entry.get("name")
    if name is not None and not name:
        agent_mod.logger.warning("kiro_hooks[%d]: name is empty, skipping", index)
        agent_mod._sel_hook_rejected(_HOOK_SPEC_AUDIT_TAG, "", "name is empty")
        return None
    if "matcher" in entry and not _hook_matcher_ok(entry["matcher"]):
        agent_mod.logger.warning(
            "kiro_hooks[%d]: matcher contains disallowed characters or is too long, skipping", index
        )
        agent_mod._sel_hook_rejected(
            _HOOK_SPEC_AUDIT_TAG, str(entry.get("matcher")), "invalid matcher"
        )
        return None
    doc: dict = {
        "name": name if isinstance(name, str) else f"{trigger}-{index}",
        "trigger": trigger,
        "action": action,
    }
    if "matcher" in entry:
        doc["matcher"] = entry["matcher"]
    for field, _expected in _KAS_DOCUMENT_FIELD_TYPES:
        if field in entry and field != "name":
            doc[field] = entry[field]
    if "timeout" in entry:
        doc["timeout"] = entry["timeout"]
    return doc


def _hook_documents_from_array_form(hooks: list) -> list[dict]:
    """Normalize a KAS array of hook documents to the document list."""
    if len(hooks) > _MAX_SPEC_HOOK_DOCUMENTS:
        agent_mod.logger.warning(
            "kiro_hooks: %d documents exceeds the limit of %d, ignoring the remainder",
            len(hooks),
            _MAX_SPEC_HOOK_DOCUMENTS,
        )
        agent_mod._sel_hook_rejected(
            _HOOK_SPEC_AUDIT_TAG, str(len(hooks)), "document limit exceeded"
        )
    docs: list[dict] = []
    for index, entry in enumerate(hooks[:_MAX_SPEC_HOOK_DOCUMENTS]):
        doc = _hook_document_from_document(entry, index=index)
        if doc is not None:
            docs.append(doc)
    return docs


def normalize_spec_hooks(value: object) -> list[dict]:
    """Normalize a KAS array of hook documents to the internal document list.

    A rejected element is warned about and SEL-audited rather than raising, which
    is how a spec's ``hooks`` has always treated bad input.

    Crew's object-of-arrays is NOT read here. It goes to ``_merge_kiro_hooks`` as
    it was read, because that merge is the object form's own validator and
    auditor: every rule about a command, a matcher, dedup and the caps lives
    there, and re-deriving the object form from documents would move its error
    path and the bytes kiro-cli receives. So anything that is not an array —
    an object included — is rejected here, and the one caller sends an object
    straight to that merge instead.
    """
    if isinstance(value, list):
        return _hook_documents_from_array_form(value)
    agent_mod.logger.warning("kiro_hooks is not an array of hook documents, ignoring")
    agent_mod._sel_hook_rejected(
        _HOOK_SPEC_AUDIT_TAG, str(value), "hooks is not an array of hook documents"
    )
    return []


def _hook_command_reaches_a_share(command: str) -> bool:
    """True when resolving this command would touch a network or device path.

    ``Path.resolve()`` on a UNC path is an outbound SMB authentication on Windows,
    so an LLM-writable config naming ``\\\\attacker\\share\\x.sh`` would make the
    install pass hand credentials to a host of someone else's choosing. The shape
    is judged lexically, before any resolution, and it is judged on the string the
    author wrote AND on its user-expanded form so a ``~`` cannot smuggle one in.

    The three terms are the spelling every UNC gate in this tree uses, and each
    one is load-bearing. The probe is a Windows one: on POSIX a leading ``//`` is
    an ordinary path and ``\\\\host\\share`` is one filename, so refusing either
    there would reject a legal command for a risk that platform does not have.
    And ``unc_probe_allowed`` is the user's own consent — a roaming profile whose
    data home IS a share must be able to run a hook script that lives on it.

    One caller: :func:`_resolved_hook_command`, where the resolve is the first
    thing that touches the path. ``_validate_hook_command`` deliberately does NOT
    ask — the object form has always resolved its command there, autoimport hands
    it a path already resolved and stat-ed, and refusing on shape at that point
    would silently un-install every hook on a host whose hooks directory lives on
    a share while preventing no probe at all.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    for candidate in (command, os.path.expanduser(command)):
        if is_unc_shape(candidate) and not unc_probe_allowed(candidate):
            return True
    return False


def _resolved_hook_command(command: object) -> str | None:
    """Resolve a hook command for the suppression comparison, or None.

    One spelling for both sides: a document's command and an autoimport entry's
    command must resolve through the same steps or a match is missed. ``resolve``
    raises on a symlink loop, and a command that cannot be resolved matches
    nothing.

    ``expanduser`` here is deliberately NOT what ``_validate_hook_command`` does:
    its allowlist rejects ``~`` outright, so a document spelling its command that
    way can never itself install a hook. The expansion exists for the other side
    of the comparison — autoimport reports absolute resolved paths, and a document
    that switched off ``~/.kiro/hooks/guard.sh`` has to suppress the script that
    scan finds.
    """
    if not isinstance(command, str) or not command:
        return None
    if _hook_command_reaches_a_share(command):
        # Naming the consequence, not just the refusal: this read is what
        # subtracts a switched-off script from the autoimport scan, so a command
        # left unresolved here leaves that script installed. On a host whose
        # hooks directory is itself on a share, that is every such document.
        agent_mod.logger.warning(
            "kiro_hooks: command %s is a network or device path, so it is not resolved "
            "and a scan-discovered script by that name stays installed",
            agent_mod._hook_diagnostic(command),
        )
        return None
    try:
        return str(Path(os.path.expanduser(command)).resolve())
    except (OSError, ValueError, RuntimeError):
        agent_mod.logger.debug("kiro_hooks: cannot resolve a hook command", exc_info=True)
        return None


# Why each suppressed command is suppressed, as the audit line reports it. The
# two causes are not interchangeable in an audit trail: one says the author
# switched the hook off, the other says the author wanted it to run behind a
# prompt kiro-cli cannot give.
_HOOK_SUPPRESSED_DISABLED = "suppressed by a disabled spec document"
_HOOK_SUPPRESSED_CONFIRM = "suppressed by a confirmation-gated spec document"


def hook_documents_suppressed_commands(hooks: object) -> dict[str, str]:
    """Resolved commands whose author switched execution OFF, and why, read RAW.

    ``enabled: false`` and ``confirm: true`` keep a hook out of the emission, and
    that has to hold against the OTHER source of hooks: autoimport scans
    ``~/.kiro/hooks`` for executable scripts, so a document naming a command that
    lives there would be dropped here and rediscovered as a fresh entry, landing
    on autoimport's default event — a broader one than the document named. The
    caller subtracts these commands from what autoimport found, so "off" means off
    whichever way the script is reachable.

    Read from the RAW array rather than from the normalized documents, and this
    ordering is the point: a document that says ``enabled: false`` and is then
    rejected for something else entirely — a kiro-agent matcher like
    ``@git/status``, a bad ``timeout`` — never becomes a document at all, so a
    post-validation read would let autoimport arm its script, unscoped, against an
    author who switched it off. "Off" is legible from two well-typed fields and a
    command string, so it is taken from those alone.

    Only the fields this decision needs are trusted: the flag must be exactly
    ``False``/``True`` and the command a non-empty string. Everything else about
    the entry stays the validator's business, and a normalized document is
    accepted here too because it carries the same three fields.

    Bypassing the validator means carrying its two bounds here rather than
    inheriting them. The same ``_MAX_SPEC_HOOK_DOCUMENTS`` slice the normalizer
    announces, because a document the log calls ignored must not still delete an
    autoimported script; and ``_MAX_HOOK_PAYLOAD_LEN`` on the command, because
    this mapping is retained and an unbounded string in it is unbounded memory.

    The cause travels with the command because the caller audits it. ``enabled:
    false`` outranks ``confirm: true`` on one document and across two documents
    naming the same command: "the author switched this off" is the stronger
    statement, and it must not be weakened by the order the array happens to be
    written in.
    """
    if not isinstance(hooks, list):
        return {}
    suppressed: dict[str, str] = {}
    for entry in hooks[:_MAX_SPEC_HOOK_DOCUMENTS]:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            cause = _HOOK_SUPPRESSED_DISABLED
        elif entry.get("confirm") is True:
            cause = _HOOK_SUPPRESSED_CONFIRM
        else:
            continue
        raw_action = entry.get("action")
        action: dict = raw_action if isinstance(raw_action, dict) else {}
        command = action.get("command")
        if not isinstance(command, str) or not command:
            continue
        if len(command) > _MAX_HOOK_PAYLOAD_LEN:
            continue
        resolved = _resolved_hook_command(command)
        if resolved is None:
            continue
        if suppressed.get(resolved) == _HOOK_SUPPRESSED_DISABLED:
            continue
        suppressed[resolved] = cause
    return suppressed


def hook_documents_to_object_form(docs: Sequence[dict]) -> dict[str, list[dict]]:
    """Derive the kiro-cli object form from normalized hook documents.

    Only a ``command`` action on one of the five triggers kiro-cli names is
    expressible. An ``agent`` action, one of the seven triggers kiro-cli has no
    name for, and the per-document ``name``, ``description`` and ``timeout`` have
    no object-form slot, so they are dropped HERE, on the emission path, and kept
    in Crew's stored spec.

    ``enabled`` and ``confirm`` are not dropped that way. Each grants LESS
    execution than the object form can express, so an entry emitted without them
    would run unconditionally and unprompted: ``enabled: false`` and
    ``confirm: true`` keep the whole hook out of the emission instead.
    """
    result: dict[str, list[dict]] = {}
    for doc in docs:
        raw_action = doc.get("action")
        action: dict = raw_action if isinstance(raw_action, dict) else {}
        event = _event_for_hook_trigger(doc.get("trigger"))
        command = action.get("command")
        # ``enabled`` and ``confirm`` each grant LESS execution than the object
        # form can express, so neither may be dropped the way a label is: an
        # entry emitted without them runs unconditionally and unprompted. A hook
        # the author switched off, or asked to be prompted for, is left out.
        if doc.get("enabled") is False:
            # A chosen steady state, re-read on every refresh: INFO, not a
            # warning an operator learns to scroll past. The SEL line stays,
            # because what is installed differs from what was authored.
            agent_mod.logger.info(
                "kiro_hooks: hook %s is disabled, leaving it out of the kiro-cli spec",
                agent_mod._hook_diagnostic(doc.get("name")),
            )
            agent_mod._sel_hook_rejected(str(doc.get("trigger")), str(command), "hook is disabled")
            continue
        if doc.get("confirm") is True:
            # Unlike ``enabled: false``, this author wanted the hook to run —
            # with a prompt kiro-cli cannot give. The hook not firing is the
            # surprise, so it warns at the same level as an inexpressible
            # trigger rather than at the disabled hook's INFO.
            agent_mod.logger.warning(
                "kiro_hooks: hook %s asks to be confirmed, which a kiro-cli hook cannot do, "
                "leaving it out of the kiro-cli spec",
                agent_mod._hook_diagnostic(doc.get("name")),
            )
            agent_mod._sel_hook_rejected(
                str(doc.get("trigger")), str(command), "hook asks to be confirmed"
            )
            continue
        if event is None or action.get("type") != "command" or not isinstance(command, str):
            agent_mod.logger.warning(
                "kiro_hooks: hook %s has trigger %s and action type %s, which no kiro-cli hook "
                "event can express, so it does not run there",
                agent_mod._hook_diagnostic(doc.get("name")),
                agent_mod._hook_diagnostic(doc.get("trigger")),
                agent_mod._hook_diagnostic(action.get("type")),
            )
            agent_mod._sel_hook_rejected(
                str(doc.get("trigger")),
                str(command),
                "no kiro-cli hook event can express this hook",
            )
            continue
        if doc.get("timeout") is not None:
            # A bound the author asked for, which kiro-cli's hook entry cannot
            # carry: the command still runs, under kiro-cli's own bound rather
            # than this one. That is a constraint quietly widened, so it warns
            # like a confirm that cannot prompt rather than sitting at INFO.
            agent_mod.logger.warning(
                "kiro_hooks: hook %s asks for a %s second timeout, which a kiro-cli hook "
                "cannot carry, so the command runs under kiro-cli's own bound",
                agent_mod._hook_diagnostic(doc.get("name")),
                agent_mod._hook_diagnostic(doc.get("timeout")),
            )
        entry: dict[str, str] = {"command": command}
        if isinstance(doc.get("matcher"), str):
            entry["matcher"] = doc["matcher"]
        result.setdefault(event, []).append(entry)
    return result


# Recognize hook event from filename suffix when no "# event:" header is set.
# Ordering matters: check more specific suffixes first.
_FILENAME_EVENT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("-post.sh", "postToolUse"),
    ("-prompt.sh", "userPromptSubmit"),
    ("-spawn.sh", "agentSpawn"),
    ("-stop.sh", "stop"),
    ("-pre.sh", "preToolUse"),
)

# Header parsing — only inspect the first few lines so the scan stays O(K).
_HOOK_HEADER_SCAN_LINES = 5
_HOOK_HEADER_RE = re.compile(r"^\s*#\s*(event|matcher)\s*:\s*(\S.*?)\s*$", re.IGNORECASE)


def _parse_hook_script_headers(path: Path) -> tuple[str | None, str | None]:
    """Read the first few lines of a hook script and extract ``# event:`` / ``# matcher:`` directives.

    Returns ``(event_header, matcher_header)``.  Either may be ``None`` if not present.
    Values are returned unparsed; callers normalize/validate them.
    """
    event_header: str | None = None
    matcher_header: str | None = None
    try:
        # Read at most a handful of lines; hook scripts can be large, and we
        # only care about headers immediately after the shebang.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _HOOK_HEADER_SCAN_LINES:
                    break
                m = _HOOK_HEADER_RE.match(line)
                if not m:
                    continue
                key = m.group(1).lower()
                val = m.group(2)
                if key == "event" and event_header is None:
                    event_header = val
                elif key == "matcher" and matcher_header is None:
                    matcher_header = val
    except OSError:
        agent_mod.logger.debug(
            "kiro_hooks_autoimport: could not read %s for headers",
            agent_mod._hook_diagnostic(path),
            exc_info=True,
        )
    return event_header, matcher_header


def _infer_hook_event(script_path: Path, event_header: str | None) -> str | None:
    """Resolve a script's kiro hook event.

    Precedence:
      1. Explicit ``# event:`` header (normalized to camelCase).  Unknown values
         return ``None`` so the caller can WARN and skip.
      2. Filename suffix convention (``*-post.sh`` -> ``postToolUse`` etc.).
      3. Default: ``preToolUse``.
    """
    if event_header is not None:
        canonical = _HOOK_EVENT_CANONICAL.get(
            event_header.lower().replace("-", "").replace("_", "")
        )
        return canonical  # None if unknown -- caller decides what to do

    name = script_path.name.lower()
    for suffix, event in _FILENAME_EVENT_SUFFIXES:
        if name.endswith(suffix):
            return event
    return "preToolUse"


def _autoimport_kiro_hooks(hooks_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Scan ``hooks_dir`` for executable ``*.sh`` files and return a ``kiro_hooks``-shaped dict.

    Each discovered script becomes an entry under its resolved event (camelCase).
    Returns an empty dict if the directory is missing or contains no usable scripts.

    Security parity with the explicit config path:
      * Each script's resolved path goes through ``_validate_hook_command``.
      * ``# matcher:`` headers are validated against ``_SAFE_MATCHER_RE`` / ``_MAX_MATCHER_LEN``.
      * Non-executable files are skipped (INFO log).
      * Sensitive paths are skipped (via ``_validate_hook_command``).

    Final dedup, per-event cap, and total cap are enforced by ``_merge_kiro_hooks``
    which runs on the returned dict.  That keeps explicit config precedence correct:
    callers should invoke ``_merge_kiro_hooks`` with the already-merged ``hooks``
    (bundled + explicit) so auto-imported scripts that duplicate an explicit entry
    are deduped out rather than taking its slot.
    """
    result: dict[str, list[dict[str, str]]] = {}
    try:
        resolved_hooks_dir = hooks_dir.resolve()
    except (OSError, ValueError):
        # OSError: ENAMETOOLONG, ELOOP, EACCES on a path component.
        # ValueError: null bytes (``"\x00"``) reject at Path construction.
        # Emit SEL audit so an auditor sees a distinct "hooks_dir
        # unresolvable" signal — same symmetry principle as the
        # per-entry ``cannot resolve entry`` branch below.
        agent_mod.logger.debug(
            "kiro_hooks_autoimport: cannot resolve %s, skipping",
            agent_mod._hook_diagnostic(hooks_dir),
            exc_info=True,
        )
        agent_mod._sel_hook_rejected("autoimport", str(hooks_dir), "cannot resolve hooks_dir")
        return result
    try:
        entries = sorted(resolved_hooks_dir.iterdir())
    except FileNotFoundError:
        agent_mod.logger.debug(
            "kiro_hooks_autoimport: directory %s does not exist, skipping",
            agent_mod._hook_diagnostic(hooks_dir),
        )
        return result
    except OSError:
        agent_mod.logger.warning(
            "kiro_hooks_autoimport: cannot read %s, skipping",
            agent_mod._hook_diagnostic(hooks_dir),
            exc_info=True,
        )
        # Emit SEL audit so an auditor reconstructing agent-install
        # activity sees a distinct "hooks dir unreadable" signal rather
        # than only the merge-summary ``requested_autoimport=0`` (which
        # looks identical to the no-scripts-configured case).  Same
        # symmetry principle as the per-script rejection branches.
        agent_mod._sel_hook_rejected("autoimport", str(hooks_dir), "cannot read hooks_dir")
        return result

    loaded = 0
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".sh":
            continue

        # Resolve once up-front and reuse the resolved path for all subsequent
        # checks (stat, validation).  This closes two issues:
        # * TOCTOU: repeated resolve() in _validate_hook_command could race
        #   with an attacker swapping the symlink target between calls.
        # * Symlink escape: entry.is_file() follows symlinks, so a symlink
        #   inside the hooks dir pointing at /tmp/attacker.sh would otherwise
        #   pass (not in _SENSITIVE_HOME_DIRS).  Require the resolved target
        #   to stay under the resolved hooks dir.
        try:
            resolved_entry = entry.resolve()
        except (OSError, ValueError):
            # OSError: typical filesystem failures.  ValueError: filename
            # from ``iterdir()`` carries a null byte or other malformed
            # character that ``Path.resolve()`` rejects.  Without this
            # catch, a maliciously-named file in hooks_dir crashes agent
            # bootstrap.
            agent_mod.logger.warning(
                "kiro_hooks_autoimport: cannot resolve %s, skipping",
                agent_mod._hook_diagnostic(entry),
                exc_info=True,
            )
            agent_mod._sel_hook_rejected("autoimport", str(entry), "cannot resolve entry")
            continue
        if (
            resolved_entry != resolved_hooks_dir
            and resolved_hooks_dir not in resolved_entry.parents
        ):
            agent_mod.logger.warning(
                "kiro_hooks_autoimport: %s resolves outside %s (to %s), skipping",
                agent_mod._hook_diagnostic(entry),
                agent_mod._hook_diagnostic(resolved_hooks_dir),
                agent_mod._hook_diagnostic(resolved_entry),
            )
            agent_mod._sel_hook_rejected(
                "autoimport", str(entry), "resolved path escapes hooks dir"
            )
            continue

        try:
            resolved_entry.stat()  # surface a stat error (broken symlink, perms) as a skip
        except OSError:
            agent_mod.logger.warning(
                "kiro_hooks_autoimport: cannot stat %s, skipping", agent_mod._hook_diagnostic(entry)
            )
            agent_mod._sel_hook_rejected("autoimport", str(entry), "cannot stat entry")
            continue
        # Executable check is platform-aware: POSIX requires the execute bit (so
        # `chmod -x` disables a hook); Windows has no execute bit, so requiring
        # X_OK there would skip EVERY hook and silently break the whole autoimport
        # — instead a known script extension (.sh/.ps1/.cmd/...) is treated as
        # runnable. See platform_compat.is_executable_file.
        if not platform_compat.is_executable_file(resolved_entry):
            agent_mod.logger.info(
                "kiro_hooks_autoimport: %s is not executable, skipping",
                agent_mod._hook_diagnostic(entry),
            )
            # Audit parity with the other rejection branches
            # (symlink-escape, cannot-resolve, cannot-stat,
            # failed-validation, unknown-event, invalid-matcher,
            # cannot-read-dir): the non-executable skip is also a
            # permission decision — it determines that a discovered
            # ``.sh`` file will NOT be loaded as a hook — so it must
            # emit a SEL audit event per AUTOSDE.yaml security-controls
            # rule.  Without this call, an auditor reconstructing
            # agent-install activity from SEL would not see scripts
            # that were skipped for lacking the execute bit.
            agent_mod._sel_hook_rejected("autoimport", str(entry), "not executable")
            continue

        # Defense-in-depth: run the full validation (including
        # is_sensitive_path) BEFORE any file I/O on the script.  The
        # symlink-escape check above already rejects most attacks, but
        # running _validate_hook_command first keeps the "no reads on
        # sensitive paths" invariant intact even if the resolved-path
        # check is ever loosened.  The ``"autoimport"`` event label
        # below is a log tag only - _validate_hook_command uses ``event``
        # solely for log formatting, never as a policy key (e.g. it is
        # never matched against _VALID_HOOK_EVENTS).  The real event is
        # computed from headers after this call succeeds.
        validated_command = _validate_hook_command(str(resolved_entry), "autoimport")
        if validated_command is None:
            # _validate_hook_command already emitted a WARNING with the reason.
            agent_mod._sel_hook_rejected("autoimport", str(entry), "failed validation")
            continue

        event_header, matcher_header = _parse_hook_script_headers(resolved_entry)
        event = _infer_hook_event(entry, event_header)
        if event is None:
            agent_mod.logger.warning(
                "kiro_hooks_autoimport: %s declares unknown event %s, skipping",
                agent_mod._hook_diagnostic(entry),
                agent_mod._hook_diagnostic(event_header),
            )
            # Match the other three rejection branches in this function
            # (symlink-escape, failed-validation, invalid-matcher): every
            # rejection must emit a SEL audit event per AUTOSDE.yaml's
            # security-controls rule.  Without this call, an auditor
            # reconstructing agent-install activity from SEL would not
            # see scripts that were dropped for declaring unknown event
            # names, which defeats the purpose of the audit trail.
            agent_mod._sel_hook_rejected("autoimport", str(entry), "unknown event header")
            continue

        entry_dict: dict[str, str] = {"command": validated_command}
        if matcher_header is not None:
            if len(matcher_header) > _MAX_MATCHER_LEN or not _SAFE_MATCHER_RE.match(matcher_header):
                # An invalid matcher is treated as a validation failure:
                # promoting a tool-scoped hook to unscoped (firing on every
                # tool call) would be a silent privilege expansion.
                agent_mod.logger.warning(
                    "kiro_hooks_autoimport: %s matcher %s is invalid, skipping script",
                    agent_mod._hook_diagnostic(entry),
                    agent_mod._hook_diagnostic(matcher_header),
                )
                agent_mod._sel_hook_rejected("autoimport", str(entry), "invalid matcher")
                continue
            entry_dict["matcher"] = matcher_header

        result.setdefault(event, []).append(entry_dict)
        loaded += 1

    if loaded:
        agent_mod.logger.info(
            "kiro_hooks_autoimport: loaded %d scripts from %s",
            loaded,
            agent_mod._hook_diagnostic(hooks_dir),
        )
    else:
        agent_mod.logger.debug(
            "kiro_hooks_autoimport: no scripts loaded from %s",
            agent_mod._hook_diagnostic(hooks_dir),
        )
    return result


def _merge_kiro_hooks(hooks: dict, user_hooks: dict) -> dict:
    """Append user-defined kiro_hooks to bundled hooks (per event type).

    Bundled hooks are always first.  User hooks are appended, deduped by
    ``(command, matcher)`` tuple so the same hook doesn't fire twice.
    Malformed entries (missing ``command``) are silently skipped.
    Commands are validated: must be absolute paths to existing files,
    with no shell metacharacters and not in sensitive locations.
    """
    if not isinstance(user_hooks, dict):
        agent_mod.logger.warning("kiro_hooks is not a dict, ignoring")
        return hooks
    merged = dict(hooks)
    total_added = 0
    for event, entries in user_hooks.items():
        if event not in _VALID_HOOK_EVENTS:
            # Two different rejections wearing one message is a support cost: a
            # Kiro-Agent-only trigger is a name Kiro Crew knows and stores, it
            # just cannot travel in a kiro-cli spec, and reporting it as
            # "unknown" sends the reader hunting a typo that is not there.
            #
            # Through `_hook_diagnostic`, like every other rejection line here: the
            # event name is author-supplied, and that helper escapes before it
            # redacts so a newline inside it cannot forge a second log record.
            crew_only = event in _CREW_ONLY_HOOK_EVENTS
            reason = (
                "Kiro Agent trigger, not emitted to kiro-cli" if crew_only else "unknown event type"
            )
            agent_mod.logger.warning(
                "kiro_hooks: %s: %s, skipping", reason, agent_mod._hook_diagnostic(event)
            )
            # Audit parity with every other rejection branch in this
            # function: per AUTOSDE.yaml security-controls, rejecting an
            # entire event-bucket is a permission decision that must be
            # SEL-audited.  Use the (invalid) event name as the tag so
            # auditors can correlate with the config input.
            agent_mod._sel_hook_rejected(str(event), str(entries), reason)
            continue
        if not isinstance(entries, list):
            agent_mod.logger.warning(
                "kiro_hooks[%s] is not a list, skipping", agent_mod._hook_diagnostic(event)
            )
            # Same audit-parity rationale: dropping a non-list
            # entries-bucket removes all configured hooks for that
            # event.  SEL must record the decision so auditors can
            # distinguish "0 configured" from "N dropped as non-list".
            agent_mod._sel_hook_rejected(event, str(entries), "entries not a list")
            continue
        existing = list(merged.get(event, []))
        existing_keys = {
            (e.get("command"), e.get("matcher")) for e in existing if isinstance(e, dict)
        }
        added = 0
        for entry in entries:
            if added >= _MAX_USER_HOOKS_PER_EVENT:
                agent_mod.logger.warning(
                    "kiro_hooks[%s]: limit of %d reached, ignoring remaining",
                    event,
                    _MAX_USER_HOOKS_PER_EVENT,
                )
                # Audit parity with every other rejection branch in this
                # function (missing command, failed validation, non-string
                # matcher, invalid matcher): hitting the per-event cap is
                # a permission decision - configured hooks are being
                # prevented from loading - and must emit a SEL audit
                # event per AUTOSDE.yaml security-controls.  Without
                # this, an auditor cannot distinguish "user configured 15
                # preToolUse hooks and 5 were cap-dropped" from "user
                # configured 10 and all loaded".
                agent_mod._sel_hook_rejected(
                    event,
                    (str(entry.get("command", "")) if isinstance(entry, dict) else str(entry)),
                    "per-event limit exceeded",
                )
                break
            if total_added >= _MAX_TOTAL_USER_HOOKS:
                agent_mod.logger.warning(
                    "kiro_hooks: global limit of %d reached, ignoring remaining",
                    _MAX_TOTAL_USER_HOOKS,
                )
                # Same audit-parity rationale as the per-event cap above:
                # hitting the global cap drops remaining hooks across all
                # events, and auditors need a SEL signal to distinguish
                # "25 configured, 5 cap-dropped" from "20 configured, all
                # loaded".
                agent_mod._sel_hook_rejected(
                    event,
                    (str(entry.get("command", "")) if isinstance(entry, dict) else str(entry)),
                    "global limit exceeded",
                )
                break
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("command"), str)
                or not entry["command"]
            ):
                agent_mod.logger.warning("kiro_hooks[%s]: skipping entry without command", event)
                agent_mod._sel_hook_rejected(event, str(entry), "missing or invalid command")
                continue
            resolved = _validate_hook_command(entry["command"], event)
            if resolved is None:
                agent_mod._sel_hook_rejected(event, entry["command"], "failed validation")
                continue
            matcher = entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                agent_mod.logger.warning(
                    "kiro_hooks[%s]: matcher must be a string, skipping", event
                )
                agent_mod._sel_hook_rejected(event, entry["command"], "non-string matcher")
                continue
            if isinstance(matcher, str) and (
                len(matcher) > _MAX_MATCHER_LEN or not _SAFE_MATCHER_RE.match(matcher)
            ):
                agent_mod.logger.warning(
                    "kiro_hooks[%s]: matcher contains disallowed characters or is too long, skipping",
                    event,
                )
                agent_mod._sel_hook_rejected(event, entry["command"], "invalid matcher")
                continue
            key = (resolved, matcher)
            if key not in existing_keys:
                sanitized = {"command": resolved}
                if isinstance(matcher, str):
                    sanitized["matcher"] = matcher
                existing.append(sanitized)
                existing_keys.add(key)
                added += 1
                total_added += 1
        merged[event] = existing
    return merged


def _apply_user_kiro_hooks(config: dict, mc_cfg: dict) -> None:
    """Merge user-defined kiro_hooks from kirocrew config into *config* (additive).

    Two sources, explicit first then auto-discovered:

      1. ``agent.kiro_hooks`` in ``~/.kiro/crew/config.json`` -- explicit entries
         the user wrote by hand.  Unchanged behavior.
      2. ``agent.kiro_hooks_autoimport`` (default true): scan
         ``agent.kiro_hooks_dir`` (default ``~/.kiro/hooks``) for executable
         ``*.sh`` scripts and merge each as a hook entry.  Event is parsed from
         an optional ``# event:`` header, inferred from a filename suffix, or
         defaults to ``preToolUse``.  Optional ``# matcher:`` header gives the
         same tool-name matcher as explicit entries.

    Autoimport runs in a single merge pass with explicit entries listed first,
    so autoimported scripts that duplicate an explicit entry are deduped out
    (explicit wins) and caps (``_MAX_USER_HOOKS_PER_EVENT`` and
    ``_MAX_TOTAL_USER_HOOKS``) are enforced across both sources combined,
    not per-source.
    """
    agent_cfg = mc_cfg.get("agent") if isinstance(mc_cfg.get("agent"), dict) else {}
    user_hooks = agent_cfg.get("kiro_hooks") if isinstance(agent_cfg, dict) else None
    autoimport_enabled = True
    hooks_dir = agent_mod._DEFAULT_KIRO_HOOKS_DIR
    if isinstance(agent_cfg, dict):
        if "kiro_hooks_autoimport" in agent_cfg:
            autoimport_enabled = bool(agent_cfg.get("kiro_hooks_autoimport"))
        custom_dir = agent_cfg.get("kiro_hooks_dir")
        if isinstance(custom_dir, str) and custom_dir:
            # config.json is LLM-writable; a malicious override could point
            # hooks_dir at /tmp, a world-writable mount, or ~/Downloads.
            # Require the resolved path to live under the user's HOME and
            # not match a sensitive location.  On any failure, log + SEL
            # audit and fall back to the default (~/.kiro/hooks) rather
            # than turning autoimport off entirely - the safe default is
            # still available.
            requested = Path(os.path.expanduser(custom_dir))
            try:
                resolved = requested.resolve()
                home = Path.home().resolve()
            except (OSError, ValueError):
                # OSError: ENAMETOOLONG, ELOOP (symlink loop), EACCES.
                # ValueError: Path() / resolve() reject strings with null
                # bytes (``"\x00"``) or similar malformed Unicode.  An
                # LLM-writable ``kiro_hooks_dir: "\x00"`` would otherwise
                # propagate ValueError up through install_agent() and
                # crash agent bootstrap (denial of service).
                resolved = None
                home = None
            if (
                resolved is None
                or home is None
                # Strict containment: require ``resolved`` to be *under*
                # HOME, not equal to it.  ``~`` alone would otherwise scan
                # the entire home directory for executable ``*.sh`` files,
                # auto-registering anything a user (or attacker) drops
                # anywhere under ``$HOME``.  ``Path.parents`` of e.g.
                # ``/srv/op`` is ``(/, /srv)`` and does NOT include
                # ``/srv/op`` itself, so a bare ``home not in parents``
                # rejects ``resolved == home``.
                or home not in resolved.parents
                or agent_mod.is_sensitive_path(str(resolved))
            ):
                agent_mod.logger.warning(
                    "kiro_hooks_autoimport: kiro_hooks_dir %s rejected "
                    "(must resolve under %s and not be sensitive), "
                    "falling back to %s",
                    agent_mod._hook_diagnostic(custom_dir),
                    home,
                    agent_mod._DEFAULT_KIRO_HOOKS_DIR,
                )
                agent_mod._sel_hook_rejected(
                    "autoimport", str(requested), "kiro_hooks_dir outside HOME or sensitive"
                )
            else:
                # Store the already-resolved path, not the unresolved
                # ``requested``.  Keeping ``requested`` would leave a
                # symlink-swap window: a path component could be swapped
                # between this resolve() and the one inside
                # _autoimport_kiro_hooks, bypassing the HOME containment
                # check we just performed.
                hooks_dir = resolved

    # Both spec shapes are accepted. The object form goes to the merge below
    # exactly as it is read: that merge is the object form's own validator and
    # auditor, and routing it through the document form instead would move its
    # error path and could perturb the bytes kiro-cli receives. The array form is
    # normalized to hook documents and then projected onto the object form, so
    # command validation, matcher rules, dedup and the caps hold both shapes to
    # one bar.
    explicit_hooks: dict
    suppressed_commands: dict[str, str] = {}
    # Documents the author wrote, for the audit summary. Zero on the object-form
    # path, where the per-event loop below counts entries instead.
    requested_documents = 0
    if isinstance(user_hooks, dict):
        explicit_hooks = user_hooks
    elif user_hooks is not None:
        # Every other value, a list included, goes through the normalizer, so a
        # ``hooks`` that is neither shape is audited rather than dropped in
        # silence.
        documents = normalize_spec_hooks(user_hooks)
        explicit_hooks = hook_documents_to_object_form(documents)
        # Read from the raw value, BEFORE validation: a document that says
        # ``enabled: false`` and is rejected for an unrelated field must still
        # keep its script out of the autoimport scan.
        suppressed_commands = hook_documents_suppressed_commands(user_hooks)
        # The audit's "requested" figure counts what the author WROTE — the raw
        # array — not the documents that survived validation and not the
        # projection that survived expressibility. Either narrower count reports
        # a fraction of the config as requested.
        requested_documents = len(user_hooks) if isinstance(user_hooks, list) else 0
    else:
        explicit_hooks = {}
    has_explicit = bool(explicit_hooks)
    if not has_explicit and not autoimport_enabled:
        return

    before = sum(len(v) for v in config.get("hooks", {}).values() if isinstance(v, list))

    # Collect both sources up-front and merge in a SINGLE ``_merge_kiro_hooks``
    # pass.  Rationale: ``_merge_kiro_hooks`` initializes ``total_added = 0`` on
    # each call, so invoking it twice would allow the per-call
    # ``_MAX_TOTAL_USER_HOOKS`` cap (20) to apply to each source independently —
    # yielding up to 40 user hooks total instead of the intended 20.  A single
    # pass enforces the per-event cap AND the total cap across the combined
    # set.  Explicit entries are listed first in each event's list so they
    # claim the dedup key before any duplicate from autoimport, preserving the
    # "explicit wins" precedence.
    # Count explicit entries AND audit any non-list buckets as we go.
    # Using a plain loop rather than a generator expression so we can
    # emit WARNING + SEL audit for each dropped event bucket -- dropping
    # a whole event's hooks is a permission decision per AUTOSDE.yaml
    # security-controls, and the caller-side filter must audit it
    # (``_merge_kiro_hooks``'s internal defensive check never fires here
    # because this filter runs first).
    requested_explicit = requested_documents
    for event, entries in explicit_hooks.items():
        if isinstance(entries, list):
            if not requested_documents:
                requested_explicit += len(entries)
        else:
            agent_mod.logger.warning(
                "kiro_hooks[%s] is not a list, skipping", agent_mod._hook_diagnostic(event)
            )
            agent_mod._sel_hook_rejected(str(event), str(entries), "entries not a list")
    requested_autoimport = 0
    discovered: dict[str, list[dict[str, str]]] = {}
    if autoimport_enabled:
        discovered = _autoimport_kiro_hooks(hooks_dir)
        requested_autoimport = sum(len(v) for v in discovered.values() if isinstance(v, list))

    if requested_explicit == 0 and requested_autoimport == 0:
        # Nothing to merge; keep config["hooks"] untouched (or create empty
        # dict for shape consistency if it wasn't there).
        if "hooks" not in config:
            config["hooks"] = {}
        return

    if suppressed_commands and discovered:
        # A script the author switched off is not re-armed by having been found on
        # disk. Filtered before the merge, so the caps and the dedup below see the
        # set that is actually installed.
        kept: dict[str, list[dict[str, str]]] = {}
        for event, entries in discovered.items():
            survivors = [
                entry
                for entry in entries
                if _resolved_hook_command(entry.get("command", "")) not in suppressed_commands
            ]
            dropped = len(entries) - len(survivors)
            if dropped:
                agent_mod.logger.info(
                    "kiro_hooks_autoimport[%s]: %d script(s) left out, switched off in the spec",
                    event,
                    dropped,
                )
                # The emission path's own audit cannot stand in for this: an
                # off-document rejected for an unrelated field never reaches it,
                # so without this line a discovered, validated hook would stop
                # being installed with nothing in the audit trail.
                for entry in entries:
                    if entry not in survivors:
                        command = str(entry.get("command", ""))
                        # The cause is read back from the mapping rather than
                        # assumed: the same filter drops a hook the author
                        # disabled and one the author asked to be prompted for,
                        # and an audit line that names the wrong one is worse
                        # than a generic one.
                        agent_mod._sel_hook_rejected(
                            str(event),
                            command,
                            suppressed_commands.get(
                                _resolved_hook_command(entry.get("command", "")) or "",
                                _HOOK_SUPPRESSED_DISABLED,
                            ),
                        )
            if survivors:
                kept[event] = survivors
        discovered = kept

    combined_user_hooks: dict[str, list[dict[str, str]]] = {}
    for src in (explicit_hooks, discovered):
        if not isinstance(src, dict):
            continue
        for event, entries in src.items():
            if not isinstance(entries, list):
                # Already WARN+SEL-audited in the ``requested_explicit``
                # loop above (for explicit_hooks) or filtered out at
                # return-time of ``_autoimport_kiro_hooks`` (discovered
                # never contains non-list values).  Defensive continue.
                continue
            combined_user_hooks.setdefault(event, []).extend(entries)

    config["hooks"] = _merge_kiro_hooks(config.get("hooks", {}), combined_user_hooks)

    after = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
    added = after - before
    agent_mod._sel_hooks_merged(requested_explicit, requested_autoimport, added)
