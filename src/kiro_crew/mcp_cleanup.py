"""Shared MCP config cleanup utilities.

KiroCrew does NOT write KiroCrew-managed MCP servers to the user's global
provider MCP config (``~/.kiro/settings/mcp.json``) during normal
operation — the KiroCrew agent file is authoritative, and provider
globals are user-owned.  Remaining helpers here clean up stale
kirocrew-binary entries left over from older install methods.

Extracted from agent.py so both agent.py and cli.py can import at the
top level without circular dependencies (agent.py imports cli.py).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from kiro_crew.config.paths import kiro_home

logger = logging.getLogger(__name__)

# Override hook + accessor, NOT a resolved constant (issue #874). Binding
# `kiro_home()` at import time froze whichever home was active when this module
# was first imported, so conftest's isolation fixture -- which runs after
# collection has already imported it -- could not redirect it, and a test that
# reached `clean_stale_managed_mcp()` without patching would rewrite the
# operator's REAL mcp.json. Keeping the module-level name means the existing
# `monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", tmp)` call sites still work.
_KIRO_MCP_JSON: Path | None = None  # explicit override hook, None = live


def _kiro_mcp_json() -> Path:
    if _KIRO_MCP_JSON is not None:
        return _KIRO_MCP_JSON
    return kiro_home() / "settings" / "mcp.json"


# Managed servers whose command is the kirocrew binary itself.
# Only these are affected by install-method path changes.
# Ordered tuples (not sets) so consumers that iterate — e.g. `kirocrew
# doctor`'s MCP probe — get a deterministic order.
#
# The split matters to every consumer that asks "should this server be in the
# spec?". An ALWAYS_ON server missing from an agent spec is a broken install; an
# OPT_IN one is an assignable set that most agents are simply not granted, so
# demanding its presence — or minting an auto-approve grant for it — would undo
# the assignment. Membership here must track the ``opt_in`` flags in
# ``agent._MANAGED_MCP_SERVERS``; a ratchet test pins the two together.
ALWAYS_ON_BIN_MCP_SERVERS = (
    "kirocrew-cron",
    "kirocrew-core",
    "kirocrew-computer",
)
OPT_IN_BIN_MCP_SERVERS = ("kirocrew-dashboard",)

# Every managed-binary server name, regardless of how it reaches a spec. This is
# the cleanup view: Kiro Crew never legitimately writes any of them into the
# user's global mcp.json, so a stray entry is purgeable either way.
KIROCREW_BIN_MCP_SERVERS = ALWAYS_ON_BIN_MCP_SERVERS + OPT_IN_BIN_MCP_SERVERS

# MeshClaw was the predecessor of KiroCrew. The rename left these managed
# server entries — pointing at now-dead MeshClaw build paths — behind in the
# user's global provider config; they are unambiguously stale and safe to purge.
PREDECESSOR_BIN_MCP_SERVERS = frozenset({"meshclaw-cron", "meshclaw-core"})

# Every managed-binary server name KiroCrew is responsible for removing from
# the user's global mcp.json (Kiro Crew never legitimately writes these there).
#
# ALWAYS_ON only, and there is no ownership-proven exception. This purge exists
# to reclaim entries an OLDER INSTALL METHOD wrote to the global file — but an
# opt-in server is never written there by any version of Kiro Crew, since the
# only way it is ever granted is by hand. So no legitimate residue can exist
# under that name, and anything found there is necessarily the user's own: to be
# left alone, not reclaimed on a technicality about how it happens to be spelled.
STALE_MANAGED_MCP_SERVERS = frozenset(ALWAYS_ON_BIN_MCP_SERVERS) | PREDECESSOR_BIN_MCP_SERVERS


# The argv token the deleted Playwright MCP proxy was registered with. An entry
# still carrying it spawns `kirocrew mcp-playwright-proxy`, a subcommand this
# release removed, so kiro-cli hits ModuleNotFoundError on EVERY session until
# the entry goes. Browsing is gone either way (there is no proxy any more), so
# the only question is whether the operator also gets a crash on every session.
_DELETED_PROXY_ARGV_TOKEN = "mcp-playwright-proxy"


def _invokes_deleted_playwright_proxy(spec: object) -> bool:
    """True if a server spec launches the Playwright MCP proxy this release deleted.

    Matched on the ARGV token, never on the server name. The canonical name was
    ``playwright-mcp``, but that is also what an operator's OWN Playwright server
    is called, and purging by name would delete a server Kiro Crew never wrote --
    the same trap ``_invokes_meshclaw`` exists to avoid.
    """
    if not isinstance(spec, dict):
        return False
    args = spec.get("args", [])
    if not isinstance(args, list):
        return False
    return any(isinstance(a, str) and a == _DELETED_PROXY_ARGV_TOKEN for a in args)


def _invokes_meshclaw(spec: object) -> bool:
    """True if a server spec's command is the dead MeshClaw predecessor binary.

    Catches stale entries the rename left behind whose *name* isn't in the
    managed set — e.g. a leftover ``npm:@playwright/mcp`` proxy pointing at an
    old MeshClaw runtime (``.../MeshClaw/.../bin/meshclaw``,
    ``...\\MeshClaw\\Scripts\\meshclaw.exe``). Keyed on the command basename so
    it matches both the bare name and absolute paths, and never matches a
    genuine playwright server (which runs ``npx``/``node``).
    """
    if not isinstance(spec, dict):
        return False
    cmd = spec.get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return False
    # mcp.json is cross-platform data (a config written on Windows may be read
    # anywhere), so split on BOTH separators rather than the host's os.sep —
    # os.path.basename only honors the local separator. Then drop a launcher
    # suffix so ``...\\Scripts\\meshclaw.exe`` (pip's Windows console script)
    # matches the bare predecessor name.
    leaf = re.split(r"[\\/]", cmd)[-1]
    stem = leaf.split(".", 1)[0]
    return stem == "meshclaw"


def clean_stale_managed_mcp() -> list[str]:
    """Remove stale managed-binary MCP entries from ``~/.kiro/settings/mcp.json``.

    Runs from explicit setup (``kirocrew setup``) and once on first gateway
    start (marker-guarded by ``run_first_run_setup``) — never on every startup,
    which would violate the "KiroCrew owns only the agent file" boundary.

    Removes two classes of stale entry left in the user's global provider
    config; genuine user-installed servers are never touched:

    * **By name** — ``kirocrew-cron`` / ``kirocrew-core`` (written there by an
      older install method; KiroCrew now keeps these in the agent file) and the
      dead predecessor ``meshclaw-cron`` / ``meshclaw-core``.
    * **By command** — any server whose command is the dead MeshClaw predecessor
      binary (basename ``meshclaw``), e.g. a leftover ``npm:@playwright/mcp``
      proxy entry pointing at an old MeshClaw runtime.

    Returns names of removed servers (empty list on no-op or error).
    """
    if not _kiro_mcp_json().is_file():
        return []
    try:
        data = json.loads(_kiro_mcp_json().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    removed = sorted(
        name
        for name, spec in servers.items()
        if name in STALE_MANAGED_MCP_SERVERS
        or _invokes_meshclaw(spec)
        or _invokes_deleted_playwright_proxy(spec)
    )
    if not removed:
        return []
    for name in removed:
        del servers[name]
    try:
        _kiro_mcp_json().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("Removed stale managed MCP entries from kiro mcp.json: %s", removed)
    except OSError:
        logger.debug("Could not clean kiro mcp.json", exc_info=True)
        return []
    return removed


def purge_deleted_proxy_from_config(config: dict) -> list[str]:
    """Drop any MCP server entry whose argv invokes the deleted Playwright proxy.

    Runs on EVERY rebuild of the agent config (not behind the first-run
    marker) because the entry can be re-injected from ~/.kiro/crew/mcp.json
    by the merge passes that precede this call.  Matched by ARGV token, never
    by server name, so an operator's own ``playwright-mcp`` server whose
    command does not invoke the deleted subcommand is left untouched.

    Mutates *config* in place.  Returns server names that were removed.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    to_remove = [
        name for name, spec in servers.items()
        if _invokes_deleted_playwright_proxy(spec)
    ]
    for name in to_remove:
        del servers[name]
    if to_remove:
        # Also strip @refs from tools/allowedTools so kiro-cli does not try
        # to mount a server that no longer exists in the map.
        for key in ("tools", "allowedTools"):
            lst = config.get(key)
            if isinstance(lst, list):
                for name in to_remove:
                    ref = f"@{name}"
                    while ref in lst:
                        lst.remove(ref)
        logger.info(
            "Purged deleted-proxy MCP entries from agent config: %s",
            to_remove,
        )
    return to_remove
