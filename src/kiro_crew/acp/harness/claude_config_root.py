"""Isolated Claude Code config root (``CLAUDE_CONFIG_DIR``).

Closes the "inherited global ``~/.claude``" governance gap documented in
``docs/system-specs/modules/claude-code-provider.md`` (*Known gap: the user's
global ``~/.claude`` is inherited*). With no ``CLAUDE_CONFIG_DIR`` set, the
``claude-agent-acp`` adapter and the SDK read the operator's real ``~/.claude``,
whose ``permissions.allow`` entries MERGE across sources and auto-approve tool
calls past Crew's gate -- Claude's own engine never calls ``canUseTool`` for a
pre-approved tool, so for that call there is nothing for Crew to gate or record.

Pointing ``CLAUDE_CONFIG_DIR`` at a Crew-owned root that carries NO
``permissions`` keys removes that channel at its source: the user tier a Claude
session loads no longer carries any ``allow`` or ``acceptEdits`` rule, so every
tool call falls through to ``canUseTool`` and runs Crew's approval path. It is
the config-isolation half of WS-B; the OS credential mask (an ENFORCED routing
for this harness) is a separate change.

The root is ``~/.claudecrew`` by default (``KIROCREW_CLAUDE_CONFIG_DIR``
overrides the location). It is seeded ONCE, idempotently, from the operator's
own ``~/.claude``:

* ``settings.json`` is copied with the whole ``permissions`` key stripped, so
  model, env and theme preferences carry while no inherited allow rule does. A
  settings file already present in the root is left untouched -- a restart never
  clobbers what the operator (or a prior seed) put there.
* Where the credential store is a FILE (Linux / WSL), ``.credentials.json`` is
  copied so the session can authenticate without a fresh sign-in. On macOS the
  credential lives in the login Keychain keyed by the config directory, so it
  cannot be copied: the operator signs in once with ``claude`` while
  ``CLAUDE_CONFIG_DIR`` points at this root, and a log line says so.

The credential FENCE already follows this override. ``.claude/.credentials.json``
is a declared credential leaf (``agent_sdk.host_auth``) spliced into
``security._SENSITIVE_HOME_DIRS``, and ``CLAUDE_CONFIG_DIR`` is one of its
``home_override_env_vars``, so ``override_anchored_leaves`` re-anchors the leaf
under this root for the sandbox and the read gate. The home-relative
``~/.claude/.credentials.json`` stays fenced too, so setting the override only
ADDS the new location to the fence -- it widens nothing.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import MutableMapping

from kiro_crew.acp.claude_settings_sources import read_claude_settings_bytes
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

#: Operator override for WHERE the isolated root lives. Distinct from
#: ``CLAUDE_CONFIG_DIR`` itself, which an operator sets to opt OUT of isolation
#: entirely (they point the adapter wherever they like and this module stands
#: aside). This one only relocates the Crew-owned root.
_ROOT_LOCATION_ENV = "KIROCREW_CLAUDE_CONFIG_DIR"

#: Default root. A dotted sibling of ``~/.claude`` so that, on macOS, the login
#: Keychain keys a self-contained credential entry per config directory.
_DEFAULT_ROOT_NAME = ".claudecrew"

_SETTINGS_NAME = "settings.json"
_CREDENTIALS_NAME = ".credentials.json"

#: Ceiling on the credential file we copy. The adapter's own file is small; a
#: pathological size is refused rather than read into memory.
_CREDENTIALS_MAX_BYTES = 1 << 20


def isolated_config_root() -> Path | None:
    """The Crew-owned Claude config root, or ``None`` when no home resolves."""
    override = os.environ.get(_ROOT_LOCATION_ENV)
    if override:
        return Path(override).expanduser()
    try:
        return Path.home() / _DEFAULT_ROOT_NAME
    except RuntimeError:
        return None


def _user_claude_dir() -> Path | None:
    """The operator's own ``~/.claude`` -- the seed SOURCE, never the target."""
    try:
        return Path.home() / ".claude"
    except RuntimeError:
        return None


