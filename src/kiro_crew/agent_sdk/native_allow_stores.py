"""Each harness's own permission files: the ones that can let it run a tool unasked.

A harness reads these when it starts and honours what they allow, so whoever can
write one decides what a later session may run without a permission prompt. Each
entry is on the write-only tier the way ``~/.kiro/agents`` is: the agent's
file-edit tools are refused (``security.paths_lists._WRITE_PROTECTED_HOME_PATHS``,
anchored under each home variable below) and reads are unchanged. ``agent.env``
refuses those variables, so a store cannot move for the child alone. A shell write
is not fenced here.

``test/test_native_allow_stores.py`` fails when a harness in ``ACP_BACKENDS_KNOWN``
has neither a row here nor a reason in :data:`NATIVE_ALLOW_STORES_NONE`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)

__all__ = [
    "NATIVE_ALLOW_STORES",
    "NATIVE_ALLOW_STORES_NONE",
    "PROJECT_ALLOW_STORES",
    "NativeAllowStore",
    "home_leaves",
    "matches_store",
    "override_anchored_leaves",
    "override_env_vars",
]


@dataclass(frozen=True)
class NativeAllowStore:
    """One permission file (or directory) a harness reads from the user's home."""

    #: ``$HOME``-relative, POSIX separators.
    leaf: str
    #: ``(variable, spelling under it)`` for each variable the harness reads to move
    #: this path.
    overrides: tuple[tuple[str, str], ...] = ()


def _claude(name: str) -> NativeAllowStore:
    return NativeAllowStore(f".claude/{name}", (("CLAUDE_CONFIG_DIR", name), ("CLAUDE_HOME", name)))


def _goose(name: str) -> NativeAllowStore:
    # goose reads GOOSE_PATH_ROOT first (``acp.harness.goose.goose_config_dir``).
    return NativeAllowStore(
        f".config/goose/{name}",
        (("GOOSE_PATH_ROOT", f"config/{name}"), ("XDG_CONFIG_HOME", f"goose/{name}")),
    )


def _pi(name: str) -> NativeAllowStore:
    return NativeAllowStore(f".pi/agent/{name}", (("PI_CODING_AGENT_DIR", name),))


NATIVE_ALLOW_STORES: Mapping[str, tuple[NativeAllowStore, ...]] = {
    ACP_BACKEND_CLAUDE: (_claude("settings.json"), _claude("settings.local.json")),
    # permission.yaml: tools under user.always_allow run without a permission request.
    # config.yaml: its extensions block decides what starts at the next spawn.
    ACP_BACKEND_GOOSE: (_goose("permission.yaml"), _goose("config.yaml")),
    ACP_BACKEND_OPENCODE: tuple(
        NativeAllowStore(f".config/opencode/{name}", (("XDG_CONFIG_HOME", f"opencode/{name}"),))
        for name in ("opencode.json", "opencode.jsonc", "config.json")
    ),
    ACP_BACKEND_CODEX: (NativeAllowStore(".codex/config.toml", (("CODEX_HOME", "config.toml"),)),),
    # extensions: code pi loads into its own process.
    ACP_BACKEND_PI: (_pi("settings.json"), _pi("extensions")),
    # Its permission settings (allowed tools and URLs, trusted folders, its own hooks)
    # live in config.json, which is already refused to the file tools for reading and
    # writing as its credential leaf (``host_auth``), so it is not listed again here.
    # mcp-config.json: its MCP servers, started at the next spawn without a prompt.
    # installed-plugins: plugins it loads, whose hooks and MCP servers run unasked.
    ACP_BACKEND_COPILOT: tuple(
        NativeAllowStore(f".copilot/{name}", (("COPILOT_HOME", name),))
        for name in ("mcp-config.json", "installed-plugins")
    ),
}

#: Harnesses with no row above, and why.
NATIVE_ALLOW_STORES_NONE: Mapping[str, str] = {
    ACP_BACKEND_KIRO: "its stores, ~/.kiro/agents and ~/.kiro/settings/mcp.json, are "
    "fenced by name in security.paths_lists",
    ACP_BACKEND_KAS: "it runs kiro-cli's engine and reads the same stores as kiro",
    ACP_BACKEND_DEEPSEEK: "no on-disk allow list is known; every call goes through "
    "Crew's gate extension",
}

#: A project's own harness config, refused for the file-edit tools in any directory:
#: each harness reads it from the session's work dir or a parent at launch. A file
#: entry matches as a path's last segments; a trailing ``/`` marks a directory,
#: matched with everything under it.
PROJECT_ALLOW_STORES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    # opencode: config found walking up from the work dir, and its .opencode dirs
    # (config, agents, plugins, custom tools).
    "opencode.json",
    "opencode.jsonc",
    ".opencode/",
    # pi, once the user trusts the folder: settings (packages, extension paths),
    # MCP servers, and extensions loaded into its process.
    ".pi/settings.json",
    ".pi/mcp.json",
    ".pi/extensions/",
)


def _stores() -> tuple[NativeAllowStore, ...]:
    return tuple(store for stores in NATIVE_ALLOW_STORES.values() for store in stores)


def home_leaves() -> tuple[str, ...]:
    """Every entry's ``$HOME``-relative leaf, for the write-protected list."""
    return tuple(dict.fromkeys(store.leaf for store in _stores()))


def override_anchored_leaves() -> tuple[tuple[str, tuple[str, ...], str], ...]:
    """``(leaf, (variable,), spelling)`` in the shape ``host_auth`` projects."""
    return tuple(
        (store.leaf, (env,), spelling) for store in _stores() for env, spelling in store.overrides
    )


def override_env_vars() -> tuple[str, ...]:
    """Every variable that moves a store, so the gate resolves its root."""
    return tuple(dict.fromkeys(env for store in _stores() for env, _ in store.overrides))


def matches_store(path_str: str, base_dir: str | None = None) -> bool:
    """Whether any form of *path_str* is a project's own harness config.

    These sit in whatever directory a session works in, so a file entry is matched
    by its last segments and a directory entry by its segments anywhere in the path.
    The ``$HOME`` stores, their home-variable spellings and a symlinked config dir are
    anchored by ``security.paths_targets`` like the kiro-cli leaves. Called from
    ``security.paths.sensitive_write_path_refusal``.
    """
    # Circular import: security.paths_lists imports this module.
    from kiro_crew.security.paths_targets import _candidate_forms

    if not path_str:
        return False
    files = [_parts(rel) for rel in PROJECT_ALLOW_STORES if not rel.endswith("/")]
    dirs = [_parts(rel) for rel in PROJECT_ALLOW_STORES if rel.endswith("/")]
    for cand in _candidate_forms(path_str, base_dir):
        segs = tuple(re.split(r"[\\/]+", cand.casefold().rstrip("/\\")))
        if any(segs[-len(tail) :] == tail for tail in files):
            return True
        if any(
            segs[i : i + len(run)] == run for run in dirs for i in range(len(segs) - len(run) + 1)
        ):
            return True
    return False


def _parts(rel: str) -> tuple[str, ...]:
    return tuple(PurePosixPath(rel.casefold()).parts)
