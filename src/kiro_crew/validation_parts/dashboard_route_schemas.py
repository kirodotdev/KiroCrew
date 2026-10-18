"""Request schemas of the dashboard's hook and file routes.

``HOOK_CREATE_SCHEMA`` / ``HOOK_UPDATE_SCHEMA`` and ``FILE_READ_SCHEMA`` /
``FILE_WRITE_SCHEMA``, with the path shape gate the file routes share.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re

from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.validation_parts.input_bounds import ALLOWED_HOOK_EVENTS

# ── Tool Schemas (Hooks) ──


def _validate_hook_has_action(args: dict) -> None:
    """A hook must have either a command or skills — an empty hook is invalid."""
    if not args.get("command") and not args.get("skills"):
        raise ValidationError("command", "either command or skills must be provided")
    # Skills injection only fires for a *standalone* skills hook (no command) on
    # UserPromptSubmit/AgentSpawn — see ScriptHookStore.fire(), which emits the
    # "Load skills:" directive only when `hook.skills and not hook.command` and
    # the event is one of those two. Reject any other pairing at save time so a
    # hook can never save cleanly and then silently never fire:
    #   - skills + a command: the command runs but the skills are inert.
    #   - skills on PreToolUse/PostToolUse/Stop: the directive has no consumer.
    if args.get("skills"):
        if args.get("command"):
            raise ValidationError(
                "skills",
                "skills cannot be combined with a command — the skills would "
                "never fire; use a skills-only hook or drop the skills",
            )
        event = args.get("event", "")
        if event in ("PreToolUse", "PostToolUse", "Stop"):
            raise ValidationError(
                "skills",
                f"skills hooks cannot fire on {event} events — "
                "choose UserPromptSubmit or AgentSpawn",
            )


def _validate_hook_create(args: dict) -> None:
    """Validate a hook at creation time: action + regex syntax + no null enable."""
    _validate_hook_has_action(args)
    _validate_hook_regex(args)
    _reject_null_enabled(args)


def _validate_hook_update(args: dict) -> None:
    """Validate a hook at update time: regex syntax (action already checked by store)."""
    _validate_hook_regex(args)
    _reject_null_enabled(args)


def _reject_null_enabled(args: dict) -> None:
    """Refuse an explicit ``"enabled": null``, on create and on update alike.

    ``validate_field`` answers ``spec.default`` for a ``None`` value BEFORE the type
    check, so a field with no default answers ``None`` -- and the KEY's presence puts
    that ``None`` into the cleaned dict. Downstream, ``data.get("enabled", True)``
    keeps it and the store's own event-aware default is skipped because the key IS
    present, so the hook persists with ``enabled`` neither true nor false. Every
    reader treats it as off: ``fire`` skips the hook and the row renders dimmed.

    Refused rather than coerced, because both coercions lie about what was asked
    for. Reading it as ``True`` invents a request to enable; reading it as omitted
    discards a key the caller deliberately sent. A 400 naming the field is the only
    answer that does not decide for them.

    Both hook validators call this: the same shape is reachable through the update
    schema, whose ``enabled`` has never carried a default, so fixing only the create
    path would leave the identical corruption one endpoint away.
    """
    if "enabled" in args and args["enabled"] is None:
        raise ValidationError("enabled", "expected bool, got null")


def _validate_hook_regex(args: dict) -> None:
    """Reject an invalid regex pattern at save time with a field-level error."""
    matcher = args.get("matcher", "")
    mode = args.get("matcher_mode", "glob")
    if mode == "regex" and matcher:
        try:
            re.compile(matcher)
        except re.error as exc:
            raise ValidationError("matcher", f"invalid regex: {exc}") from None


HOOK_CREATE_SCHEMA = ToolSchema(
    tool_name="hook_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=200),
        FieldSpec("command", str, max_len=2000, default=""),
        FieldSpec("event", str, required=True, allowed=ALLOWED_HOOK_EVENTS),
        FieldSpec("matcher", str, max_len=500, default=""),  # optional: empty = match all
        FieldSpec(
            "matcher_mode",
            str,
            max_len=10,
            default="glob",
            allowed=frozenset({"glob", "regex", "contains"}),
        ),
        FieldSpec("skills", list, default=[], item_type=str, item_max_len=100),
        FieldSpec("timeout", int, min_val=1, max_val=300, default=30),
        # NO default, deliberately: `validate_tool_args` INJECTS a non-``None``
        # default for an omitted field, and the store decides a hook's initial
        # ``enabled`` from whether the caller named it -- a trigger no event fires
        # is stored off unless the caller explicitly asked for on. A ``True`` here
        # fabricates that explicit request on every create, which is the one input
        # that made the store's rule unreachable on the dashboard's own path.
        # ``ScriptHook.from_dict`` still defaults an absent value to True, so a
        # hook on a live event is unaffected.
        FieldSpec("enabled", bool),
    ],
    custom_validator=_validate_hook_create,
)

HOOK_UPDATE_SCHEMA = ToolSchema(
    tool_name="hook_update",
    fields=[
        FieldSpec("name", str, max_len=200),  # optional on update
        FieldSpec("command", str, max_len=2000),  # optional on update
        FieldSpec("event", str, allowed=ALLOWED_HOOK_EVENTS),
        FieldSpec("matcher", str, max_len=500),  # optional: empty = match all
        FieldSpec(
            "matcher_mode", str, max_len=10, allowed=frozenset({"glob", "regex", "contains"})
        ),
        FieldSpec("skills", list, item_type=str, item_max_len=100),
        FieldSpec("timeout", int, min_val=1, max_val=300),
        FieldSpec("enabled", bool),
    ],
    custom_validator=_validate_hook_update,
)

# ── Tool Schemas (File I/O) ──

#: Syntactic shape gate for a filesystem path arriving over the dashboard's
#: file endpoints. It admits POSIX (``/x``, ``~/x``) *and* native Windows
#: (``C:\x``, ``C:/x``, UNC ``\\host\share\x``) absolute paths. A prefix is
#: still required, so a bare relative path is still refused --
#: the endpoints that support relative input rewrite it to an absolute path
#: via ``_resolve_project_relative`` under ``resolve=1``, ahead of this gate.
#:
#: One pattern rather than a ``sys.platform`` branch: a drive letter and a UNC
#: root have no meaning on POSIX, so accepting those shapes there grants
#: nothing, and a single pattern cannot drift
#: between platforms the way two would. This is a *syntax* gate only -- the
#: security boundary is downstream, where ``hooks.validate_file_path``
#: canonicalizes through ``realpath`` (resolving ``..`` and following symlinks)
#: and refuses the resolved target via ``is_sensitive_path``.
#:
#: The prefix alternation is matched separately from the body so that ``:``
#: stays confined to a drive prefix: an NTFS alternate data stream
#: (``C:\x\file.txt:hidden``, which reads a different byte stream than the path
#: the caller appears to name) has a ``:`` in the body and is still refused.
#: A drive-relative path (``C:x``) is likewise refused -- it resolves against a
#: per-drive working directory the caller cannot see.
#:
#: The body is a DENYLIST, not an allowlist of punctuation. Every filesystem
#: this gate fronts accepts any byte but the separator and NUL in a name, so an
#: enumerated allowlist refuses legal files -- ``Notes (draft).md``,
#: ``Q1 2026 #2.md``, ``50% done.md``, ``report [final].md`` -- and returns
#: HTTP 400 for each. Only two classes are hazards in the path *string* itself
#: and both stay refused: a control character, which truncates at NUL, splits a
#: log line at CR/LF, and injects a terminal escape at ESC or at the 8-bit C1
#: forms a UTF-8 terminal decodes the same way (U+0085 NEL, U+009B CSI); and
#: ``:`` in the body, for the alternate-data-stream reason above. The deny range
#: therefore spans C0, DEL and C1 -- no filesystem name legitimately carries a
#: control character, so the wider range costs nothing. Nothing else is excluded,
#: because nothing downstream interprets the string: every subprocess in the
#: file handlers is ``exec``-form argv with no shell, and the required absolute
#: prefix means the path can never be read as a leading-dash option.
_FS_PATH_PATTERN = re.compile(r"^(?:[~/]|[A-Za-z]:[\\/]|\\\\)[^\x00-\x1f\x7f-\x9f:]+$")

FILE_READ_SCHEMA = ToolSchema(
    tool_name="file_read",
    fields=[
        FieldSpec("path", str, required=True, max_len=4096, pattern=_FS_PATH_PATTERN),
    ],
)

FILE_WRITE_SCHEMA = ToolSchema(
    tool_name="file_write",
    fields=[
        FieldSpec("path", str, required=True, max_len=4096, pattern=_FS_PATH_PATTERN),
        FieldSpec("content", str, required=True, max_len=512000),
    ],
)