def _seed_settings(source_dir: Path, root: Path) -> None:
    """Write ``root/settings.json`` from the operator's, with ``permissions`` gone.

    Leaves an existing target untouched. A source that is absent, a link, or
    unparseable yields an empty settings file rather than carrying anything
    across -- the security guarantee is the ABSENCE of inherited permissions, so
    failing toward an empty file is the safe direction.
    """
    target = root / _SETTINGS_NAME
    if target.exists():
        return
    carried: dict = {}
    source = source_dir / _SETTINGS_NAME
    try:
        raw = read_claude_settings_bytes(source, follow_links=False)
    except FileNotFoundError:
        raw = None
    if raw:
        try:
            parsed = loads_user_json(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            # Strip EVERY permission channel. ``permissions`` carries ``allow`` /
            # ``acceptEdits`` (the bypass this whole module closes); a top-level
            # ``defaultMode`` could reopen it too, so it goes as well.
            carried = {
                key: value
                for key, value in parsed.items()
                if key not in ("permissions", "defaultMode")
            }
    _atomic_write_private(target, (_dumps(carried) + "\n").encode("utf-8"))


def _seed_credentials(source_dir: Path, root: Path) -> None:
    """Copy the FILE-based credential into the root where one exists.

    No-op on macOS (the credential is in the login Keychain, not a file) and
    whenever the target already exists or the source cannot be read as a regular
    file. Same-UID, same-home copy: it carries the operator's own live token into
    the Crew-owned root so the session authenticates without a fresh sign-in.
    """
    if sys.platform == "darwin":
        return
    target = root / _CREDENTIALS_NAME
    if target.exists():
        return
    source = source_dir / _CREDENTIALS_NAME
    try:
        data = _read_private_file(source)
    except (FileNotFoundError, OSError):
        return
    if data is None:
        return
    _atomic_write_private(target, data)


def ensure_isolated_config_root() -> Path | None:
    """Create and seed the isolated root; return it, or ``None`` when unavailable.

    Idempotent: safe to call on every gateway boot. Any failure degrades to
    ``None`` and is logged, so a seeding problem never aborts startup -- the
    caller then leaves ``CLAUDE_CONFIG_DIR`` as it found it.
    """
    root = isolated_config_root()
    if root is None:
        return None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create isolated Claude config root %s: %s", root, exc)
        return None
    source_dir = _user_claude_dir()
    if source_dir is not None:
        try:
            _seed_settings(source_dir, root)
            _seed_credentials(source_dir, root)
        except OSError as exc:
            logger.warning("Could not seed isolated Claude config root %s: %s", root, exc)
            return None
    if sys.platform == "darwin" and not (root / _CREDENTIALS_NAME).exists():
        logger.info(
            "Isolated Claude config root %s has no sign-in yet. Run `claude` once "
            "with CLAUDE_CONFIG_DIR set to this path and complete its sign-in.",
            root,
        )
    return root


def apply_to_environ(environ: MutableMapping[str, str]) -> None:
    """Set ``CLAUDE_CONFIG_DIR`` to the isolated root, unless already set.

    An operator who set ``CLAUDE_CONFIG_DIR`` themselves has opted out of Crew's
    isolation and always wins -- the same "only when unset" rule the adapter's
    ``apply_spawn_env`` uses for ``CLAUDE_CODE_EXECUTABLE``.

    Setting it in the GATEWAY process environment is the single lever: the harness
    child inherits it at spawn, and the sandbox floor / read gate read the live
    value when they anchor the credential leaf, so one assignment relocates both
    the adapter's config and the fence that guards its token.
    """
    if environ.get("CLAUDE_CONFIG_DIR"):
        return
    root = ensure_isolated_config_root()
    if root is not None:
        environ["CLAUDE_CONFIG_DIR"] = str(root)


def _dumps(value: dict) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True)


def _read_private_file(path: Path) -> bytes | None:
    """Bounded, no-follow read of a regular file; ``None`` if it is not one."""
    try:
        if path.is_symlink():
            return None
    except OSError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not os.path.isfile(path):
            return None
        data = os.read(fd, _CREDENTIALS_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _CREDENTIALS_MAX_BYTES:
        return None
    return data


def _atomic_write_private(target: Path, data: bytes) -> None:
    """Create *target* at ``0o600`` via an ``O_EXCL`` temp sibling and rename.

    ``O_EXCL`` on the temp means two boots racing the same seed cannot clobber
    each other, and a pre-existing target is never overwritten because callers
    check ``exists()`` first and the final rename is the only writer.
    """
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
