"""What a view hands kiro-cli beyond its authored spec, and how kiro-cli is made to load it.

The display text a view drops or bounds, with the oversized-source warning and its
bounded memory; the search tool a search view gains; and the in-place rewrite
kiro-cli's file watcher reloads on (:func:`announce_alias`).

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from pathlib import Path
from typing import Any

from kiro_crew.acp.skill_projection_parts.side_files import _LEGACY_ALIAS_NAME_RE
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


_SEARCH_TOOL = "@kirocrew-core/skill_search"

# Display-only fields kiro-cli copies out of EVERY agent spec in the directory into
# each ``session/new`` / ``session/load`` reply (its per-agent info: name,
# description, source, welcomeMessage). A projected alias is a second copy of an
# agent the host already lists, so its copy of that text only doubles the reply,
# and past the stdout frame limit the reply is dropped. Kiro Crew renders an
# agent's welcomeMessage from the AUTHORED spec (agent_discovery), never from an
# alias, so the alias leaves it out and keeps a short description.
_ALIAS_DROPPED_DISPLAY_FIELDS = ("welcomeMessage",)
_ALIAS_DESCRIPTION_MAX_CHARS = 512
# Warn when an authored spec's display text alone is this large: kiro-cli sends it
# in every session reply however Kiro Crew projects it, so the fix is at the source.
_DISPLAY_TEXT_WARN_BYTES = 32 * 1024
# ... and when the whole directory's display text approaches the frame limit.
_DISPLAY_TEXT_TOTAL_WARN_BYTES = 4 * 1024 * 1024
_DISPLAY_TEXT_WARNED: set[tuple[str, int]] = set()  # (sha256 of path, size)
_DISPLAY_TEXT_WARNED_MAX = 256
# The ACP readers' stdout frame limit (``acp.runtime._STDOUT_BUFFER_LIMIT``; a test
# pins the two equal). Not imported: this module is imported by the runtime.
_STDOUT_FRAME_LIMIT_BYTES = 10 * 1024 * 1024


def _loggable(value: str) -> str:
    """*value* (an agent name or path read from disk) as one bounded, escaped token."""
    return repr(value[:_LOGGED_NAME_MAX_CHARS])


_LOGGED_NAME_MAX_CHARS = 240


def _display_text_bytes(spec: dict[str, Any]) -> int:
    """UTF-8 bytes of the display-only text kiro-cli ships for *spec* per session reply."""
    total = 0
    for key in ("description", *_ALIAS_DROPPED_DISPLAY_FIELDS):
        value = spec.get(key)
        if isinstance(value, str):
            total += len(value.encode("utf-8", "replace"))
    return total


def _strip_alias_display_text(view: dict[str, Any]) -> None:
    """Keep an alias view's per-session-reply footprint small (see above)."""
    for key in _ALIAS_DROPPED_DISPLAY_FIELDS:
        view.pop(key, None)
    description = view.get("description")
    if isinstance(description, str) and len(description) > _ALIAS_DESCRIPTION_MAX_CHARS:
        view["description"] = description[: _ALIAS_DESCRIPTION_MAX_CHARS - 1] + "\u2026"


