"""Value checks for the free-text keys Settings -> Advanced may change.

``config_all_settings.generic_value_error`` runs the check named for a key after
the schema's own type check, so each one sees a ``str`` (or a ``list`` of them for
a string-list key). A check returns a plain sentence saying what is wrong, or
``None``. ``""`` always passes for a single value: it clears the key to the
reader's default.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable

Check = Callable[[object], "str | None"]


def _one_line_error(value: str) -> str | None:
    if any(not ch.isprintable() for ch in value):
        return "must be one line of plain text"
    return None


def _path_error(value: str) -> str | None:
    """A directory or file path: ``~`` is expanded, then it must be absolute."""
    if value == "":
        return None
    if _one_line_error(value):
        return "must be one line, with no control characters"
    if not os.path.isabs(os.path.expanduser(value)):
        return "must be an absolute path, such as /home/me/work or ~/work"
    return None


def _url_error(value: str, schemes: tuple[str, ...]) -> str | None:
    """A base URL: the scheme is one of *schemes*, it names a host, and it carries
    no user name or password."""
    if value == "":
        return None
    shape = " or ".join(f"{s}://" for s in schemes)
    if _one_line_error(value):
        return f"must be one URL starting with {shape}"
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port  # raises ValueError on a port that is not a number
    except ValueError:
        return f"must be a URL starting with {shape}"
    if parts.scheme not in schemes:
        return f"must start with {shape}"
    if not parts.hostname:
        return "must name a host after the scheme"
    if parts.username is not None or parts.password is not None:
        return "must not carry a user name or password"
    return None


def _http_url(value: object) -> str | None:
    return _url_error(str(value), ("http", "https"))


def _https_url(value: object) -> str | None:
    return _url_error(str(value), ("https",))


def _ollama_url(value: object) -> str | None:
    """The Ollama reader's own rule, with a remote host allowed: whether text may
    leave this machine is ``memory.ollama_allow_remote``'s decision, at use."""
    from kiro_crew.embeddings_ollama import _url_error as ollama_url_error

    text = str(value)
    if text == "":
        return None
    reason = ollama_url_error(text, allow_remote=True)
    return reason.removeprefix("memory.ollama_url ") or None


def _path(value: object) -> str | None:
    return _path_error(str(value))


def _path_list(value: object) -> str | None:
    for item in value if isinstance(value, list) else []:
        reason = _path_error(item)
        if reason:
            return f"{item!r} {reason}"
    return None


def _line(value: object) -> str | None:
    return _one_line_error(str(value))


def _name(value: object) -> str | None:
    """A name its reader looks up: one line, and never empty."""
    if value == "":
        return "must not be empty; use Reset to go back to the default"
    return _one_line_error(str(value))


def _line_list(value: object) -> str | None:
    for item in value if isinstance(value, list) else []:
        if item == "" or _one_line_error(item):
            return "each item must be one line of plain text"
    return None


def _dir(value: object) -> str | None:
    """A folder: a path, and never one that names an existing file.

    A missing folder passes; its reader creates it or reports it at use.
    """
    reason = _path(value)
    if reason or value == "":
        return reason
    if os.path.isfile(os.path.expanduser(str(value))):
        return "must be a folder, not a file"
    return None


def _branch_prefix(value: object) -> str | None:
    """Parts a git branch name may hold, as ``coerce_branch_prefix`` reads them."""
    from kiro_crew.crewmate_worktree import _name

    text = str(value)
    if text == "":
        return None
    parts = text.removesuffix("/").split(sep="/")
    if all(part and _name(part) == part for part in parts):
        return None
    return (
        "must be parts of letters, digits, '.', '_' and '-' split by '/', "
        "such as crew/ or alice/"
    )


def _member_backend(value: object) -> str | None:
    """``""`` (follow ``agent.acp_backend``) or a harness this build can run."""
    from kiro_crew.agent_sdk.backends import selectable_stored_backend_values

    allowed = selectable_stored_backend_values()
    if value == "" or value in allowed:
        return None
    return f"must be empty or one of {allowed}"


def _dispatch_tags(value: object) -> str | None:
    """Words this crew advertises to a dispatching hub, in its reader's own shape."""
    from kiro_crew.work_vocab import DISPATCH_TAG_RE, MAX_DISPATCH_TAGS

    items = value if isinstance(value, list) else []
    if len(items) > MAX_DISPATCH_TAGS:
        return f"must hold at most {MAX_DISPATCH_TAGS} tags"
    for item in items:
        if not DISPATCH_TAG_RE.fullmatch(item):
            return f"{item!r} must be a short lower-case word such as gpu or docker"
    return None


#: The check each free-text key that Settings -> Advanced may change runs.
TEXT_CHECKS: dict[str, Check] = {
    # Paths: a leading ~ is expanded, and the result must be absolute.
    # Folders: absolute after ~ is expanded, and never an existing file.
    "dashboard.default_project": _dir,
    "dashboard.terminal.cwd": _dir,
    "snapshot_dir": _dir,
    "taskrunner.workspace_dir": _dir,
    "telemetry.local_dir": _dir,
    # Addresses. The download keys are sha256-checked whatever the host, and
    # their readers refuse anything but https.
    "dashboard.url": _http_url,
    "agent.kas_bundle_url": _https_url,
    "memory.embed_model_url": _https_url,
    "memory.ollama_url": _ollama_url,
    # Names and labels: one line each.
    "agent.bot_name": _line,
    "dashboard.bot_name": _line,
    "agent.crew_mode_agent": _line,
    "default_workspace": _name,
    "default_memory_store": _name,
    "dashboard.tips_model": _line,
    "skills.judge_model": _line,
    "memory.ollama_model": _line,
    "tunnel.name_override": _line,
    "agent.member_branch_prefix": _branch_prefix,
    "agent.member_acp_backend": _member_backend,
    "memory.semantic_keys": _line_list,
    "session.idle_exempt_keys": _line_list,
    # An argument list run with no shell: each item is free text.
    "taskrunner.test_command": _line_list,
    "work_ledger.remote_dispatch_tags": _dispatch_tags,
}


def text_max_len() -> dict[str, int]:
    """Free-text keys whose reader keeps more than the page's usual 256 characters."""
    from kiro_crew.config.memory_sections import EXTRACTION_INSTRUCTIONS_MAX_CHARS

    # Free text in the person's own words; its reader keeps newlines and tabs.
    return {"memory.extraction_instructions": EXTRACTION_INSTRUCTIONS_MAX_CHARS}


def stored_value(check: Check, value: object) -> object:
    """*value* as it is saved once *check* passed it: a path's ``~`` expanded.

    Not every reader of a path expands ``~`` (a terminal's start folder is
    handed to the process as is), so the page saves the absolute path it checked.
    """
    if check in (_path, _dir) and isinstance(value, str) and value:
        return os.path.expanduser(value)
    if check is _path_list and isinstance(value, list):
        return [os.path.expanduser(item) for item in value]
    return value
