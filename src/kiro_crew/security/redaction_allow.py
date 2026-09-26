"""Hosts a reader allowed long queries for, one workspace at a time.

The dashboard's *Allow for this host* writes here after the reader confirms.
An allowed host skips ONLY the query-length and base64 heuristics of the
exfiltration check (the same relaxation the platform's exact-host exemption
gives); every credential check still runs, so an allowed host never lets a
secret through. The list is read only when a reply is SHOWN: every saved
reply keeps each blocked link as a placeholder with its record, and the
display puts an allowed host's address back. So what is saved never depends
on this list, a list that has not loaded yet only shows links blocked a
little longer, and revoking a host re-blocks every link on it.

An agent able to edit this list could allow the host it wants to send
conversation data to, so the file lives in the ``redaction-allow`` directory,
which the sandbox seals read-only (``sandbox._CREW_READONLY_LEAVES``): only the
gateway's own routes write it. Every failure to read degrades to "no host
allowed", which means more redaction, the safe direction.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from kiro_crew.config.paths import config_dir

_LOCK = threading.RLock()
_HOST_RE = re.compile(
    r"^(?:[a-z0-9._-]{1,253}\.[a-z]{2,63}|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:.]{1,45}\])$"
)
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,128}$")
MAX_HOSTS_PER_WORKSPACE = 200
#: How many workspaces the list holds. With the per-workspace cap it bounds the
#: whole file and its in-memory snapshot, on load and on every insertion.
MAX_WORKSPACES = 64
DEFAULT_WORKSPACE = "default"

_path_override: Path | None = None


def _path() -> Path:
    if _path_override is not None:
        return _path_override
    return config_dir() / "redaction-allow" / "hosts.json"


def normalize_workspace(workspace: str | None) -> str | None:
    """The key a workspace is stored under, or None for a name it cannot hold.

    No workspace means the default one. Any other name is kept exactly or not
    at all: folding an unsupported name into another workspace's key would let
    a host allowed there apply to workspaces nobody allowed it in.
    """
    ws = (workspace or "").strip()
    if not ws:
        return DEFAULT_WORKSPACE
    return ws if _WORKSPACE_RE.match(ws) else None


def valid_host(host: object) -> bool:
    return isinstance(host, str) and bool(_HOST_RE.match(host))


# The gateway is the list's only writer, so it holds the list in memory: the
# render paths read this snapshot and never touch the disk on the event loop.
# Nothing loads it at boot: the first read starts a background thread and
# answers "no host allowed" until that thread lands -- more redaction, never
# less. A write reads the file itself under the lock and replaces the snapshot
# whole. The snapshot is keyed by the file it was read from, so a different
# config home reads afresh.
_snapshot: tuple[Path, dict[str, list[str]]] | None = None
_loading = False
_load_thread: threading.Thread | None = None


def preload() -> None:
    """Read the list from disk into the snapshot. Call off the event loop."""
    global _snapshot, _loading
    path = _path()
    # Under the writers' lock, so a write cannot land between this read and
    # the snapshot it fills.
    with _LOCK:
        _snapshot = (path, _load(path))
        _loading = False


def _background_preload() -> None:
    global _loading
    try:
        preload()
    finally:
        _loading = False


def _start_background_load() -> None:
    global _loading, _load_thread
    with _LOCK:
        if _loading:
            return
        _loading = True
        _load_thread = threading.Thread(
            target=_background_preload, name="redaction-allow-load", daemon=True
        )
        _load_thread.start()


def _current() -> dict[str, list[str]]:
    snap = _snapshot
    if snap is None or snap[0] != _path():
        _start_background_load()
        return {}
    return snap[1]


def _read_for_write() -> dict[str, list[str]]:
    """The list a write builds on. Call with ``_LOCK`` held.

    Always the file's real contents: a write that started from the empty
    answer ``_current`` gives while the background read is pending would
    replace the file with that empty list and lose every entry in it.
    """
    snap = _snapshot
    if snap is None or snap[0] != _path():
        preload()
        snap = _snapshot
    return {ws: list(hosts) for ws, hosts in (snap[1] if snap is not None else {}).items()}


def _load(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for ws, hosts in raw.items():
        if len(out) >= MAX_WORKSPACES:
            break
        if isinstance(ws, str) and _WORKSPACE_RE.match(ws) and isinstance(hosts, list):
            kept = [h for h in hosts if valid_host(h)][:MAX_HOSTS_PER_WORKSPACE]
            if kept:
                out[ws] = kept
    return out


def _write(data: dict[str, list[str]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    global _snapshot
    _snapshot = (path, {ws: list(hosts) for ws, hosts in data.items()})


def allowed_hosts_for(workspace: str | None) -> frozenset[str]:
    """The hosts allowed in ``workspace``; empty on any read failure."""
    ws = normalize_workspace(workspace)
    return frozenset(_current().get(ws, ())) if ws is not None else frozenset()


def list_allowed() -> dict[str, list[str]]:
    """Every workspace's hosts, read from the file. Call off the event loop."""
    with _LOCK:
        return _read_for_write()


def allow_host(workspace: str | None, host: str) -> bool:
    """Allow ``host`` in ``workspace``. False when the host or workspace name is
    off-shape, the workspace's list is full, or the list already holds
    :data:`MAX_WORKSPACES` other workspaces."""
    host = host.lower()
    ws = normalize_workspace(workspace)
    if not valid_host(host) or ws is None:
        return False
    with _LOCK:
        data = _read_for_write()
        if ws not in data and len(data) >= MAX_WORKSPACES:
            return False
        hosts = data.setdefault(ws, [])
        if host in hosts:
            return True
        if len(hosts) >= MAX_HOSTS_PER_WORKSPACE:
            return False
        hosts.append(host)
        _write(data)
    return True


def revoke_host(workspace: str | None, host: str) -> bool:
    """Remove ``host`` from ``workspace``. True when it was there."""
    host = host.lower()
    ws = normalize_workspace(workspace)
    if ws is None:
        return False
    with _LOCK:
        data = _read_for_write()
        hosts = data.get(ws, [])
        if host not in hosts:
            return False
        hosts.remove(host)
        if not hosts:
            data.pop(ws, None)
        _write(data)
    return True
