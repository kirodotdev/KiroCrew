"""Portable path discovery for Kiro BYO and its host integration.

Kiro BYO deliberately does not assume that Kiro Crew's data directory lives
next to the application or at any particular drive/path.  The Kiro BYO home and
Kiro Crew data home are separate concepts:

* ``KIRO_BYO_HOME`` points at Kiro BYO's persistent control/config directory.
* ``KIRO_BYO_CREW_DATA_HOME`` is Kiro BYO's explicit integration override for
  the Kiro Crew user-data directory. Existing host variables ``KIRO_CREW_HOME``
  / ``KIROCREW_HOME`` are also read when present, but Kiro BYO does not create
  or modify them.

The installer persists the resolved values into ``data/integration.json`` and
into the runtime bootstrap state when known.  Runtime code consumes those facts
rather than guessing Windows/macOS/Linux layout conventions.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


_APP_HOME_OVERRIDE: ContextVar[Path | None] = ContextVar("kiro_byo_app_home", default=None)


@contextmanager
def use_app_home(path: str | Path):
    """Temporarily route Kiro BYO persistent state to an app-owned data dir.

    KiroCrew App hooks share the Gateway process, so mutating ``os.environ`` is
    the wrong isolation primitive.  A ContextVar keeps concurrent app requests
    scoped to their own task while the standalone ACP process continues to use
    bootstrap state / KIRO_BYO_HOME normally.
    """

    token = _APP_HOME_OVERRIDE.set(Path(path).expanduser())
    try:
        yield
    finally:
        _APP_HOME_OVERRIDE.reset(token)


def app_home() -> Path:
    """Return Kiro BYO's persistent home directory.

    ``KIRO_BYO_HOME`` is authoritative when explicitly set.  Packaged installs
    also carry a bootstrap state next to the installed runtime so GUI launches
    do not depend on a shell inheriting a newly-created environment variable.
    """

    overridden = _APP_HOME_OVERRIDE.get()
    if overridden is not None:
        return overridden

    configured = os.environ.get("KIRO_BYO_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()

    # Prefer the bootstrap state beside the imported runtime.  If ``kiro_byo``
    # was imported through the KiroCrew App package namespace first, the top-level
    # module can still resolve to the source checkout rather than the copy installed
    # beside KiroCrew.  Scan the active interpreter's import roots for an installed
    # runtime state before falling back to the checkout.  This makes the ACP child
    # converge on the same App-owned data directory even when module provenance
    # differs between the dashboard process and the fresh self-served ACP process.
    state_paths: list[Path] = [Path(__file__).resolve().parent / "_bootstrap_state.json"]
    seen: set[str] = {str(state_paths[0])}
    for entry in sys.path:
        if not isinstance(entry, str) or not entry.strip():
            continue
        try:
            candidate_state = Path(entry).expanduser().resolve() / "kiro_byo" / "_bootstrap_state.json"
        except (OSError, RuntimeError):
            continue
        key = str(candidate_state)
        if key not in seen:
            seen.add(key)
            state_paths.append(candidate_state)

    for state_path in state_paths:
        state = _read_json(state_path)
        persisted = state.get("kiro_byo_home", "")
        if isinstance(persisted, str) and persisted.strip():
            candidate = Path(persisted.strip()).expanduser()
            if candidate.exists():
                return candidate

    # Development/source-checkout fallback only when this is actually a source
    # tree. A future pip/app bundle must not accidentally use site-packages as
    # its writable data directory.
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").exists() and (source_root / "src" / "kiro_byo").exists():
        return source_root

    # Portable installed fallback. Current Windows integration writes bootstrap
    # state, so this is mainly for future standalone/macOS/Linux packaging.
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "KiroBYO"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "KiroBYO"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / "kiro-byo"


def integration_state_path() -> Path:
    return app_home() / "data" / "integration.json"


def crew_data_home() -> Path | None:
    """Return the configured Kiro Crew data home when it is actually known.

    No platform-specific default is invented here.  Kiro Crew can be portable,
    installed on another drive, or configured with a user-selected data root.
    Consumers that require this path should either ask the user or rely on an
    installer/host supplied value.
    """

    for name in ("KIRO_BYO_CREW_DATA_HOME", "KIRO_CREW_HOME", "KIROCREW_HOME"):
        configured = os.environ.get(name, "").strip()
        if configured:
            return Path(configured).expanduser()

    state = _read_json(integration_state_path())
    persisted = state.get("kiro_crew_home", "")
    if isinstance(persisted, str) and persisted.strip():
        return Path(persisted.strip()).expanduser()

    runtime_state = _read_json(Path(__file__).resolve().parent / "_bootstrap_state.json")
    persisted = runtime_state.get("kiro_crew_home", "")
    if isinstance(persisted, str) and persisted.strip():
        return Path(persisted.strip()).expanduser()

    return None
