"""Spill-to-file for oversized MCP tool responses.

When a tool/call result exceeds ``response_spill_threshold_bytes``, the full
text payload is written to a sidecar file under ``~/.kirocrew/mcp_spill/`` and
the in-band response is truncated to the first 16 KiB plus a marker telling
the agent where to find the full content. Non-tool-result frames, errors, and
small responses pass through untouched.

Any failure in the spill path (disk full, permission denied, parse error) is
swallowed and the original unmodified response forwarded — the happy path is
never broken.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat as stat_mod
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Truncation prefix size — first 16 KiB of the original text content kept
# inline so the agent has enough context to decide whether to read the file.
_INLINE_PREFIX_BYTES = 16 * 1024

# Spill directory (created mode 0700 on first use).
_SPILL_DIR_NAME = "mcp_spill"

# Max age (seconds) for spill files — older ones are cleaned on startup.
_SPILL_MAX_AGE_SECS = 24 * 60 * 60  # 24 hours

# Regex to extract a JSON-RPC id from the first ~4 KiB of a drained oversize
# chunk. Handles both string ids ("id":"some-string") and integer ids (id:42).
_ID_REGEX = re.compile(rb'"id"\s*:\s*("(?:[^"\\]|\\.)*?"|\d+)')


def _spill_dir() -> Path:
    """Return the spill directory path (does NOT create it)."""
    mc_home = os.environ.get("KIROCREW_HOME") or os.path.expanduser("~/.kirocrew")
    return Path(mc_home) / _SPILL_DIR_NAME


def _sanitize_server_name(name: str) -> str:
    """Remove characters unsafe for filenames."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:64]


def cleanup_old_spill_files() -> int:
    """Delete spill files older than 24h. Returns count deleted. Best-effort.

    Symlink-hardened: ``is_dir()``/``is_file()``/``stat()`` all FOLLOW
    symlinks, so an agent that swapped the spill dir (or planted a link
    inside it) could otherwise aim this sweep at an arbitrary directory and
    have the gateway delete 24h-old files there (confused deputy). The dir
    itself and every entry are checked with lstat semantics; links are
    skipped, never followed.
    """
    spill_dir = _spill_dir()
    if spill_dir.is_symlink() or not spill_dir.is_dir():
        return 0
    now = time.time()
    deleted = 0
    try:
        for entry in spill_dir.iterdir():
            try:
                st = entry.lstat()  # never follows — a symlink reports itself
                if not stat_mod.S_ISREG(st.st_mode):
                    continue  # skip symlinks / dirs / anything non-regular
                if (now - st.st_mtime) > _SPILL_MAX_AGE_SECS:
                    entry.unlink()
                    deleted += 1
            except OSError:
                continue
    except OSError:
        pass
    if deleted:
        logger.info("spill cleanup: removed %d files older than 24h", deleted)
    return deleted


def extract_id_from_bytes(data: bytes) -> Optional[Any]:
    """Parse the JSON-RPC 'id' field from the first ~4 KiB of raw bytes.

    Returns the id value (str or int) or None if unparseable.
    """
    prefix = data[:4096]
    m = _ID_REGEX.search(prefix)
    if m is None:
        return None
    raw = m.group(1)
    if raw.startswith(b'"'):
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    else:
        try:
            return int(raw)
        except ValueError:
            return None


def maybe_spill_response(
    line: bytes,
    server_name: str,
    threshold_bytes: int,
) -> bytes:
    """If ``line`` is an oversized tool/call result, spill and truncate.

    Returns the (possibly rewritten) line to forward. On any failure
    returns the original ``line`` unmodified.
    """
    if len(line) <= threshold_bytes:
        return line

    try:
        msg = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line  # not JSON — pass through

    if not isinstance(msg, dict):
        return line

    # Only spill tool/call results (has result.content list with text items)
    result = msg.get("result")
    if not isinstance(result, dict):
        return line
    content_list = result.get("content")
    if not isinstance(content_list, list):
        return line

    # Check if this looks like an MCP tools/call result
    has_text_items = any(
        isinstance(item, dict) and item.get("type") == "text" and "text" in item
        for item in content_list
    )
    if not has_text_items:
        return line

    # Spill the full text to file
    try:
        msg_id = msg.get("id", "unknown")
        request_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(msg_id))[:64]
        timestamp = int(time.time())
        safe_server = _sanitize_server_name(server_name)
        filename = f"{safe_server}-{request_id}-{timestamp}.json"

        spill_dir = _spill_dir()
        # Never operate through a symlinked spill dir (agent-swappable): an
        # attacker-planted link would redirect the write outside ~/.kirocrew.
        if spill_dir.is_symlink():
            logger.warning("spill dir is a symlink — refusing to spill")
            return line
        spill_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        spill_path = spill_dir / filename

        # Exclusive, no-follow write: O_EXCL refuses ANY pre-existing entry
        # at the path (including a pre-planted symlink, so nothing can make
        # this write land on another file), and O_NOFOLLOW backstops it.
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        fd = os.open(str(spill_path), open_flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        abs_path = str(spill_path.resolve())

        # Truncate each text item's content to first 16 KiB + marker
        total_bytes = len(line)
        rewritten_content = []
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                text = item["text"]
                if len(text.encode("utf-8")) > _INLINE_PREFIX_BYTES:
                    truncated = text.encode("utf-8")[:_INLINE_PREFIX_BYTES].decode("utf-8", errors="ignore")
                    marker = (
                        f"\n\n[KiroCrew: response truncated -- full {total_bytes} bytes "
                        f"at {abs_path}. Read with bash: head/grep/jq.]"
                    )
                    rewritten_content.append({**item, "text": truncated + marker})
                else:
                    rewritten_content.append(item)
            else:
                rewritten_content.append(item)

        # Rebuild the response with truncated content
        rewritten_msg = dict(msg)
        rewritten_msg["result"] = {**result, "content": rewritten_content}
        rewritten_line = json.dumps(rewritten_msg, separators=(",", ":")).encode("utf-8") + b"\n"

        logger.info(
            "spill: %s response %s spilled to %s (%d bytes -> %d inline)",
            server_name, msg_id, abs_path, total_bytes, len(rewritten_line),
        )
        return rewritten_line

    except Exception:
        # Any failure in spill path -> forward original unmodified
        logger.warning(
            "spill: failed to write oversized response for %s; forwarding original",
            server_name, exc_info=True,
        )
        return line
