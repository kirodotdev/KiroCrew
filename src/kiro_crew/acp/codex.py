"""Codex-specific paths, resolution, and ACP protocol shaping.

Kiro Crew's PreToolUse gate — the bundled denied-command rules, the
sensitive-path block, the governance ceiling — runs from exactly one place,
``HookManager.on_tool_call``, reached only from the permission-request branch of
the dispatch parser. kiro-cli is made to ask because the spawn names an agent;
claude-agent-acp is made to ask by a settings file Kiro Crew writes. codex-acp
is made to ask by ACP v1 session config ``mode=read-only``, which Kiro Crew
applies after ``session/new`` / ``session/load`` and refuses if the adapter
does not advertise that value or the write fails.

Nothing here reads or writes a credential. ``codex login`` owns the ChatGPT
OAuth flow and persists its own tokens; Kiro Crew only checks whether that file
exists, so it can name the right sign-in command instead of kiro-cli's.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class Verdict(str, Enum):
    """Whether a backend's tool decisions reach Kiro Crew's gate.

    INDETERMINATE is deliberately NOT a synonym for "probably fine". A
    guarantee that lapses whenever a file is unreadable is not a guarantee, so
    the enforcement treats it exactly like BYPASSED. It is a distinct value only
    so the operator-facing message can say "could not determine" rather than
    asserting a policy the file never contained.
    """

    ROUTED = "routed"
    BYPASSED = "bypassed"
    INDETERMINATE = "indeterminate"


CODEX_ACP_BIN = "codex-acp"

# Sentinel distinguishing "never resolved" from "resolved to nothing", so a
# failed resolution is retried on the next spawn instead of being cached.
_UNRESOLVED: object = object()
_argv_cache: object = _UNRESOLVED


def resolve_argv() -> list[str] | None:
    """Find the standalone codex-acp adapter and return its argv.

    Ladder, in order:

    1. ``CODEX_ACP_BIN`` — explicit operator override. Honoured even when not
       executable, since a bare ``.js`` entry is wrapped with node.
    2. ``mise which codex-acp`` — respects ``MISE_DATA_DIR`` and all mise config.
    3. The augmented PATH — mise shims, nvm, fnm, volta, ``npm i -g``.

    A candidate whose co-located Node is older than
    :data:`~kiro_crew.acp.client._MIN_ADAPTER_NODE_MAJOR` is SKIPPED rather than
    returned, so the ladder keeps walking. This matters because rung 2 honours
    mise's ACTIVE node, which on a host with several installs can be the EOL one:
    measured here, ``mise which`` returned a Node 16 copy while the augmented PATH
    listed Node 24 first, and the Node 16 pairing crashes before answering
    ``initialize``.

    There is deliberately **no** ``codex acp`` subcommand rung. The Codex CLI does
    not serve ACP: it treats ``acp`` as a prompt, so spawning it that way starts an
    ordinary chat turn against the operator's subscription instead of an ACP
    server, and the failure surfaces as a protocol timeout rather than a missing
    binary. Only the standalone adapter speaks ACP.
    """
    # Imported here rather than at module scope: these live in acp.client, which
    # imports this module's sibling tool_gate, and a top-level import would be a
    # cycle. They are the same helpers the claude ladder uses, so the two
    # backends resolve node and normalise casing identically.
    from kiro_crew import platform_compat
    from kiro_crew.acp.client import (
        _mise_which,
        _normalize_exe_casing,
        _ordered_path_matches,
        _resolve_node_for_script,
        _script_runtime_is_supported,
    )
    from kiro_crew.env import augmented_path

    candidates: list[str] = []

    override = os.environ.get("CODEX_ACP_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    mise_resolved = _mise_which(CODEX_ACP_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    search_path = augmented_path(os.environ.get("PATH", ""))
    # Every match, best runtime first — not `shutil.which`'s first hit, which on
    # a mise host is the shim whose node is whatever version happens to be active.
    candidates.extend(_ordered_path_matches(CODEX_ACP_BIN, search_path))

    for script in candidates:
        resolved = str(Path(script).resolve())
        # Skip the whole candidate, not just its node pairing: an EOL-tree
        # candidate is typically an executable shim pointing back at that same
        # EOL node, so a pairing-only check falls through and re-acquires it.
        if not _script_runtime_is_supported(resolved):
            logger.debug("Skipping %s: Node runtime below the adapter floor", resolved)
            continue
        node = _resolve_node_for_script(resolved)
        if node:
            return [node, resolved]
        if platform_compat.is_executable_file(script):
            return [_normalize_exe_casing(script) or script]
        node_on_path = shutil.which("node", path=search_path)
        if node_on_path:
            return [node_on_path, resolved]

    return None


def resolve_argv_cached() -> list[str] | None:
    """``resolve_argv`` memoising only SUCCESS.

    A failed resolution is not cached, so installing the adapter (or exporting
    ``CODEX_ACP_BIN``) takes effect on the next spawn without restarting the
    gateway. The cost is re-probing the ladder on each failed attempt, which is
    the right trade: the failure path is already terminal for that session.
    """
    global _argv_cache  # noqa: PLW0603
    if _argv_cache is not _UNRESOLVED:
        return _argv_cache  # type: ignore[return-value]
    resolved = resolve_argv()
    if resolved:
        _argv_cache = resolved
    return resolved


def missing_adapter_message() -> str:
    """What to tell an operator whose host has no adapter."""
    return (
        "No Codex ACP adapter found. Install the standalone `codex-acp` adapter, "
        "or set CODEX_ACP_BIN to its path. Note that the `codex` CLI itself does "
        "not serve ACP. Then sign in with `codex login`."
    )


#: Sign-in readings. ``UNKNOWN`` is the load-bearing one: no persisted token was
#: found, but that is NOT proof the adapter cannot authenticate.
SIGNIN_PRESENT = "present"
SIGNIN_UNKNOWN = "unknown"


def signin_state() -> str:
    """Whether ``codex login`` has persisted tokens, or whether we cannot tell.

    Never returns a negative. ``auth.json`` is ONE of the adapter's credential
    channels, so its absence does not mean unauthenticated: a real turn has been
    driven through AcpClient on a host where this file did not exist and no
    key-shaped environment override was set either, so the adapter reached a
    credential some other way. Reporting "not signed in" there sends an operator
    to run ``codex login`` to fix a problem they do not have — the same
    false-negative trap the install probe already avoids, which is why this
    mirrors its three-state shape rather than returning bool.

    Existence only. Kiro Crew never opens this file: it holds a live ChatGPT OAuth
    access and refresh pair, the adapter reads it in-process, and the path is on
    the sensitive-path list precisely so nothing else does.
    """
    try:
        return SIGNIN_PRESENT if auth_json_path().is_file() else SIGNIN_UNKNOWN
    except OSError:
        return SIGNIN_UNKNOWN


def not_signed_in_message() -> str:
    """The non-retryable auth error text, naming Codex's own command.

    Names ``codex login`` rather than ``kiro-cli login``: telling an operator on a
    Codex-only host to authenticate a CLI they may not have installed is the
    failure this whole backend-aware error path exists to prevent.
    """
    return f"Codex is not logged in. {signin_hint()}"


# Keys a strict spec-adapter deserializer accepts on an mcpServers entry. Kiro's
# passthrough keys (autoApprove, timeout, vendor keys) make a Rust serde
# deserializer reject the WHOLE session/new, not just the offending entry.
SPEC_STDIO_SERVER_KEYS = frozenset({"name", "command", "args", "env"})

# A spec adapter accepts only these characters in an MCP server name.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_UNSAFE_RUN_RE = re.compile(r"[^A-Za-z0-9_-]+")
_NAME_HASH_LEN = 6


def reserved_managed_names() -> frozenset[str]:
    """Names a user-configured server must never be allowed to sanitise onto.

    Fails CLOSED. The reference implementation returns an empty set when the
    managed table cannot be read, which is the dangerous direction: with nothing
    reserved, a user-configured ``kirocrew core`` sanitises to ``kirocrew-core``
    and impersonates the trusted managed server, inheriting whatever trust the
    declared name carries.
    """
    from kiro_crew.agent import _MANAGED_MCP_SERVERS

    return frozenset(_MANAGED_MCP_SERVERS)


def safe_server_name(name: str, taken: set[str]) -> str:
    """Coerce ``name`` into the adapter's charset without colliding.

    Collisions are resolved with a widening hash of the ORIGINAL name, so two
    different inputs that sanitise to the same base stay distinguishable rather
    than one silently shadowing the other.
    """
    if _SAFE_NAME_RE.fullmatch(name) and name not in taken:
        return name

    base = _UNSAFE_RUN_RE.sub("-", name).strip("-")
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    if not base:
        base = digest[:_NAME_HASH_LEN]
    candidate = base
    width = _NAME_HASH_LEN
    while candidate in taken and width <= len(digest):
        candidate = f"{base}-{digest[:width]}"
        width += 2
    return candidate


def reshape_managed_servers() -> list[dict]:
    """Deprecated alias for :func:`kiro_crew.acp.spec_servers.managed_spec_servers`.

    Kept so existing callers and tests keep resolving. The implementation moved to
    the dialect-neutral :mod:`kiro_crew.acp.spec_servers` because claude and goose
    need the same shaping — and it was CORRECTED on the way: this version omitted
    ``env`` when empty (which ``McpServerStdio`` requires, so a default install
    would have failed deserialization), injected ``kirocrew-computer`` without
    consulting its ``spec_gate``, and delivered the ``opt_in``
    ``kirocrew-dashboard`` set to every session. None of that was ever observed
    because nothing called this function.
    """
    from kiro_crew.acp.spec_servers import managed_spec_servers

    return managed_spec_servers()


def reduce_to_spec_keys(entry: dict) -> dict:
    """Deprecated alias for :func:`kiro_crew.acp.spec_servers.reduce_to_spec_keys`.

    The moved version additionally DEFAULTS the required ``args`` and ``env``
    fields, which ``McpServerStdio`` demands even when empty.
    """
    from kiro_crew.acp.spec_servers import reduce_to_spec_keys as _reduce

    return _reduce(entry)


def merge_session_servers(managed: list[dict], pooled: list[dict]) -> list[dict]:
    """Deprecated alias for :func:`kiro_crew.acp.spec_servers.merge_session_servers`."""
    from kiro_crew.acp.spec_servers import merge_session_servers as _merge

    return _merge(managed, pooled)


# Codex advertises reasoning effort as part of the model id — one advertised row
# per level, spelled ``<base>[<effort>]`` — rather than as a separate config
# option. So a picked id must be split before it goes on the wire: the composite
# form is answered -32602.
_COMPOSITE_MODEL_RE = re.compile(r"^(?P<base>[^\[\]]+)\[(?P<effort>[^\[\]]+)\]$")


def split_model_id(model_id: str) -> tuple[str, str]:
    """Split ``<base>[<effort>]`` into its parts.

    Returns ``(base, effort)``, with an empty effort when the id carries no
    suffix. A malformed id is returned unchanged as the base, because guessing at
    a repair would send something the adapter never advertised.
    """
    match = _COMPOSITE_MODEL_RE.match(model_id.strip())
    if not match:
        return (model_id.strip(), "")
    return (match.group("base").strip(), match.group("effort").strip())


def base_model_id(model_id: str) -> str:
    """The model id with any advertised effort suffix removed."""
    return split_model_id(model_id)[0]


def advertised_effort(model_id: str) -> str:
    """The effort level carried in an advertised composite id, or ``""``."""
    return split_model_id(model_id)[1]


def wire_model_id(model_id: str, *, is_default: bool) -> str:
    """The value to send in ``session/set_config_option("model", ...)``.

    ``""`` for the default, which the caller must treat as "reset the session
    rather than push a value": Codex has no id meaning "your own default", so
    there is nothing to send that would restore it on a live session.

    Otherwise the effort-stripped base id. The suffix is the caller's business —
    it routes to the effort control — and leaving it on would earn a -32602 that
    reads as "model unavailable" rather than "that is not a model id".
    """
    if is_default or not model_id.strip():
        return ""
    return base_model_id(model_id)


def is_composite_advertisement(model_ids: list[str]) -> bool:
    """Whether an advertised list uses the ``<base>[<effort>]`` shape.

    Reads the advertised list rather than the backend id on purpose for
    DIAGNOSIS, but callers deciding behaviour should key on the backend
    descriptor instead: the reference implementation identified codex by this
    shape, so any future backend advertising the same shape silently inherited
    codex's entitlement comparison.
    """
    return any(_COMPOSITE_MODEL_RE.match(model_id or "") for model_id in model_ids)


def codex_home() -> Path:
    """``$CODEX_HOME``, else the documented default ``~/.codex``."""
    override = os.environ.get("CODEX_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def auth_json_path() -> Path:
    """Where ``codex login`` persists its ChatGPT OAuth tokens."""
    return codex_home() / "auth.json"


def signin_hint() -> str:
    """Sign-in guidance, including the headless case."""
    return (
        "Run `codex login` (ChatGPT-subscription OAuth, no API key) on this host "
        "and complete the browser flow. On a headless host, forward the OAuth "
        "callback port the login flow prints, or copy an existing "
        f"{auth_json_path()} to this machine."
    )


__all__ = [
    "CODEX_ACP_BIN",
    "SPEC_STDIO_SERVER_KEYS",
    "Verdict",
    "advertised_effort",
    "auth_json_path",
    "base_model_id",
    "codex_home",
    "is_composite_advertisement",
    "signin_state",
    "SIGNIN_PRESENT",
    "SIGNIN_UNKNOWN",
    "merge_session_servers",
    "missing_adapter_message",
    "not_signed_in_message",
    "reduce_to_spec_keys",
    "reserved_managed_names",
    "reshape_managed_servers",
    "resolve_argv",
    "resolve_argv_cached",
    "safe_server_name",
    "signin_hint",
    "split_model_id",
    "wire_model_id",
]
