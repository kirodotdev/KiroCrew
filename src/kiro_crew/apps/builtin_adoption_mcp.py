"""E10 for a pinned tree's ``mcpServers``: the stdio servers adoption may carry.

A former built-in that ships a stdio MCP server keeps it through adoption only
when the pinned tree starts that server from its OWN files and asks for nothing
the wheel copy did not declare. The pinned tree's digest is checked on the copy
STAGE and APPLY verify, so a launch file inside that tree is code the pin names;
anything else (a ``-m`` module, which would run the released wheel copy or
another installed package, a PATH binary, an absolute path, a file outside the
tree, an HTTP ``url``) runs code the pin does not cover, and is refused.

The shape accepted is exactly the one registration already resolves inside an
installed app's root: a bare ``python``/``python3``/``py`` ``command`` and a
relative ``.py`` launch target in ``args`` that names a file inside the tree.
Both sides answer from ``bridges._resolve_app_launcher_arg``, so adoption
accepts nothing that registration would not anchor to the adopted tree. After
the swap the record is an ordinary registry app and the server is registered,
spawned and sandboxed exactly like any other registry app's stdio server;
nothing here touches the sandbox or the governance strip. The tool gate's
own-server grant follows such a record through ``builtin_adoption_gate``, which
asks :func:`carried_server` of the installed entry again at every boot.

The ceiling is the wheel built-in's own declaration, never more: no server name
it did not declare, no ``autoApprove`` tool it did not auto-approve, no ``env``
entry it did not set with the same value (a value is as much a launch input as a
key: ``PYTHONPATH`` or ``PYTHONSTARTUP`` would point the server at code outside the
tree), a server it shipped disabled stays disabled, and no other field. Narrowing is allowed, since it only removes reach. Nothing here names an
app. Owning spec: app-kit-platform.md section 33, the E10 row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The fields an adopted stdio entry may carry; every one of them is compared below.
_CARRIED_FIELDS = frozenset({"command", "args", "env", "autoApprove", "disabled"})


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def tree_owned_launch(cfg: Any, tree_root: Path) -> bool:
    """True when the stdio entry *cfg* starts a ``.py`` file inside *tree_root*.

    The same predicate ``resolve_stdio_command`` applies when it anchors an app's
    own launcher to its install root, so a tree this accepts is one whose server
    registration resolves inside the adopted root.
    """
    from kiro_crew.apps.bridges import (
        _BARE_PYTHON,
        _normalize_attached_m,
        _resolve_app_launcher_arg,
    )

    if not isinstance(cfg, dict) or "url" in cfg:
        return False
    command = cfg.get("command")
    if not isinstance(command, str):
        return False
    base = command.strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    args = _string_list(cfg.get("args"))
    if base not in _BARE_PYTHON or args is None:
        return False
    normalized = _normalize_attached_m(list(args))
    return _resolve_app_launcher_arg(normalized, tree_root) is not normalized


def _within_wheel_declaration(cfg: dict[str, Any], shipped: Any) -> bool:
    if not isinstance(shipped, dict) or set(cfg) - _CARRIED_FIELDS:
        return False
    auto = _string_list(cfg.get("autoApprove", []))
    shipped_auto = _string_list(shipped.get("autoApprove", []))
    if auto is None or shipped_auto is None or not set(auto) <= set(shipped_auto):
        return False
    env = cfg.get("env", {})
    shipped_env = shipped.get("env", {})
    if not isinstance(env, dict) or not isinstance(shipped_env, dict):
        return False
    if any(key not in shipped_env or shipped_env[key] != value for key, value in env.items()):
        return False
    return bool(cfg.get("disabled")) or not shipped.get("disabled")


def carried_server(name: str, cfg: Any, tree_root: Path, shipped_servers: Any) -> bool:
    """True when the entry *cfg* for server *name* may run from the adopted *tree_root*.

    The wheel built-in's ``mcpServers`` (*shipped_servers*) declares *name*, the tree
    starts it from its own files, and it asks for nothing that declaration did not.
    One predicate for the E10 check below and for the tool gate's own-server trust
    of an adopted record (``builtin_adoption_gate``), so the two cannot disagree.
    """
    return (
        isinstance(shipped_servers, dict)
        and name in shipped_servers
        and tree_owned_launch(cfg, tree_root)
        and _within_wheel_declaration(cfg, shipped_servers[name])
    )


def adopted_mcp_servers_refusal(
    manifest: Any, tree_root: Path, shipped_manifest: Any
) -> str | None:
    """``pinned_feature_unsupported`` unless every server in *manifest* may be carried.

    *manifest* is the pinned tree's parsed manifest at *tree_root*, and
    *shipped_manifest* the wheel built-in's. A tree that declares no server passes;
    with no wheel manifest to compare against, any server is refused.
    """
    servers = getattr(manifest, "mcpServers", None) or {}
    if not servers:
        return None
    shipped = getattr(shipped_manifest, "mcpServers", None)
    if not isinstance(servers, dict) or not isinstance(shipped, dict):
        return "pinned_feature_unsupported"
    for name, cfg in servers.items():
        if not carried_server(name, cfg, tree_root, shipped):
            return "pinned_feature_unsupported"
    return None
