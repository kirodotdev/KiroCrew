"""Render a confinement plan as an exact native Windows MXC policy document."""

from __future__ import annotations

import json
import ntpath
from collections.abc import Iterable

from kiro_crew.sandbox_plan import ConfinementPlan


class WindowsPolicyUnsupported(ValueError):
    """A boundary cannot be represented without weakening it."""


def windows_literal_path(value: str) -> str:
    drive, tail = ntpath.splitdrive(value)
    if (
        not value
        or "\0" in value
        or len(drive) != 2
        or drive[1] != ":"
        or not ("a" <= drive[0].lower() <= "z")
        or not tail.startswith(("/", "\\"))
        or ":" in tail
    ):
        raise WindowsPolicyUnsupported("Windows sandbox paths must be local and absolute")
    if any(char in value for char in "*?"):
        raise WindowsPolicyUnsupported("Literal Windows sandbox paths cannot contain wildcards")
    value.encode("utf-8", errors="strict")
    return ntpath.normpath(value).replace("\\", "/")


def permission_entries(
    plan: ConfinementPlan, *, readonly_files: Iterable[str] = ()
) -> dict[str, str]:
    """Preserve exact denies, readable governance seals and approved windows."""
    entries = {":root": "write"}

    def put(path: str, access: str) -> None:
        target = windows_literal_path(path)
        key = next((key for key in entries if key.casefold() == target.casefold()), target)
        previous = entries.get(key)
        if previous is not None and previous != access:
            raise WindowsPolicyUnsupported("Conflicting access modes for one Windows sandbox path")
        entries[key] = access

    for path in (*plan.readonly, *plan.runtime_parents, *readonly_files):
        put(path, "read")
    for mask in plan.masks:
        if mask.cancelled:
            if mask.read_only_when_cancelled:
                put(mask.path, "read")
        else:
            put(mask.path, "deny")
    for path in plan.files:
        put(path, "deny")
    for path in (*plan.windows, *plan.writable):
        put(path, "write")
    for path, _name in plan.expose:
        put(path, "read")
    if plan.hide_ssh:
        put(plan.ssh_dir, "deny")
        if plan.ssh_known_hosts:
            put(plan.ssh_known_hosts, "read")
    return entries


def render_permissions(entries: dict[str, str], *, network: bool) -> str:
    """Serialize a complete policy; the native runner merges no configuration."""
    if entries.get(":root") not in {"read", "write"}:
        raise WindowsPolicyUnsupported("Unsupported root filesystem permission")
    if any(access not in {"read", "write", "deny"} for access in entries.values()):
        raise WindowsPolicyUnsupported("Unknown Windows filesystem permission")
    return json.dumps(
        {"version": 1, "filesystem": entries, "network": network},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
