"""Pinned UTF-8 text decoding for subprocesses whose output encoding is known.

## The failure class this closes

A subprocess call in text mode (``text=True`` or ``universal_newlines=True``)
with no explicit ``encoding=`` decodes the child's output with
``locale.getpreferredencoding(False)``. On POSIX that is effectively always
UTF-8, so the bug is invisible where most development happens. On Windows it is
the legacy ANSI code page -- cp1252, cp936, cp949, depending on the system
locale -- so any non-ASCII byte the child prints comes back as mojibake or, with
strict decoding, a ``UnicodeDecodeError``. This module is the prevention half: one
shared definition of "decode this child as UTF-8" so new call sites cannot
re-forget the encoding, enforced by ``scripts/check_subprocess_encoding.py`` in CI.

## When pinning UTF-8 is CORRECT -- and when it is not

Use this module only for children whose output encoding is KNOWABLE:

* ``git`` -- emits paths and blob content as raw bytes and its message strings
  as UTF-8; it does not transcode to the console code page.
* ``gh`` -- a Go binary; always writes UTF-8.
* Python children we spawn ourselves whose output we control.

Do NOT use it for children that genuinely write in the console/locale encoding
(``systeminfo``, ``wmic``, arbitrary user shells): pinning UTF-8 there trades
one mojibake for another. Those sites keep locale decoding on purpose and carry
the lint gate's opt-out marker instead.

## Why a kwargs mapping instead of a wrapper function

Two reasons, both structural:

* The test suite patches ``<module>.subprocess.run`` by name in dozens of
  places to stub out real spawns. A wrapper function imported into each module
  would route calls around those patches, silently turning stubbed tests into
  real ``git`` invocations. ``subprocess.run(..., **UTF8_TEXT)`` keeps every
  call going through the module's own ``subprocess`` attribute, so existing
  patches keep intercepting.
* ``test_spawn_audit`` requires every spawn primitive under ``src/kiro_crew``
  to be routed or individually justified. A generic pass-through spawn wrapper
  with caller-controlled argv would be a new unaudited primitive -- exactly
  what that audit exists to prevent. A mapping spawns nothing.

``errors="replace"`` is the deliberate shape: a malformed byte in
one path or commit message must degrade to U+FFFD in that spot, not throw away
the whole diff or crash the caller. The one place that policy is WRONG is a
payload that must round-trip byte-exactly back into a child (a captured diff
fed to ``git apply``): those sites pin ``errors="surrogateescape"`` inline on
both the decode and encode ends instead.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

# Splat into any subprocess.run / subprocess.Popen / subprocess.check_output
# call (or a kwargs-forwarding wrapper such as sandbox.run_limited) in place of
# ``text=True``. Passing ``encoding`` alone already implies text mode;
# ``text=True`` stays in the mapping so a call site that asserts
# ``text is True`` in a spy keeps seeing it.
UTF8_TEXT: Mapping[str, Any] = MappingProxyType(
    {"text": True, "encoding": "utf-8", "errors": "replace"}
)


def utf8_stdout(raw: bytes | str | None) -> str:
    """Decode a child's captured output as UTF-8 with no newline translation.

    Text mode cannot express this: ``subprocess`` wraps the pipe in a
    ``TextIOWrapper`` with universal newlines hard-enabled and exposes no
    ``newline=`` control, so every ``\\r`` the child prints is rewritten to
    ``\\n`` before the caller sees it. For output where a carriage return is
    CONTENT -- git prints paths byte-for-byte, and a POSIX path may legally
    contain ``\\r`` -- capture bytes (drop the ``UTF8_TEXT`` splat) and decode
    through this function instead. Same ``errors="replace"`` policy as
    ``UTF8_TEXT``, for the same reason.

    A ``str`` passes through unchanged, so a test stand-in that substitutes an
    already-decoded ``CompletedProcess`` keeps working; ``None`` (stream not
    captured) decodes as ``""``.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


def utf8_path_stdout(raw: bytes | str | None) -> str:
    """Decode a child's captured output that names a FILESYSTEM PATH.

    ``errors="replace"`` is wrong for a path: a byte that is not valid UTF-8
    becomes U+FFFD, a decoded string that fails to round-trip to the bytes
    the filesystem knows -- ``os.lstat`` then inspects a DIFFERENT path than
    git answered with, and a guard that clears on ``FileNotFoundError`` fails
    open for a path that exists. ``errors="surrogateescape"`` (PEP 383) maps
    each such byte to a lone surrogate that ``os.fsencode`` -- the encode step
    inside every ``os`` path call -- restores byte-exactly, so the ``lstat``
    lands on the path git actually printed. This is the module docstring's
    "must round-trip byte-exactly" policy, packaged for the git path probes
    (``rev-parse --absolute-git-dir``) that feed
    :func:`kiro_crew.git_worktree_scope.worktree_probe_failure_is_empty_scope`.

    Same pass-through contract as :func:`utf8_stdout`: ``str`` unchanged (test
    stand-ins), ``None`` decodes as ``""``. Never hand the result to a display
    or JSON surface -- a lone surrogate is unencodable there; this decoder is
    for values consumed by ``os`` path calls.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "surrogateescape")
