"""The single canonical table of kiro-cli / amazon-q identity-store locations.

Six places in the tree used to resolve these stores, each with its own hardcoded
per-platform list and no shared helper, so they drifted apart one reader at a
time (#6352). This module is that shared source of truth: an ordered table of
``(platform, home_relative_dir, product, trust)`` rows, plus the small set of
projections every reader needs. Every other reader is now a thin wrapper over a
projection here, so a new location is added ONCE.

Two properties are enforced by the table's shape, not by scattered convention:

* **TRUSTED implies FENCED.** A store is trusted (``from_cli_store=True`` at the
  usage reader) only when an agent file tool cannot write it, and that fence is
  home-anchored at exactly these directories. So every row is fenced; the
  ``trust`` column only distinguishes kiro-cli's OWN credential (``TRUSTED``)
  from another product's readable-but-not-owned store (``OTHER``).
* **Ordering is preserved.** The projections emit rows in the table's order,
  which is the exact order the pre-refactor readers used
  (``.local/share`` -> ``Library/Application Support`` -> ``AppData/Local`` ->
  ``AppData/Roaming``). Golden tests freeze that order.

LEAF module: stdlib-only imports so any module (including ``security.py``, which
is imported very early) can depend on it without a cycle.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

# The on-disk filename of every identity/state SQLite database. One constant so
# the six sites that used to spell ``"data.sqlite3"`` inline cannot drift.
AUTH_SQLITE_DB = "data.sqlite3"

# SQLite records a transaction in a sidecar file beside the database and folds it
# back into the main file only on a checkpoint, so a sidecar holds the same rows --
# for an auth store, the same live bearer token -- as the database itself.
# ``-wal``/``-shm`` are the write-ahead pair a WAL-mode store runs with; ``-journal``
# is the rollback journal a non-WAL connection writes instead. A consumer that
# fences the store by NAME needs all four spellings; one that fences the store's
# whole DIRECTORY covers them without this list.
AUTH_SQLITE_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")


class Platform(enum.Enum):
    """The three platform families the stores are laid out for."""

    DARWIN = "darwin"
    WIN32 = "win32"
    POSIX = "posix"


class Product(enum.Enum):
    """The two products whose identity stores are resolved here."""

    KIRO_CLI = "kiro-cli"
    AMAZON_Q = "amazon-q"


class Trust(enum.Enum):
    """Whether a store is kiro-cli's OWN credential or another product's."""

    TRUSTED = "trusted"
    OTHER = "other"


@dataclass(frozen=True)
class StoreRoot:
    """One identity-store location, as a home-relative directory.

    ``home_relative_dir`` is a forward-slash POSIX string (e.g.
    ``".local/share/kiro-cli"``); callers join it under a concrete home. It is
    the value the fence (``_SENSITIVE_HOME_DIRS``) is expressed in, so
    :func:`fenced_home_dirs` returns these strings verbatim.

    ``env_var`` names the environment variable the CLI resolves this location's
    ROOT from (``XDG_DATA_HOME`` / ``LOCALAPPDATA`` / ``APPDATA``), or ``None``
    when the location is a fixed home anchor (macOS). It is honoured only on the
    SOURCE side of staging and state-db discovery -- never for the fenced /
    trusted lists, which stay anchored so a redirected path cannot become a
    forgeable trusted path.

    ``trust`` is DERIVED, not stored: kiro-cli's own stores are TRUSTED, every
    other product's are OTHER. One bit lives in one place (``product``), so the
    two axes cannot drift apart -- storing trust as an independent column would
    re-seed exactly the redundancy this table exists to remove -- while trust
    remains a real, consumable concept (:func:`sqlite_dbs` keys on it).
    """

    platform: Platform
    home_relative_dir: str
    product: Product
    env_var: Optional[str] = None

    @property
    def trust(self) -> Trust:
        return Trust.TRUSTED if self.product is Product.KIRO_CLI else Trust.OTHER


# The canonical, ordered table. Order is load-bearing: the projections emit in
# this order, matching every pre-refactor reader.
#
# TRUSTED implies FENCED holds by construction -- every row is a fenced store
# directory, and only kiro-cli rows are TRUSTED.
#
# Within each platform: Local before Roaming (Windows), kiro-cli before amazon-q.
IDENTITY_STORE_ROOTS: tuple[StoreRoot, ...] = (
    # ── POSIX (Linux): ~/.local/share/<product>, root from XDG_DATA_HOME ──
    StoreRoot(Platform.POSIX, ".local/share/kiro-cli", Product.KIRO_CLI, "XDG_DATA_HOME"),
    StoreRoot(Platform.POSIX, ".local/share/amazon-q", Product.AMAZON_Q, "XDG_DATA_HOME"),
    # ── macOS: ~/Library/Application Support/<product>, fixed anchor (env None) ──
    StoreRoot(
        Platform.DARWIN,
        "Library/Application Support/kiro-cli",
        Product.KIRO_CLI,
        None,
    ),
    StoreRoot(Platform.DARWIN, "Library/Application Support/amazon-q", Product.AMAZON_Q, None),
    # ── Windows Local: ~/AppData/Local/<product>, root from LOCALAPPDATA ──
    StoreRoot(Platform.WIN32, "AppData/Local/kiro-cli", Product.KIRO_CLI, "LOCALAPPDATA"),
    StoreRoot(Platform.WIN32, "AppData/Local/amazon-q", Product.AMAZON_Q, "LOCALAPPDATA"),
    # ── Windows Roaming: ~/AppData/Roaming/<product>, root from APPDATA ──
    StoreRoot(Platform.WIN32, "AppData/Roaming/kiro-cli", Product.KIRO_CLI, "APPDATA"),
    StoreRoot(Platform.WIN32, "AppData/Roaming/amazon-q", Product.AMAZON_Q, "APPDATA"),
)


def fenced_home_dirs() -> tuple[str, ...]:
    """Every store directory, home-relative, in table order.

    Spliced into ``security._SENSITIVE_HOME_DIRS`` so agent file tools cannot
    read any identity store (or its WAL/SHM/journal sidecars). Returns all eight
    directories -- both products, all platform layouts -- because the fence is
    the floor under both the TRUSTED and OTHER read lists.
    """

    return tuple(root.home_relative_dir for root in IDENTITY_STORE_ROOTS)


def _rows_for_product(product: Product) -> tuple[StoreRoot, ...]:
    return tuple(root for root in IDENTITY_STORE_ROOTS if root.product is product)


def sqlite_dbs(trust: Trust, home: Optional[Path] = None) -> tuple[Path, ...]:
    """Absolute ``<home>/<dir>/data.sqlite3`` paths for one trust class, table order.

    Keyed on :class:`Trust` -- the consumer's real question is "which stores are
    kiro-cli's own credential vs another product's" -- so the module's headline
    TRUSTED-implies-FENCED invariant is load-bearing here, not documentary.
    Order is ``.local/share`` -> ``Library/Application Support`` ->
    ``AppData/Local`` -> ``AppData/Roaming`` -- the exact order the read lists
    (``_CLI_SQLITE_DBS`` / ``_OTHER_SQLITE_DBS``) used before the refactor, so
    the resulting tuples are byte-identical. ``home`` defaults to
    :meth:`Path.home` so the module-level tuples match the old ``Path.home()``
    idiom.
    """

    base = Path.home() if home is None else home
    if not isinstance(trust, Trust):
        # Loud misuse guard: Trust and Product are distinct enums, so a Product
        # member silently matches no row and the projection returns an EMPTY
        # tuple -- a subset test over it would pass vacuously (this exact
        # mistake shipped once). Tests are not mypy-checked; this is.
        raise TypeError(f"sqlite_dbs is keyed on Trust, got {trust!r}")
    return tuple(
        base.joinpath(*root.home_relative_dir.split("/")) / AUTH_SQLITE_DB
        for root in IDENTITY_STORE_ROOTS
        if root.trust is trust
    )


def _store_write_time(db: Path) -> float:
    """Newest write across a store's main file and its WAL sidecar.

    The store runs in SQLite WAL mode: a commit lands in the ``-wal`` sidecar and
    the main file's mtime does not advance until a checkpoint, so the main file
    alone under-reports recency on exactly the side being written. The ``-shm``
    index is not consulted -- it is mapped shared memory, not a write record.
    Raises ``OSError`` if the main file vanished; a missing sidecar is normal.
    """

    newest = db.stat().st_mtime
    wal = db.with_name(db.name + "-wal")
    try:
        newest = max(newest, wal.stat().st_mtime)
    except OSError:
        pass
    return newest


def selected_store(
    platform: str,
    home: Path,
) -> Path:
    """The single live kiro-cli store path on ``platform``, from fixed anchors.

    Hardwired to kiro-cli: the logout-detection fingerprint is deliberately NOT
    amazon-q's (a recorded pre-refactor decision), and every caller selects the
    CLI's own store -- so no product knob is offered that could loosen that.

    Candidates are PROJECTED from :data:`IDENTITY_STORE_ROOTS` (filtered to this
    platform's kiro-cli rows), so a relocated row reaches live-store selection
    the same way it reaches the fence, the sqlite tuples, staging, and state-db
    discovery. No environment variable is consulted -- and none is accepted --
    because the fence that makes these stores unwritable by agent file tools is
    anchored at exactly these paths, so a redirected location either resolves
    here or falls outside the fence where an agent could forge the rows.

    On Windows, when BOTH the Local and Roaming stores exist the most recently
    written one wins (WAL-aware, per store), so a leftover in the abandoned root
    never masks the live account; equal timestamps prefer Local (the first
    table row -- the current layout); and when neither exists the Local anchor
    is returned as the safe default.
    """

    plat = Platform(platform) if platform in {"darwin", "win32"} else Platform.POSIX
    candidates = [
        home.joinpath(*root.home_relative_dir.split("/")) / AUTH_SQLITE_DB
        for root in _rows_for_product(Product.KIRO_CLI)
        if root.platform is plat
    ]
    if len(candidates) == 1:
        return candidates[0]
    # Windows: table order is Local (current layout) then Roaming (legacy).
    local, roaming = candidates
    if not roaming.exists():
        return local
    if not local.exists():
        return roaming
    try:
        if _store_write_time(roaming) > _store_write_time(local):
            return roaming
    except OSError:
        pass
    return local


@dataclass(frozen=True)
class StoreMapping:
    """One source store directory mapped to its fixed staged-relative directory.

    ``env_var`` on the row governs the SOURCE side only: when set and present in
    ``environ`` it re-roots the source, since that is where this host's real
    store lives. The staged side is always the fixed default layout the staged
    environment re-points those variables at.
    """

    source: Path
    staged_relative: Path
    product: Product


def _source_root(root: StoreRoot, home: Path, environ: Mapping[str, str]) -> Path:
    """Resolve one row's SOURCE directory, honouring its env var if set.

    macOS rows carry ``env_var=None`` (fixed anchor). Windows/POSIX rows re-root
    only their FIRST path segment from the env var (``AppData/Local`` ->
    ``<LOCALAPPDATA>``, ``.local/share`` -> ``<XDG_DATA_HOME>``), keeping the
    product-name tail; an unset variable falls back to the home anchor.
    """

    segments = root.home_relative_dir.split("/")
    tail = segments[-1]
    default_root = home.joinpath(*segments[:-1])
    if root.env_var:
        override = environ.get(root.env_var)
        if override:
            return Path(override) / tail
    return default_root / tail


def store_mappings(
    platform: str,
    home: Path,
    environ: Mapping[str, str],
) -> tuple[StoreMapping, ...]:
    """Source->staged directory mappings for one platform, both products.

    Emitted PRODUCT-MAJOR (all kiro-cli mappings, then all amazon-q), matching
    the exact order the pre-refactor ``_auth_store_mappings`` loop produced --
    on Windows that is kiro-cli Local, kiro-cli Roaming, amazon-q Local,
    amazon-q Roaming. The env var is honoured on the SOURCE side only;
    ``staged_relative`` is the fixed default layout regardless of redirection.
    """

    plat = Platform(platform) if platform in {"darwin", "win32"} else Platform.POSIX
    mappings: list[StoreMapping] = []
    for product in Product:
        for root in IDENTITY_STORE_ROOTS:
            if root.platform is not plat or root.product is not product:
                continue
            mappings.append(
                StoreMapping(
                    source=_source_root(root, home, environ),
                    staged_relative=Path(*root.home_relative_dir.split("/")),
                    product=root.product,
                )
            )
    return tuple(mappings)


def state_db_candidates(
    platform: str,
    home: Path,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    """Candidate kiro-cli state-database paths, most likely first.

    Hardwired to kiro-cli (every caller probes the CLI's own state database;
    see :func:`selected_store` for the recorded not-amazon-q decision).
    Mirrors the per-platform data directories the readiness probe stages from,
    honouring ``XDG_DATA_HOME`` (POSIX) and ``LOCALAPPDATA`` (the current Windows
    layout) on the source side, so a host with a relocated data dir is not
    silently treated as having no store. The Windows Roaming candidate is a FIXED
    home anchor -- ``APPDATA`` is deliberately NOT followed here (matching the
    pre-refactor ``kiro_cli.kiro_cli_state_dbs``): the current generation writes
    the ``LOCALAPPDATA`` location, and the roaming default is retained only as a
    legacy fallback. Deduped while preserving first-seen order (Local before
    Roaming on Windows).
    """

    plat = Platform(platform) if platform in {"darwin", "win32"} else Platform.POSIX
    roots: list[Path] = []
    for root in _rows_for_product(Product.KIRO_CLI):
        if root.platform is not plat:
            continue
        # Roaming (APPDATA) is a fixed anchor for state-db discovery; every other
        # row follows its env var when set.
        if root.env_var == "APPDATA":
            roots.append(home.joinpath(*root.home_relative_dir.split("/")))
        else:
            roots.append(_source_root(root, home, environ))
    unique = list(dict.fromkeys(roots))
    return tuple(root / AUTH_SQLITE_DB for root in unique)
