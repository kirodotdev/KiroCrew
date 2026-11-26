"""The wheel-pinned table of former built-in apps that move to their own repository.

``adoption-pins.json`` ships beside this module and is immutable once installed:
it is the integrity anchor for adoption (``commit``, ``archiveSha256`` and
``treeSha256``). Core names no app here: each row is data, written from the output
of ``scripts/adoption_pin.py compute``, and the row of an app whose wheel copy a
release deletes also carries what ``adoption_pin.py freeze`` printed from that copy
(:data:`DELETED_COPY_KEY`). Every validation failure fails toward "no adoption",
which is the behaviour a host without this table already has.

Owning spec: ``docs/system-specs/modules/app-kit-platform.md`` § Former built-in
adoption.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The data file. Tests repoint this seam and call ``load_adoption_pins.cache_clear()``.
_PINS_PATH = Path(__file__).resolve().parent / "adoption-pins.json"

#: Harness-only: a test-owned pin table read INSTEAD of the shipped one. The
#: integration and E2E harnesses set it to an empty table, so a harness boot
#: fetches nothing, and a test that drives adoption writes a fixture pin there.
#: Setting it needs the same control of the gateway's environment as
#: ``PYTHONPATH``; every digest check still runs against the table's own values.
HARNESS_PINS_ENV = "KIROCREW_HARNESS_ADOPTION_PINS"

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
#: The one optional key of a shipped row: what the app's deleted wheel copy gave E9 and
#: the limits only a wheel copy supplies, which a row carries once that copy is gone
#: (``builtin_adoption_baseline``).
DELETED_COPY_KEY = "deletedCopy"
_DELETED_COPY_KEYS = frozenset({"permissions", "grants"})
#: The optional keys of a :data:`DELETED_COPY_KEY` object, each the input of a limit only
#: a wheel copy supplies (app-kit-platform.md § 33 rows 1-4 and 9), present only when the
#: copy declared one. Each is the copy's manifest key, but for ``agentNames``: the names
#: its ``agents`` files declare.
DELETED_COPY_LIMIT_KEYS = frozenset(
    {"mcpServers", "agentNames", "agentTools", "credentialMasks", "agentReadOnlyDirs"}
)
#: The one extra key a harness table's row may carry: an absolute path to a
#: codeload-shaped archive of the pinned commit, read instead of the codeload GET
#: (and in place of the git fetch), still checked against ``archiveSha256``.
_HARNESS_ARCHIVE_KEY = "harnessArchive"

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
    #: Set only on a row of a :data:`HARNESS_PINS_ENV` table.
    harness_archive: str = ""
    #: The row's :data:`DELETED_COPY_KEY` object as canonical JSON text, so the cached
    #: pin stays immutable; ``""`` when the row has none.
    deleted_copy: str = ""

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


def _row_error(row: Any, *, harness: bool = False) -> str:
    """Why *row* is not a valid pin, or ``""``."""
    # The light module: the sandbox's mask reader loads this table while ``security``
    # imports (``credential_masks``' frozen leg), before ``apps.manifest`` can load.
    from kiro_crew.apps.manifest_names import app_name_error

    if not isinstance(row, dict):
        return "a row is not an object"
    keys = set(row)
    if harness and _HARNESS_ARCHIVE_KEY in keys:
        archive = row[_HARNESS_ARCHIVE_KEY]
        if not isinstance(archive, str) or not Path(archive).is_absolute():
            return f"{_HARNESS_ARCHIVE_KEY} is not an absolute path"
        keys.discard(_HARNESS_ARCHIVE_KEY)
    if DELETED_COPY_KEY in keys:
        error = deleted_copy_error(row[DELETED_COPY_KEY])
        if error:
            return error
        keys.discard(DELETED_COPY_KEY)
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
    return legacy_nav_error(row["legacyNav"])


def legacy_nav_error(nav: Any) -> str:
    """Why *nav* is not a ``legacyNav`` ``{route, icon}`` object, or ``""``.

    The pin row and the copy the gateway records at adoption
    (``builtin_adoption_markers.coerce_adoption_nav``) are held to this one shape.
    """
    if not isinstance(nav, dict) or set(nav) != _NAV_KEYS:
        return "legacyNav must hold exactly route and icon"
    if not isinstance(nav["route"], str) or not _ROUTE_RE.fullmatch(nav["route"]):
        return "legacyNav.route has the wrong shape"
    if not isinstance(nav["icon"], str) or not _ICON_RE.fullmatch(nav["icon"]):
        return "legacyNav.icon has the wrong shape"
    return ""


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def deleted_copy_error(value: Any) -> str:
    """Why *value* is not a :data:`DELETED_COPY_KEY` object, or ``""``.

    ``permissions`` is the deleted copy's own ``permissions`` object and ``grants`` maps
    a grant family to the entries the copy declared in it. Of
    :data:`DELETED_COPY_LIMIT_KEYS`, ``mcpServers`` and ``agentTools`` are objects and the
    rest are lists of names or entries. Only the shape is checked here; what the object
    must equal is ``adoption_pin.py verify``'s.
    """
    if not isinstance(value, dict) or not (
        _DELETED_COPY_KEYS <= set(value) <= _DELETED_COPY_KEYS | DELETED_COPY_LIMIT_KEYS
    ):
        return (
            f"{DELETED_COPY_KEY} must hold permissions and grants, and only the limit inputs"
            f" {sorted(DELETED_COPY_LIMIT_KEYS)} beside them"
        )
    if not isinstance(value["permissions"], dict):
        return f"{DELETED_COPY_KEY}.permissions is not an object"
    grants = value["grants"]
    if not isinstance(grants, dict) or not all(
        isinstance(family, str) and family and _is_string_list(entries)
        for family, entries in grants.items()
    ):
        return f"{DELETED_COPY_KEY}.grants is not a map of grant families to entry lists"
    for key in sorted(DELETED_COPY_LIMIT_KEYS & set(value)):
        frozen = value[key]
        if key in ("mcpServers", "agentTools"):
            if (
                not isinstance(frozen, dict)
                or not frozen
                or not all(isinstance(k, str) and k for k in frozen)
            ):
                return f"{DELETED_COPY_KEY}.{key} is not a non-empty object"
        elif not frozen or not _is_string_list(frozen):
            return f"{DELETED_COPY_KEY}.{key} is not a non-empty list of strings"
    return ""


@functools.lru_cache(maxsize=MAX_PINS)
def _frozen_document(text: str) -> dict[str, Any]:
    """A row's ``deletedCopy`` text parsed once; never handed out, only copied from."""
    document = json.loads(text)
    return document if isinstance(document, dict) else {}


