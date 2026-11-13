"""Where installed Agent Plugins live, and the spec's path-containment rule.

Kiro IDE, kiro-cli and KAS all install into ``<powers home>/installed/<name>/``,
list the names in ``<powers home>/installed.json`` and keep per-plugin state in
``<powers home>/data/<name>/``. The powers home is ``~/.kiro/powers`` unless
``KIRO_POWERS_HOME`` names another directory, the override Kiro IDE honours.
Reading the same tree is what lets a plugin installed once work in Kiro and in
Kiro Crew.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.config.paths import kiro_home

#: Environment override for the powers home, shared with Kiro IDE.
POWERS_HOME_ENV = "KIRO_POWERS_HOME"
INSTALLED_DIR = "installed"
INSTALLED_INDEX = "installed.json"
DATA_DIR = "data"


def powers_home() -> Path:
    """The directory that holds ``installed/``, ``installed.json`` and ``data/``."""
    override = os.environ.get(POWERS_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return kiro_home() / "powers"


def plugin_data_dir(home: Path, name: str) -> Path:
    """The ``PLUGIN_DATA`` directory for the plugin installed as *name*."""
    return home / DATA_DIR / name


def is_plain_name(name: object) -> bool:
    """True when *name* is one path segment that cannot leave its parent.

    An ``installed.json`` entry names a directory under ``installed/``, so a
    separator, ``.``, ``..`` or an empty string would point somewhere else. So
    would a colon on Windows: ``installed / "D:x"`` is the drive-relative path
    ``D:x``, outside ``installed/``, and ``name:stream`` opens an NTFS stream.
    It is refused on every platform so a listing reads the same everywhere.
    """
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    return not any(char in name for char in ("/", "\\", ":", "\x00"))


def resolves_within(path: str | os.PathLike[str], root_real: str) -> str | None:
    """The resolved *path* when it stays inside *root_real*, else ``None``.

    §4.1 lets a link point anywhere inside the plugin root and nowhere outside
    it, so the test is on the fully resolved target, not on the spelling.
    *root_real* must already be resolved.
    """
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    if resolved == root_real:
        return resolved
    try:
        if os.path.commonpath([resolved, root_real]) == root_real:
            return resolved
    except ValueError:
        # Different drives on Windows: not inside.
        return None
    return None
