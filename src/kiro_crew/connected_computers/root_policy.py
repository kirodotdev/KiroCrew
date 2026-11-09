"""Local machine: which folder may be granted as a connected computer's root.

The root is chosen in a native folder picker on the local machine and checked
again here, on its resolved real path, before any grant is written:

* absolute, an existing directory, given with no ``..`` and no NUL;
* never the file-system root, the home directory itself, or a folder that holds
  the home directory;
* never inside, or holding, Kiro Crew's data or config home (its vault, policy
  and the grants themselves live there);
* never inside, or holding, a credential home the agent sandbox already hides
  (``security.paths.sensitive_home_dirs``: ``~/.ssh``, ``~/.aws``, ...).

The OS confinement (:mod:`.confinement`) is what enforces the root at run time;
this check only stops a grant that confinement could not make safe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


class RootRefused(ValueError):
    """The folder cannot be a connected computer's root; the message says why."""


def _within(path: Path, other: Path) -> bool:
    return path == other or other in path.parents


def _protected_dirs(home: Path) -> list[Path]:
    from kiro_crew.config.paths import config_dir, data_home
    from kiro_crew.security.paths import sensitive_home_dirs

    out: list[Path] = []
    for raw in (data_home(), config_dir()):
        try:
            out.append(Path(os.path.realpath(raw)))
        except (OSError, ValueError):
            continue
    for rel in sensitive_home_dirs():
        out.append(Path(os.path.realpath(home / rel)))
    return out


def resolve_root(raw: str, *, home: Optional[Path] = None, protected: Iterable[Path] = ()) -> str:
    """The real path of ``raw`` when it may be granted; raise :class:`RootRefused`."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RootRefused("choose a folder")
    if not os.path.isabs(raw):
        raise RootRefused("the folder must be an absolute path")
    if ".." in Path(raw).parts:
        raise RootRefused("the folder path must not contain '..'")
    real = Path(os.path.realpath(raw))
    if not real.is_dir():
        raise RootRefused("the folder does not exist")
    home_dir = Path(os.path.realpath(home if home is not None else Path.home()))
    if real == Path(real.anchor):
        raise RootRefused("the whole disk cannot be shared; choose a project folder")
    if _within(home_dir, real):
        raise RootRefused("your home folder cannot be shared whole; choose a project folder in it")
    fenced = list(protected) or _protected_dirs(home_dir)
    for guarded in fenced:
        if _within(real, guarded) or _within(guarded, real):
            raise RootRefused(
                "that folder holds or is inside private settings or credentials; "
                "choose a project folder that does not"
            )
    return str(real)
