"""Bind a signed Python stdio launch to the gateway's selected bytecode cache."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.platform_compat_runtime import _PYTHON_OPTIONS_WITH_VALUES

_PYTHON_COMMAND = re.compile(
    r"(?:python(?:\d+(?:\.\d+)*)?[tdw]*|pypy(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", re.IGNORECASE
)


def _is_python_command(command: Any) -> bool:
    return isinstance(command, str) and (
        command == sys.executable or bool(_PYTHON_COMMAND.fullmatch(os.path.basename(command)))
    )


def with_python_cache_prefix(cfg: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Put the cache option last before Python's target, retaining authored flags.

    Python's ``-E`` and ``-I`` ignore the environment transport. Cache selection
    consumes an interpreter cache option during startup, so authored cache options
    are removed before the gateway emits its own. Other flags and arguments
    handed to the script/module are preserved. Non-Python commands keep their env
    transport and argv unchanged. Blocking work is owned by the cache allocator.
    """
    if not _is_python_command(cfg.get("command")):
        return cfg
    args = list(cfg.get("args") or [])
    options: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str):
            return cfg
        if arg in ("-", "--") or not arg.startswith("-"):
            break
        if arg.startswith("--"):
            consumed = 2 if arg in _PYTHON_OPTIONS_WITH_VALUES else 1
            options.extend(args[index : index + consumed])
            index += consumed
            continue
        # Short-option clusters stop at a launch selector or a value-bearing
        # option: the rest of -c/-m is the target, and of -W/-X is its value.
        target = False
        consumes_value = False
        cache_option = False
        keep_flag = arg
        for position, option in enumerate(arg[1:], start=1):
            if option in ("c", "m"):
                target = True
                break
            if option in ("W", "X"):
                consumes_value = position == len(arg) - 1
                value = (
                    args[index + 1]
                    if consumes_value and index + 1 < len(args)
                    else arg[position + 1 :]
                )
                cache_option = (
                    option == "X"
                    and isinstance(value, str)
                    and value.partition("=")[0] == "pycache_prefix"
                )
                if cache_option:
                    keep_flag = arg[:position]
                break
        if target:
            break
        consumed = 2 if consumes_value else 1
        if cache_option:
            if keep_flag != "-":
                options.append(keep_flag)
        else:
            options.extend(args[index : index + consumed])
        index += consumed
    return {
        **cfg,
        "args": [*options, "-X", f"pycache_prefix={prefix}", *args[index:]],
    }


def signed_launch_pinned(app_name: str, entry: Any, *, resource_root: Path) -> bool:
    """Validate a Python cache pin without writing or allocating a directory.

    The caller supplies the app's executed resource root. The home is read through
    its side-effect-free resolver, not the startup directory-maintenance helper.
    This checks separation from app content, not provenance against same-user writes.
    """
    from kiro_crew.config.paths import peek_data_home

    if (
        not isinstance(entry, dict)
        or entry.get("url")
        or not _is_python_command(entry.get("command"))
    ):
        return False
    args = entry.get("args")
    env = entry.get("env")
    prefix = env.get("PYTHONPYCACHEPREFIX") if isinstance(env, dict) else None
    if (
        not isinstance(args, list)
        or not all(isinstance(arg, str) for arg in args)
        or not isinstance(prefix, str)
        or not prefix
        or with_python_cache_prefix(entry, prefix) != entry
    ):
        return False
    try:
        home = peek_data_home()
        run = home / "run"
        base = run / "app-pycache" / app_name
        path = Path(prefix)
        if not path.is_absolute() or path.parent != base:
            return False
        # lstat every component: resolving alone would admit sibling cache links.
        # A missing component is accepted: an absent cache holds no bytecode and
        # Python creates it empty. A later allocation for the same app removes
        # earlier leaves, so a registered sibling's pin may point at none.
        existing = None
        for directory in (run, run / "app-pycache", base, path):
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                break
            if not stat.S_ISDIR(mode) or is_link_or_junction(directory):
                return False
            existing = directory
        if existing is None:
            return False
        resolved = existing.resolve(strict=True)
        return not any(
            resolved.is_relative_to(root.resolve())
            for root in (home / "apps" / app_name, resource_root)
        )
    except (OSError, ValueError, RuntimeError):
        return False


def app_mcp_launch_allowed(app_name: str, entry: Any) -> bool:
    """Require an existing pin only for Python launches covered by signature policy."""
    if (
        not isinstance(entry, dict)
        or entry.get("url")
        or not _is_python_command(entry.get("command"))
    ):
        return True
    from kiro_crew.apps import bridges
    from kiro_crew.apps.admission import signature_required, signed_tree_applies

    if not signature_required():
        return True
    root = bridges._app_resource_root(app_name)
    return not signed_tree_applies(app_name, root) or signed_launch_pinned(
        app_name, entry, resource_root=root
    )
