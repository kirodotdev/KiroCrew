"""PPTX Maker — path resolution and containment.

The single sanitizer layer for this app. Every filesystem path that is derived
from a browser request goes through one of the ``resolve_*`` helpers here, each
of which returns either a path that is provably contained inside an allowed root
or ``None``. Callers MUST use the returned value and never the caller's original
string, so no untrusted path reaches a filesystem operation.

Three roots matter:

* ``DECK_ROOT`` — where the presentation engine writes decks. Resolved from
  (in order) ``KIROCREW_PPTX_DECK_ROOT`` (dev/test override), the engine's own
  ``config.json``, then the engine default. Deck artifacts are SERVED to the
  browser, so this is the root with the strictest containment.
* ``engine_root()`` — the vendored engine checkout under the app's data dir.
* ``user_config_dir()`` — the engine's user config dir (styles, templates,
  ``state.json``), which the library endpoints write to.

Containment is enforced twice on purpose: every path SEGMENT must match a
conservative allow-list (so ``..`` and separators can never appear), and the
resolved result must still be inside the root after symlinks are followed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.security import is_sensitive_path, redact

logger = logging.getLogger("kirocrew.app.pptx-maker")

APP_NAME = "pptx-maker"

# Per-segment allow-list. Deliberately excludes "/", "\" and "." runs, so no
# traversal or separator can survive it — this is the primary path guard and the
# resolve()-containment check below is the backstop.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Environment override, for development and tests only. Production resolves the
# deck root from the engine's own config so this app and the engine can never
# disagree about where decks live.
DECK_ROOT_ENV = "KIROCREW_PPTX_DECK_ROOT"

# The engine's own defaults, mirrored here so a deck root can be resolved
# before the engine is provisioned (the UI needs to render a path either way).
ENGINE_DEFAULT_DECK_ROOT = Path("Documents") / "SDPM-Presentations"
# Seeded for brand-new users: a config-dir location needs no OS file-access
# grant, unlike ~/Documents on macOS.
SEEDED_DECK_ROOT = "~/.config/sdpm/decks"

_ENGINE_DIRNAME = "sdpm"


def app_root() -> Path:
    """The app's writable data dir (``~/.kiro/crew/apps/pptx-maker/data``).

    A builtin app's *source* lives read-only inside the installed Python
    package, so everything this app provisions at runtime (the engine checkout,
    logs) lands here instead.
    """
    return app_data_dir(APP_NAME)


def engine_root() -> Path:
    """The vendored engine checkout root."""
    return app_root() / "vendor" / _ENGINE_DIRNAME


def engine_python() -> Path:
    """The engine venv's interpreter (created by the provision script)."""
    return engine_root() / "mcp-local" / ".venv" / "bin" / "python"


def preview_tools_root() -> Path:
    """Root of the app-managed preview-tool installs (``soffice``/``pdftoppm``).

    A sibling of the engine checkout under ``vendor/`` rather than inside it,
    because the engine tree is REPLACED wholesale on every version bump: a tool
    installed inside it would be discarded on each update and re-downloaded.
    """
    return app_root() / "vendor" / "preview-tools"


def preview_tools_bin() -> Path:
    """The single directory prepended to ``PATH`` for engine spawns.

    The engine resolves both binaries with ``shutil.which()``, so a managed
    install is only reachable if it is on the child's ``PATH``. Every managed
    tool therefore exposes its executable here — as the real file or a link to
    it inside its own unpacked tree — so exactly one directory has to be
    injected no matter how many tools are managed.
    """
    return preview_tools_root() / "bin"


def engine_mcp_dir() -> Path:
    """The engine's MCP project dir — the cwd every engine call uses."""
    return engine_root() / "mcp-local"


def engine_skill_dir() -> Path:
    """The engine's bundled skill tree (styles, templates, icon scripts)."""
    return engine_root() / "skill"


