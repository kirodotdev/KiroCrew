"""Value checks for the free-text keys Settings -> Advanced may change.

``config_all_settings.generic_value_error`` runs the check named for a key after
the schema's own type check, so each one sees the type the schema declares: a
``str``, a ``list`` of them for a string-list key, or an ``int``. A check returns
a plain sentence saying what is wrong, or ``None``. ``""`` passes for a single
text value unless the check says otherwise: it clears the key to the reader's
default.
"""

from __future__ import annotations

import os
import re
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


# ── Checks for the keys editable with a risk line ──

_HOST_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*"
)
_PORT_RE = re.compile(r"[0-9]{1,5}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Raster only: an SVG can carry script, and the logo route serves this file to
# anyone who can open the sign-in page.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico")


def _host_error(item: str, *, port: bool) -> str | None:
    host, sep, rest = item.rpartition(":") if port and ":" in item else (item, "", "")
    if sep and not (_PORT_RE.fullmatch(rest) and 0 < int(rest) < 65536):
        return f"{item!r} has a port that is not a number from 1 to 65535"
    if not _HOST_RE.fullmatch(host):
        shape = "a host name such as git.example.com" + (" or git.example.com:8443" if port else "")
        return f"{item!r} must be {shape}, with no scheme or path"
    return None


def _hosts(port: bool) -> Check:
    def check(value: object) -> str | None:
        for item in value if isinstance(value, list) else []:
            reason = _host_error(item, port=port)
            if reason:
                return reason
        return None

    return check


def _host(value: object) -> str | None:
    text = str(value)
    return None if text == "" else _host_error(text, port=False)


def _image_path(value: object) -> str | None:
    """The logo file: an absolute path to an image."""
    text = str(value)
    reason = _path_error(text)
    if reason or text == "":
        return reason
    if not text.lower().endswith(_IMAGE_SUFFIXES):
        return "must be a .png, .jpg, .gif, .webp or .ico image"
    return None


def _https_or_loopback_url(value: object) -> str | None:
    """The relay reader's rule: https://, or http:// only to this machine's loopback.

    A credential rides with each call to these addresses, so it never travels
    unencrypted off the machine.
    """
    from kiro_crew.relay_client.lease import lease_url_problem

    text = str(value)
    if text == "":
        return None
    reason = lease_url_problem(text)
    return reason.replace("the lease service URL", "the address") if reason else None


def _relay_token_type(value: object) -> str | None:
    from kiro_crew.relay_client.lease import TOKEN_TYPES

    allowed = sorted(TOKEN_TYPES)
    if value == "" or value in TOKEN_TYPES:
        return None
    return f"must be empty or one of {allowed}"


def _scope_prefix(value: object) -> str | None:
    from kiro_crew.auth.login.endpoints import OIDC_SCOPE_PREFIX_RE

    if value == "" or OIDC_SCOPE_PREFIX_RE.fullmatch(str(value)):
        return None
    return "must be lower-case letters and underscores"


def _env_names(value: object) -> str | None:
    for item in value if isinstance(value, list) else []:
        if not _ENV_NAME_RE.fullmatch(item):
            return f"{item!r} must be an environment variable name such as AWS_PROFILE"
    return None


def _sha256(value: object) -> str | None:
    if value == "" or _SHA256_RE.fullmatch(str(value)):
        return None
    return "must be the 64-character lower-case sha256 fingerprint the refused session printed"


def _subagent_timeout(value: object) -> str | None:
    """0 (the default) or the loader's own range, so a save never reads back raised."""
    from kiro_crew.constants import SUBAGENT_TIMEOUT_MIN

    if value == 0 or (isinstance(value, int) and value >= SUBAGENT_TIMEOUT_MIN):
        return None
    return f"must be 0 for the default, or at least {SUBAGENT_TIMEOUT_MIN} seconds"


#: The check each key editable with a risk line runs, when its type needs one.
RISK_CHECKS: dict[str, Check] = {
    "agent.sandbox_readonly_paths": _path_list,
    "agent.sandbox_readonly_exceptions": _path_list,
    "agent.subagent_cwd_allowed_roots": _path_list,
    "mcp.extra_path_dirs": _path_list,
    "publish.relocate_roots": _path_list,
    "mcp_gateway.overlay_dir": _path,
    "mcp_gateway.socket_path": _path,
    "agent.strands_project": _path,
    "agent.strands_project_sha256": _sha256,
    "agent.subagent_timeout_secs": _subagent_timeout,
    "dashboard.avatar": _image_path,
    "dashboard.github_hosts": _hosts(port=False),
    "dashboard.gitlab_hosts": _hosts(port=True),
    "dashboard.jira_hosts": _hosts(port=True),
    "dashboard.bitbucket_server_hosts": _hosts(port=True),
    "dashboard.gerrit_hosts": _hosts(port=True),
    "knowledge.doc_ingest_hosts": _hosts(port=False),
    "tunnel.relay_handle_email_domains": _hosts(port=False),
    "tunnel.relay_viewer_relay_domain": _host,
    "tunnel.relay_lease_url": _https_or_loopback_url,
    "tunnel.relay_viewer_cps_url": _https_or_loopback_url,
    "tunnel.relay_lease_token_type": _relay_token_type,
    "tunnel.relay_viewer_oidc_scope_prefix": _scope_prefix,
    "mcp_gateway.pool_identity_env": _env_names,
    "dashboard.tailscale.allowed_logins": _line_list,
    "publish.allowed_destinations": _line_list,
    "telemetry.beacon_endpoint": _https_url,
    "wakatime.api_base_url": _https_or_loopback_url,
    "wakatime.oauth_client_id": _line,
}
