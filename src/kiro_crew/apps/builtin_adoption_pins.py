"""The wheel-pinned table of former built-in apps that move to their own repository.

``adoption-pins.json`` ships beside this module and is immutable once installed:
it is the integrity anchor for adoption (``commit``, ``archiveSha256`` and
``treeSha256``). Core names no app here: each row is data, written from the output
of ``scripts/adoption_pin.py compute``. Every validation failure fails toward "no
adoption", which is the behaviour a host without this table already has.

Owning spec: ``docs/system-specs/modules/app-kit-platform.md`` § Former built-in
adoption.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The data file. Tests repoint this seam and call ``load_adoption_pins.cache_clear()``.
_PINS_PATH = Path(__file__).resolve().parent / "adoption-pins.json"

SCHEMA_VERSION = 1

#: A bound on a listing mistake, not on an attacker: the file ships in the wheel.
MAX_PINS = 64

_ROW_KEYS = frozenset(
    {
        "name",
        "repository",
        "commit",
        "version",
        "treeSha256",
        "archiveSha256",
        "minAppSdkLevel",
        "legacyNav",
    }
)
_NAV_KEYS = frozenset({"route", "icon"})

_REPOSITORY_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_ROUTE_RE = re.compile(r"/[a-z0-9]+(?:-[a-z0-9]+)*")
_ICON_RE = re.compile(r"[A-Z][A-Za-z0-9]{0,63}")


@dataclass(frozen=True)
class AdoptionPin:
    """One validated pin row."""

    name: str
    repository: str
    commit: str
    version: str
    tree_sha256: str
    archive_sha256: str
    min_app_sdk_level: int
    legacy_route: str
    legacy_icon: str

    @property
    def owner_repo(self) -> tuple[str, str]:
        """``(owner, repo)`` parsed from :attr:`repository` (validated at load)."""
        match = _REPOSITORY_RE.fullmatch(self.repository)
        if match is None:  # pragma: no cover - a loaded pin always matches
            raise ValueError("pin repository is not a validated GitHub URL")
        return match.group(1), match.group(2)

    @property
    def archive_url(self) -> str:
        """The codeload archive for :attr:`commit`. Derived, never authored."""
        owner, repo = self.owner_repo
        return f"https://codeload.github.com/{owner}/{repo}/tar.gz/{self.commit}"

    @property
    def archive_root(self) -> str:
        """The single top-level directory codeload puts every member under."""
        return f"{self.owner_repo[1]}-{self.commit}"


def _row_error(row: Any) -> str:
    """Why *row* is not a valid pin, or ``""``."""
    from kiro_crew.apps.manifest import app_name_error

    if not isinstance(row, dict):
        return "a row is not an object"
    keys = set(row)
    if keys != _ROW_KEYS:
        return f"keys {sorted(keys ^ _ROW_KEYS)} are unknown or missing"
    name = row["name"]
    if not isinstance(name, str) or app_name_error(name) is not None:
        return "name is not an admissible app name"
    repository = row["repository"]
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
        or repository.endswith(".git")
    ):
        return "repository is not https://github.com/<owner>/<repo>"
    for key, pattern in (
        ("commit", _COMMIT_RE),
        ("treeSha256", _SHA256_RE),
        ("archiveSha256", _SHA256_RE),
        ("version", _VERSION_RE),
    ):
        value = row[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return f"{key} has the wrong shape"
    level = row["minAppSdkLevel"]
    if not isinstance(level, int) or isinstance(level, bool) or level < 1:
        return "minAppSdkLevel is not an integer of at least 1"
    nav = row["legacyNav"]
    if not isinstance(nav, dict) or set(nav) != _NAV_KEYS:
        return "legacyNav must hold exactly route and icon"
    if not isinstance(nav["route"], str) or not _ROUTE_RE.fullmatch(nav["route"]):
        return "legacyNav.route has the wrong shape"
    if not isinstance(nav["icon"], str) or not _ICON_RE.fullmatch(nav["icon"]):
        return "legacyNav.icon has the wrong shape"
    return ""


def parse_adoption_pins(document: Any) -> dict[str, AdoptionPin]:
    """Validate a parsed pins document. Never raises; a bad row is dropped and logged."""
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        logger.error("adoption pins: unknown schema; no app is adopted")
        return {}
    rows = document.get("pins")
    if not isinstance(rows, list):
        logger.error("adoption pins: 'pins' is not a list; no app is adopted")
        return {}
    if len(rows) > MAX_PINS:
        logger.error("adoption pins: %d rows exceed the cap of %d", len(rows), MAX_PINS)
        return {}
    valid: dict[str, AdoptionPin] = {}
    duplicated: set[str] = set()
    for index, row in enumerate(rows):
        error = _row_error(row)
        if error:
            logger.error("adoption pins: row %d dropped: %s", index, error)
            continue
        name = row["name"]
        if name in valid or name in duplicated:
            duplicated.add(name)
            valid.pop(name, None)
            continue
        valid[name] = AdoptionPin(
            name=name,
            repository=row["repository"],
            commit=row["commit"],
            version=row["version"],
            tree_sha256=row["treeSha256"],
            archive_sha256=row["archiveSha256"],
            min_app_sdk_level=row["minAppSdkLevel"],
            legacy_route=row["legacyNav"]["route"],
            legacy_icon=row["legacyNav"]["icon"],
        )
    for name in sorted(duplicated):
        logger.error("adoption pins: %r is pinned more than once; every row dropped", name)
    return valid


@functools.lru_cache(maxsize=1)
def load_adoption_pins() -> dict[str, AdoptionPin]:
    """The validated pin table, read once per process (the wheel file is immutable)."""
    try:
        document = json.loads(_PINS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        logger.error("adoption pins unreadable (%s); no app is adopted", exc)
        return {}
    return parse_adoption_pins(document)


def adoption_pin(name: str) -> AdoptionPin | None:
    """The pin for *name*, or None."""
    return load_adoption_pins().get(name)


def adoption_pin_names() -> frozenset[str]:
    """Every pinned app name."""
    return frozenset(load_adoption_pins())