def engine_config_path() -> Path:
    """The engine's user ``config.json`` (mirrors its own config resolution)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base.expanduser() / _ENGINE_DIRNAME / "config.json"


def read_engine_config() -> dict:
    """The engine's user config, or ``{}`` when absent/unreadable/not an object."""
    try:
        data = json.loads(engine_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def deck_root() -> Path:
    """Resolve the deck root: env override, then engine config, then default.

    Resolved on every call rather than cached at import: the user can change the
    output directory from the app's settings, and a cached value would keep
    serving the old tree until the gateway restarted.
    """
    override = os.environ.get(DECK_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    configured = read_engine_config().get("output_dir")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser().resolve()
    return (Path.home() / ENGINE_DEFAULT_DECK_ROOT).resolve()


def _contained(candidate: Path, root: Path) -> Path | None:
    """Resolve *candidate* and return it only if it stays inside *root*.

    ``resolve()`` collapses ``..`` and follows symlinks, so the returned path is
    the one a filesystem call will actually act on. A path that escapes, or that
    the shared sensitive-path gate refuses, yields ``None``.
    """
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    if is_sensitive_path(str(resolved)):
        return None
    return resolved


def split_segments(subpath: str) -> list[str] | None:
    """Split a request subpath into allow-listed segments, or ``None``.

    Empty and ``.`` segments are dropped; anything that does not match
    :data:`SEGMENT_RE` (including ``..``, absolute prefixes and any separator
    that survived URL decoding) rejects the whole path.
    """
    parts = [p for p in subpath.split("/") if p not in ("", ".")]
    if not parts or not all(SEGMENT_RE.match(p) for p in parts):
        return None
    return parts


def resolve_deck_dir(deck_id: str) -> Path | None:
    """Resolve one deck's directory under the deck root, or ``None``.

    The deck root ITSELF is never a valid answer — a caller asking for a deck
    must land strictly inside it.

    A deck id that carries a CREDENTIAL is refused outright. The engine names deck
    directories from the model's own name for the deck, so the id is agent-authored
    text — and unlike every other agent-authored field it cannot simply be redacted on
    the way out: it is the directory name, the `preview/<deckId>/...` URL segment and
    the handle every later request sends back, so rewriting it would break the deck.
    Refusing the deck is the only option that neither leaks nor lies.
    `decks._deck_name` already redacts the display name; this was the same leak one
    field over, reached through `deckId` in the `/decks` response.
    """
    if not SEGMENT_RE.match(deck_id or ""):
        return None
    if redact(deck_id) != deck_id:
        logger.warning("pptx-maker: refusing a deck id that looks like a credential")
        return None
    root = deck_root()
    resolved = _contained(root / deck_id, root)
    if resolved is None or resolved == root.resolve():
        return None
    return resolved if resolved.is_dir() else None


def resolve_deck_file(deck_id: str, subpath: str) -> Path | None:
    """Resolve ``{deck_root}/{deck_id}/{subpath}`` to an existing file, or ``None``."""
    deck_dir = resolve_deck_dir(deck_id)
    if deck_dir is None:
        return None
    parts = split_segments(subpath)
    if parts is None:
        return None
    resolved = _contained(deck_dir / Path(*parts), deck_dir)
    if resolved is None or resolved == deck_dir:
        return None
    return resolved if resolved.is_file() else None


def contained_deck_file(deck_dir: Path, *parts: str) -> Path | None:
    """An existing file at ``deck_dir/<parts>``, or ``None``.

    The by-PATH counterpart to :func:`resolve_deck_file` (which starts from a
    ``deck_id``), for the internal readers that already hold a resolved deck
    directory. Same guarantees: ``_contained`` follows symlinks and applies the
    shared sensitive-path gate, so a deck file that links out of the deck — or at a
    credential path — resolves to ``None`` instead of being read.
    """
    resolved = _contained(deck_dir.joinpath(*parts), deck_dir)
    if resolved is None or resolved == deck_dir:
        return None
    return resolved if resolved.is_file() else None


def contained_deck_dir(deck_dir: Path, *parts: str) -> Path | None:
    """An existing DIRECTORY at ``deck_dir/<parts>``, or ``None``.

    The directory counterpart to :func:`contained_deck_file`, and it exists because
    having only the file version left the identical hole one level up: the readers
    resolved each *file* they opened but walked ``deck_dir / "slides"`` (and
    ``compose``/``preview``/``specs``) unresolved, and ``is_dir()``/``glob()`` FOLLOW
    symlinks. A deck shipping ``slides -> ~/.aws/sso/cache`` therefore had that
    directory's filenames enumerated and returned as slide slugs — the *names* leaked
    even where the file contents were still gated. Demonstrated before the fix: a
    linked ``slides`` reported ``['9c1e2f-token', 'botocore-client-id']``.

    Same guarantees as the file version: ``_contained`` resolves symlinks and applies
    the shared sensitive-path gate, so a subdirectory that links out of the deck — or at
    a credential path — resolves to ``None`` instead of being walked.
    """
    resolved = _contained(deck_dir.joinpath(*parts), deck_dir)
    if resolved is None or resolved == deck_dir:
        return None
    return resolved if resolved.is_dir() else None


def resolve_library_file(library_dir: Path, name: str, suffix: str) -> Path | None:
    """Resolve ``{library_dir}/{name}{suffix}`` for a style/template, or ``None``.

    Used for BOTH reads and writes, so the name allow-list and the containment
    check are shared by every library operation rather than re-derived per verb.
    The file need not exist (a create/rename target does not yet).
    """
    if not SEGMENT_RE.match(name or ""):
        return None
    return _contained(library_dir / f"{name}{suffix}", library_dir)
