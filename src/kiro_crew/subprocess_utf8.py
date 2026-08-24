"""Pinned UTF-8 text decoding for subprocesses whose output encoding is known.

## The failure class this closes

A subprocess call in text mode (``text=True`` or ``universal_newlines=True``)
with no explicit ``encoding=`` decodes the child's output with
``locale.getpreferredencoding(False)``. On POSIX that is effectively always
UTF-8, so the bug is invisible where most development happens. On Windows it is
the legacy ANSI code page -- cp1252, cp936, cp949, depending on the system
locale -- so any non-ASCII byte the child prints comes back as mojibake or, with
strict decoding, a ``UnicodeDecodeError``. Issue #3219 was this exact class
surfacing in the dashboard's file diffs; #3669 fixed the two confirmed sites
inline. This module is the prevention half (#5249): one shared definition of
"decode this child as UTF-8" so new call sites cannot re-forget the encoding,
enforced by ``scripts/check_subprocess_encoding.py`` in CI.

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

``errors="replace"`` matches the shape #3669 established: a malformed byte in
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
# ``text=True`` stays in the mapping so a call site that previously asserted
# ``text is True`` in a spy keeps seeing it.
UTF8_TEXT: Mapping[str, Any] = MappingProxyType(
    {"text": True, "encoding": "utf-8", "errors": "replace"}
)