def _warn_on_display_text(sizes: dict[str, tuple[int, str]]) -> None:
    """Name the agents whose display text inflates every session reply; once per size.

    *sizes* maps agent name to (display bytes, source path). The source path names
    the package that installed the spec, which is who can trim it.
    """
    large = sorted(
        (
            (size, name, path)
            for name, (size, path) in sizes.items()
            if size >= _DISPLAY_TEXT_WARN_BYTES
        ),
        reverse=True,
    )
    omitted = 0
    for size, name, path in large:
        entry = (hashlib.sha256(path.encode("utf-8", "replace")).hexdigest(), size)
        if entry in _DISPLAY_TEXT_WARNED:
            continue
        if len(_DISPLAY_TEXT_WARNED) >= _DISPLAY_TEXT_WARNED_MAX:
            # The bound holds per insertion: past it, the rest are only counted.
            omitted += 1
            continue
        _DISPLAY_TEXT_WARNED.add(entry)
        logger.warning(
            "skill projection: agent %s (%s) carries %d KB of description/welcomeMessage "
            "text; kiro-cli sends it for every agent in every session/new reply, which "
            "Kiro Crew drops past %d MB. Skill-view aliases leave it out; trim it at the source.",
            _loggable(name),
            _loggable(path),
            size // 1024,
            _STDOUT_FRAME_LIMIT_BYTES // (1024 * 1024),
        )
    if omitted:
        logger.warning(
            "skill projection: %d more agent(s) carry over %d KB of description/"
            "welcomeMessage text each (not named: the warning memory is full)",
            omitted,
            _DISPLAY_TEXT_WARN_BYTES // 1024,
        )
    total = sum(size for size, _path in sizes.values())
    key = ("<total>", total)
    if (
        total >= _DISPLAY_TEXT_TOTAL_WARN_BYTES
        and key not in _DISPLAY_TEXT_WARNED
        and len(_DISPLAY_TEXT_WARNED) < _DISPLAY_TEXT_WARNED_MAX
    ):
        _DISPLAY_TEXT_WARNED.add(key)
        logger.warning(
            "skill projection: the agents directory carries %d KB of description/"
            "welcomeMessage text across %d agent(s), near the %d MB session/new reply "
            "limit; largest: %s",
            total // 1024,
            len(sizes),
            _STDOUT_FRAME_LIMIT_BYTES // (1024 * 1024),
            ", ".join(f"{_loggable(name)} ({size // 1024} KB)" for size, name, _path in large[:3])
            or "-",
        )


def _announce_publication(path: Path, data: bytes) -> None:
    """Make a watcher that ignores renames notice the alias just published at *path*.

    ``atomic_write`` publishes by rename, and kiro-cli (every release since its
    agent-config hot reload landed in 2.10.0, 2.26.0 included) does not act on
    one: its watcher reloads only on a create, a DATA modification or a remove of
    a ``*.json`` in the agents directory, so the rename (a name modification) is
    dropped, and so are the temp file's own events (not ``*.json``), an attribute
    change and a close without a write. ``session/set_mode`` then looks only in
    what was loaded. A NEW alias renamed in after the process started therefore
    stays ``Mode ... not found`` indefinitely, and a KNOWN alias renamed over
    keeps serving its old content. Rewriting the same bytes in place is
    a data write, so it triggers a full rescan after a 500 ms quiet window
    (measured: visible ~0.7 s later); and because every byte written equals the
    byte already there and nothing is truncated, a reader racing it can never
    see a torn file. Best effort: the file must still hold exactly *data*, and
    any failure leaves it as published.
    """
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        logger.debug("skill projection: cannot reopen %s to announce it", path.name, exc_info=True)
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
            return
        current = b""
        while len(current) < len(data):
            chunk = os.read(fd, len(data) - len(current))
            if not chunk:
                break
            current += chunk
        if current != data:
            return
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        logger.debug("skill projection: cannot announce %s", path.name, exc_info=True)
    finally:
        try:
            os.close(fd)
        except OSError:
            # A deferred write error (network filesystems report one at close) is
            # no reason to fail a best-effort nudge, or the session start behind it.
            logger.debug("skill projection: close after announcing %s", path.name, exc_info=True)


def announce_alias(alias: str) -> None:
    """Rewrite published alias *alias* in place, byte for byte, so the host rescans.

    The recovery ``session/set_mode`` takes on a ``Mode ... not found`` for a
    fresh alias before it retries: see :func:`_announce_publication`. Only a name
    of the shape the projection mints is touched. Blocking; best effort.
    """
    if not _LEGACY_ALIAS_NAME_RE.fullmatch(alias):
        return
    path = kiro_agents_dir() / f"{alias}.json"
    try:
        data = safe_read_file_bytes(str(path))
    except (OSError, ValueError, FileTooLargeError):
        return
    if data:
        _announce_publication(path, data)
