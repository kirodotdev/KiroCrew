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

logger = logging.getLogger(__name__)

_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"

# Managed servers whose command is the kirocrew binary itself.
# Only these are affected by install-method path changes.
# Ordered tuple (not a set) so consumers that iterate — e.g. `kirocrew
# doctor`'s MCP probe — get a deterministic order.
KIROCREW_BIN_MCP_SERVERS = ("kirocrew-cron", "kirocrew-core")

# The dead predecessor product binaries in the rename lineage
# (MeshClaw → KiroClaw → KiroCrew). Their managed-server entries — pointing at
# now-dead build paths — get left behind in the user's global provider config
# by a rename and are unambiguously stale and safe to purge. ``kiroclaw`` is
# the immediate predecessor (an existing KiroClaw install wrote
# ``kiroclaw-cron`` / ``kiroclaw-core`` there); ``meshclaw`` is the generation
# before that. Both are dead — keep purging both.
PREDECESSOR_BINS = frozenset({"kiroclaw", "meshclaw"})
PREDECESSOR_BIN_MCP_SERVERS = frozenset(
    f"{bin_name}-{suffix}" for bin_name in PREDECESSOR_BINS for suffix in ("cron", "core")
)

# Every managed-binary server name KiroCrew is responsible for removing from
# the user's global mcp.json (KiroCrew never legitimately writes these there).
STALE_MANAGED_MCP_SERVERS = frozenset(KIROCREW_BIN_MCP_SERVERS) | PREDECESSOR_BIN_MCP_SERVERS


def _invokes_predecessor_bin(spec: object) -> bool:
    """True if a server spec's command is a dead predecessor product binary.

    Catches stale entries the rename left behind whose *name* isn't in the
    managed set — e.g. a leftover ``npm:@playwright/mcp`` proxy pointing at an
    old KiroClaw or MeshClaw runtime (``.../KiroClaw/.../bin/kiroclaw``,
    ``...\\KiroClaw\\Scripts\\kiroclaw.exe``, ``.../MeshClaw/.../bin/meshclaw``).
    Keyed on the command basename so it matches both the bare name and absolute
    paths, and never matches a genuine playwright server (which runs
    ``npx``/``node``).
    """
    if not isinstance(spec, dict):
        return False
    cmd = spec.get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return False
    # mcp.json is cross-platform data (a config written on Windows may be read
    # anywhere), so split on BOTH separators rather than the host's os.sep —
    # os.path.basename only honors the local separator. Then drop a launcher
    # suffix so ``...\\Scripts\\kiroclaw.exe`` (pip's Windows console script)
    # matches the bare predecessor name.
    leaf = re.split(r"[\\/]", cmd)[-1]
    stem = leaf.split(".", 1)[0]
    return stem in PREDECESSOR_BINS


def clean_stale_managed_mcp() -> list[str]:
    """Remove stale managed-binary MCP entries from ``~/.kiro/settings/mcp.json``.

    Runs from explicit setup (``kirocrew setup``) and once on first gateway
    start (marker-guarded by ``run_first_run_setup``) — never on every startup,
    which would violate the "KiroCrew owns only the agent file" boundary.

    Removes two classes of stale entry left in the user's global provider
    config; genuine user-installed servers are never touched:

    * **By name** — ``kirocrew-cron`` / ``kirocrew-core`` (written there by an
      older install method; KiroCrew now keeps these in the agent file) and the
      dead predecessors ``kiroclaw-cron`` / ``kiroclaw-core`` and
      ``meshclaw-cron`` / ``meshclaw-core``.
    * **By command** — any server whose command is a dead predecessor product
      binary (basename ``kiroclaw`` or ``meshclaw``), e.g. a leftover
      ``npm:@playwright/mcp`` proxy entry pointing at an old KiroClaw or
      MeshClaw runtime.

    Returns names of removed servers (empty list on no-op or error).
    """
    if not _KIRO_MCP_JSON.is_file():
        return []
    try:
        data = json.loads(_KIRO_MCP_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    removed = sorted(
        name
        for name, spec in servers.items()
        if name in STALE_MANAGED_MCP_SERVERS or _invokes_predecessor_bin(spec)
    )
    if not removed:
        return []
    for name in removed:
        del servers[name]
    try:
        _KIRO_MCP_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("Removed stale managed MCP entries from kiro mcp.json: %s", removed)
    except OSError:
        logger.debug("Could not clean kiro mcp.json", exc_info=True)
        return []
    return removed
