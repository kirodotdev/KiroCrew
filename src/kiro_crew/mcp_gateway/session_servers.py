"""Session-scoped MCP server injection for the shared gateway.

kiro-cli's ACP ``session/new`` accepts an ``mcpServers`` array, and a
session-injected server takes precedence over the same-named entry in the
resolved agent spec: the spec's own copy is never launched. That makes pooling
a protocol-level operation. The broker stubs replace an agent's poolable
servers for the lifetime of one session, and nothing is written to the user's
project, to their ``~/.kiro/agents/``, or through a bind mount — so pooling
works with ``agent.sandbox`` set to ``off`` (the default) and on macOS and
Windows, neither of which can bind-mount.

Only stub entries are injected. A non-poolable server is left entirely to the
agent spec, so its ``env`` — which routinely holds tokens and API keys — never
leaves the file it was declared in. Stub entries carry ``env: {}`` by
construction (``rewriter._build_stub_entry``): the pooled backend is spawned by
gatewayd, not by kiro-cli, so no credential is transmitted here either.

Precedence caveat: same-name override is verified against the shipped binary
(``test_mcp_gateway_session_inject.py`` pins it, including a live check when
kiro-cli is on PATH) but is NOT documented by kiro-cli. The documented
hierarchy covers only the three *file* tiers (agent config > workspace
``mcp.json`` > global ``mcp.json``). If a future release made injection purely
additive, an agent's own copy would launch alongside the stub, which is worse
than not pooling — hence the pinning test.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

logger = logging.getLogger(__name__)

# Keys that are positional in the ACP element shape (``name``) or that we
# always re-derive (``env``), so they must not be copied verbatim.
_ACP_RESERVED = frozenset({"name", "env", _WRAPPER_MARKER})


def _acp_env(raw: Any) -> list[dict[str, str]]:
    """Convert a kiro-agent-JSON ``env`` mapping to ACP's array-of-pairs form.

    Stub entries always carry an empty mapping, so this normally returns ``[]``.
    It is still a faithful conversion rather than a hardcoded empty list so a
    future caller that injects a non-stub entry cannot silently drop its env.
    """
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def _acp_server_entry(
    name: str, entry: dict[str, Any], channel_id: str | None = None
) -> dict[str, Any] | None:
    """Shape one rewritten ``mcpServers`` entry into an ACP array element.

    Operator-set passthrough keys (``timeout``, ``type``, ``disabledTools``,
    ``autoApprove``, vendor keys) are preserved: kiro-cli tolerates them on the
    session-injected element, and dropping ``autoApprove`` in particular would
    re-prompt for tools the agent spec had already auto-approved.

    ``channel_id`` is APPENDED as ``--channel-id <value>`` rather than
    prepended: the overlay entry runs the interpreter, so ``args`` opens with
    ``-m kiro_crew.mcp_gateway.stub`` and anything inserted ahead of that would
    be eaten by the interpreter instead of the stub. argparse does not care
    about order. The channel value is known here, at the one place that runs
    per session, so the stub does not need to recover it by walking its
    ancestors' ``/proc/<pid>/environ`` from a bash launcher.
    """
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        # A stub without a command cannot be launched; injecting it would
        # shadow the agent's working entry with a broken one. Skip instead,
        # leaving the spec's own server in place.
        return None
    args = [a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
            for a in (entry.get("args") or [])]
    if channel_id and "--channel-id" not in args:
        args.extend(["--channel-id", channel_id])
    shaped: dict[str, Any] = {
        k: v for k, v in entry.items() if k not in _ACP_RESERVED and k != "command"
    }
    shaped.update({
        "name": name,
        "command": command,
        "args": args,
        "env": _acp_env(entry.get("env")),
    })
    return shaped


def pooled_session_servers(
    overlay_dir: str | Path | None,
    agent: str | None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return ACP ``session/new`` entries for *agent*'s broker stubs.

    ``overlay_dir`` is the rewriter's output directory (usually
    ``<config_dir>/mcp-gateway/agents/``); it is ``None`` when the shared
    gateway is disabled, which is the natural off switch — this returns ``[]``
    and the session runs entirely on the agent's own servers.

    ``channel_id`` reaches the stub so it can report the channel in its caller
    identity, which ``gatewayd`` stamps onto every forwarded ``tools/call`` as
    ``_meta.kirocrew.caller``. It is deliberately NOT a pool dimension (see
    :mod:`kiro_crew.mcp_gateway.pool`), so passing it does not split backends —
    two channels reaching the same agent still share one. It is passed here
    rather than baked into the overlay because the overlay is written once at
    gateway startup and is session-agnostic, while this function runs per
    session. ``None`` simply leaves the flag off and the channel unreported.

    Fail-soft by design: any unreadable or malformed overlay yields ``[]``, so a
    bad rewrite degrades to unpooled operation rather than breaking the spawn.
    """
    if not overlay_dir or not agent:
        return []
    src = Path(overlay_dir) / f"{agent}.json"
    try:
        spec = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Normal for an agent the rewriter did not emit (e.g. it declared no
        # poolable servers at all) — not worth a warning.
        return []
    except (OSError, ValueError):
        logger.warning("MCP-gateway: cannot read overlay spec %s", src, exc_info=True)
        return []
    if not isinstance(spec, dict):
        return []
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict) or not entry.get(_WRAPPER_MARKER):
            # Not a broker stub: leave it to the agent spec entirely.
            continue
        shaped = _acp_server_entry(str(name), entry, channel_id)
        if shaped is not None:
            out.append(shaped)
    return out