def frozen_limit_input(pin: AdoptionPin | None, key: str) -> Any:
    """The input *key* of a limit that *pin*'s row froze from its deleted copy, or None.

    *key* is one of :data:`DELETED_COPY_LIMIT_KEYS`; a copy that declared nothing under
    it left it out, and so does a row with no ``deletedCopy``. No check that the copy is
    gone: the caller asks only for a name whose wheel copy its own scan of the shipped
    copies did not find (``builtin_adoption_baseline.no_copy_limit_input`` checks that
    itself). A copy, so a caller cannot change what the next one reads.
    """
    if pin is None or not pin.deleted_copy:
        return None
    return copy.deepcopy(_frozen_document(pin.deleted_copy).get(key))


def parse_adoption_pins(document: Any, *, harness: bool = False) -> dict[str, AdoptionPin]:
    """Validate a parsed pins document. Never raises; a bad row is dropped and logged.

    *harness* admits :data:`_HARNESS_ARCHIVE_KEY`; the shipped table never has it.
    """
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
        error = _row_error(row, harness=harness)
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
            harness_archive=row.get(_HARNESS_ARCHIVE_KEY, ""),
            deleted_copy=(
                json.dumps(row[DELETED_COPY_KEY], sort_keys=True) if DELETED_COPY_KEY in row else ""
            ),
        )
    for name in sorted(duplicated):
        logger.error("adoption pins: %r is pinned more than once; every row dropped", name)
    return valid


@functools.lru_cache(maxsize=1)
def load_adoption_pins() -> dict[str, AdoptionPin]:
    """The validated pin table, read once per process (the wheel file is immutable).

    The :data:`HARNESS_PINS_ENV` table when that is set, else the shipped one.
    """
    override = os.environ.get(HARNESS_PINS_ENV, "")
    path = Path(override) if override else _PINS_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        logger.error("adoption pins unreadable (%s); no app is adopted", exc)
        return {}
    return parse_adoption_pins(document, harness=bool(override))


def adoption_pin(name: str) -> AdoptionPin | None:
    """The pin for *name*, or None."""
    return load_adoption_pins().get(name)


def adoption_pin_names() -> frozenset[str]:
    """Every pinned app name."""
    return frozenset(load_adoption_pins())
