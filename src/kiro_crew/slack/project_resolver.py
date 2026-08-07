"""Auto-resolve Slack channel names to project directories.

When ``slack.auto_project_dir`` is configured (a directory path containing
project folders), incoming messages from a channel whose name matches a
subdirectory get that directory as the session CWD.

Matching rules:
  1. Exact case-insensitive match of channel name to folder name.
  2. Prefix match: channel name is a prefix of a folder name separated at a
     non-alphanumeric boundary (e.g. channel ``myproject`` matches folder
     ``myproject-dev``).
  3. DMs are excluded (no meaningful channel name to match).

The resolver caches the directory listing for ``_DIR_CACHE_TTL_SECS`` to avoid
repeated filesystem scans on every message.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

_DIR_CACHE_TTL_SECS = 60.0

# Module-level cache: (base_dir, scan_time, [folder_names])
_dir_cache: tuple[str, float, list[str]] = ("", 0.0, [])


def _scan_project_dirs(base_dir: str) -> list[str]:
    """Return subdirectory names under *base_dir*, refreshing cache if stale."""
    global _dir_cache
    now = time.time()
    if _dir_cache[0] == base_dir and (now - _dir_cache[1]) < _DIR_CACHE_TTL_SECS:
        return _dir_cache[2]
    try:
        entries = [
            entry.name for entry in os.scandir(base_dir) if entry.is_dir(follow_symlinks=True)
        ]
    except OSError as exc:
        logger.warning("auto_project_dir: failed to scan %s: %s", base_dir, exc)
        entries = []
    _dir_cache = (base_dir, now, entries)
    return entries


def resolve_channel_project(
    channel_name: str | None,
    auto_project_dir: str,
) -> str | None:
    """Resolve a channel name to a project directory path, or None.

    Args:
        channel_name: The Slack channel name (e.g. ``"kirocrew"``).
            ``None`` or empty string returns ``None``.
        auto_project_dir: Base directory to scan for project folders.

    Returns:
        Absolute path to the matched project directory, or ``None``.
    """
    if not channel_name or not auto_project_dir:
        return None

    base = Path(auto_project_dir).expanduser()
    if not base.is_absolute():
        return None

    dirs = _scan_project_dirs(str(base))
    if not dirs:
        return None

    ch_lower = channel_name.lower().strip()
    if not ch_lower:
        return None

    # Pass 1: exact case-insensitive match
    for d in dirs:
        if d.lower() == ch_lower:
            resolved = str(base / d)
            if is_sensitive_path(resolved):
                logger.debug("auto_project_dir: %s is sensitive, skipping", resolved)
                return None
            logger.info("auto_project_dir: #%s → %s (exact match)", channel_name, resolved)
            return resolved

    # Pass 2: channel name matches folder with dashes/underscores stripped
    # e.g. channel "slack-kiro-bot" matches folder "slack-kiro-bot" or "SlackKiroBot"
    ch_normalized = re.sub(r"[^a-z0-9]", "", ch_lower)
    if len(ch_normalized) >= 3:
        for d in dirs:
            d_normalized = re.sub(r"[^a-z0-9]", "", d.lower())
            if d_normalized == ch_normalized:
                resolved = str(base / d)
                if is_sensitive_path(resolved):
                    return None
                logger.info(
                    "auto_project_dir: #%s → %s (normalized match)",
                    channel_name,
                    resolved,
                )
                return resolved

    return None
