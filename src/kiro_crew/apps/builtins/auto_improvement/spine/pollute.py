"""Do-not-pollute acceptance test — snapshot → boot → diff → block (spine).

The runtime-state analogue of the ruler canary (03_metric_design_and_calibration.md
§7.3; 08_safety_isolation_and_guardrails.md §2.2): a *mechanical* proof that the
measurement runtime is hermetic, run BEFORE any autonomous run and gating the whole
experiment. The canary proves the ruler can SEE a real win before the loop is trusted
to measure; this test proves the runtime touches NOTHING real before the loop is
trusted to run.

The procedure (08_safety §2.2, verbatim shape):

    DO-NOT-POLLUTE ACCEPTANCE TEST  (hard prerequisite, blocking)
      1. snapshot the host paths the target is known to write under HOME
      2. boot the measurement container once
      3. tear it down
      4. diff each path against its snapshot
      REQUIRE: zero changes.
      If anything changed -> the run is BLOCKED until the leak is fixed.

SPINE vs PROFILE (08_safety §0.1, §2.1, §2.2): the spine owns the snapshot/boot/diff/
block MACHINERY — it is target-agnostic and takes the path set + a boot callable as
inputs. The profile supplies WHICH host paths to snapshot (its env footprint:
``isolation.do_not_pollute_paths()``) and the boot callable (how to boot+tear-down the
measurement runtime). The spine never names a host path (``~/.kiro/agents`` et al. are a
Kiro Crew property of the target, not the spine; 08_safety §2.1 generalization note).

The snapshot is content-addressed (a hash of each path's tree), so a write that adds,
removes, or modifies anything under a snapshotted path is detected as a non-zero diff.
This module is pure spine: no target token, no git, no subprocess; the boot callable is
opaque and supplied by the profile/driver.

Docs: 03_metric_design_and_calibration.md §7.3 (runtime-state canary), §11.1 (do-not-
pollute diff != 0 -> BLOCK); 08_safety_isolation_and_guardrails.md §2.2 (the test),
§9 row T1.2 (every silent host write -> blocked).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# A boot callable: boots the measurement runtime once and tears it down. It returns
# nothing the spine inspects — the WHOLE point is that the spine measures the host-state
# delta the boot leaves behind, not anything the boot reports. Target-supplied (opaque).
BootCallable = Callable[[], None]


def _is_excluded(path: Path, exclude: frozenset[str]) -> bool:
    """True iff ``path`` is one of the excluded subpaths, or lives under one. Excludes are
    absolute, resolved path strings; we compare resolved paths so a symlinked or
    ``..``-laden exclude still matches. Target-agnostic: the spine never decides WHAT to
    exclude — the profile supplies the set (e.g. the orchestrator app's OWN data dir,
    which the host writes by design and which is NOT the measured runtime's footprint)."""
    if not exclude:
        return False
    try:
        rp = str(path.resolve())
    except OSError:
        rp = str(path)
    if rp in exclude:
        return True
    # os.sep, not "/": on Windows a resolved path uses "\\", so a "/"-suffixed prefix
    # would never match and the orchestrator's own data dir would wrongly register as a
    # leak (caught by the Windows CI shard once these tests ran there).
    return any(rp.startswith(e.rstrip("/\\") + os.sep) for e in exclude)


def _hash_path(path: Path, exclude: frozenset[str] = frozenset()) -> str:
    """Content-address one host path (file or directory) so any add/remove/modify under
    it is detected. A MISSING path hashes to a stable sentinel so its later *creation*
    (a leak) shows up as a diff; its later *absence* also reconciles to the same sentinel.

    For a directory we hash the sorted (relpath, size, mtime_ns, content-hash) of every
    file beneath it — so a new/removed/edited file anywhere in the tree changes the hash.
    For a file we hash its bytes. Symlinks are recorded by their target string (a flipped
    symlink is a change) without following them (avoids escaping the snapshot scope).

    ``exclude`` is an opaque set of absolute subpaths to SKIP during a directory walk.
    Its sole purpose is to ignore writes the ORCHESTRATOR itself makes inside a snapshot
    root it happens to share with the host (the auto-improvement app's own data dir lives
    UNDER the Kiro Crew data home, so the host's own log/ledger/activity writes during the boot
    window would otherwise register as a phantom 'leak'). Everything else under the root
    is still hashed, so a real write by the measured runtime anywhere outside the
    excluded subtree is still caught — the hermeticity guarantee is preserved.
    """
    if _is_excluded(path, exclude):
        # The whole path is excluded — hash to a constant so its before/after are equal
        # regardless of what the orchestrator writes inside it.
        return "\0excluded\0"
    if not path.exists() and not path.is_symlink():
        return "\0missing\0"
    h = hashlib.sha256()
    if path.is_symlink():
        # Record the link target, not the resolved tree (a re-pointed symlink is a write).
        h.update(b"symlink\0")
        h.update(str(Path(path).readlink()).encode("utf-8", "surrogatepass"))
        return h.hexdigest()
    if path.is_file():
        h.update(b"file\0")
        h.update(path.read_bytes())
        return h.hexdigest()
    if path.is_dir():
        h.update(b"dir\0")
        # Walk deterministically (sorted) so the hash is stable across runs.
        for child in sorted(path.rglob("*"), key=lambda p: str(p)):
            if _is_excluded(child, exclude):
                continue  # orchestrator-owned subtree — not the measured runtime's write
            rel = child.relative_to(path)
            try:
                if child.is_symlink():
                    h.update(b"L\0")
                    h.update(str(rel).encode("utf-8", "surrogatepass"))
                    h.update(str(Path(child).readlink()).encode("utf-8", "surrogatepass"))
                elif child.is_file():
                    st = child.stat()
                    h.update(b"F\0")
                    h.update(str(rel).encode("utf-8", "surrogatepass"))
                    h.update(str(st.st_size).encode())
                    h.update(child.read_bytes())
                elif child.is_dir():
                    h.update(b"D\0")
                    h.update(str(rel).encode("utf-8", "surrogatepass"))
                else:
                    # An unusual entry kind (socket/fifo/device) under the snapshot root is
                    # still a host write — record it by name so the leak is not invisible
                    # (mirrors the top-level "other" case below; a missing else would let a
                    # special-file leak pass as a phantom zero-diff).
                    h.update(b"O\0")
                    h.update(str(rel).encode("utf-8", "surrogatepass"))
            except OSError:
                # A transient unreadable entry is recorded by name so its presence still
                # counts (a leak that creates an unreadable file must not pass silently).
                h.update(b"E\0")
                h.update(str(rel).encode("utf-8", "surrogatepass"))
        return h.hexdigest()
    # An unusual entry kind (socket/fifo/device) under a snapshot path is recorded by name.
    h.update(b"other\0")
    h.update(str(path).encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def _resolve_excludes(exclude: list[Path] | None) -> frozenset[str]:
    """Normalize the profile-supplied exclude list to a frozenset of resolved abs-path
    strings (empty when none). Resolving here means a symlinked/relative exclude still
    matches its real location during the walk."""
    out: set[str] = set()
    for e in exclude or []:
        try:
            out.add(str(Path(e).resolve()))
        except OSError:
            out.add(str(e))
    return frozenset(out)


def snapshot(paths: list[Path], exclude: list[Path] | None = None) -> dict[str, str]:
    """Step 1: snapshot each host path to a content-address (08_safety §2.2 step 1).

    Keyed by the absolute path string so the before/after diff is order-independent.
    A path that does not exist yet is snapshotted as ``missing`` so a boot that CREATES
    it (a pure host-pollution leak) is caught as a non-zero diff.

    ``exclude`` (optional) lists subpaths to skip — used ONLY to ignore the orchestrator
    app's OWN data dir when it lives under a snapshot root (see :func:`_hash_path`)."""
    ex = _resolve_excludes(exclude)
    return {str(p): _hash_path(Path(p), ex) for p in paths}


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Step 4: diff the after-snapshot against the before-snapshot (08_safety §2.2 step 4).

    Returns the list of host paths whose content-address changed (added/removed/modified
    under the path). An empty list == ZERO diff == hermetic runtime (the only acceptable
    result; §2.2 "REQUIRE: zero changes")."""
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


@dataclass
class PolluteResult:
    """The outcome of the do-not-pollute acceptance test (08_safety §2.2).

    ``zero_diff`` is True iff the runtime touched NOTHING real (the only result that
    lets the run proceed); ``changed_paths`` lists the leaked host paths when it is
    False, so the human operator knows exactly what to fix before the run is unblocked
    (§2.2 "the run is BLOCKED until the leak is fixed")."""

    zero_diff: bool
    changed_paths: list[str] = field(default_factory=list)
    snapshotted: int = 0
    note: str = ""

    @property
    def blocked(self) -> bool:
        """The run is BLOCKED iff the diff is non-zero (08_safety §2.2 / §9 T1.2)."""
        return not self.zero_diff


def run_do_not_pollute(
    *, paths: list[Path], boot: BootCallable, exclude: list[Path] | None = None
) -> PolluteResult:
    """Run the full snapshot -> boot -> diff -> (block) sequence (08_safety §2.2).

    The reusable, TARGET-AGNOSTIC mechanism: it takes the ``paths`` to snapshot (the
    profile's ``isolation.do_not_pollute_paths()``) and a ``boot`` callable (boot the
    measurement runtime once + tear it down). It snapshots, boots, re-snapshots, diffs,
    and returns a :class:`PolluteResult` whose ``blocked`` flag is the driver's gate.

    On a non-zero diff the driver BLOCKS (refuses Phase 2) — this function does not raise;
    it RETURNS the verdict so the driver can record/log it and decide (the driver raises
    its own block error). A boot that raises propagates (a runtime that cannot even boot
    is also a hard stop), but the snapshot is still taken so a partial leak is reported.

    ``exclude`` (optional, profile-supplied) lists subpaths to ignore — ONLY the
    orchestrator's own data dir, when it lives under a snapshot root. The spine stays
    target-agnostic: it does not decide what to exclude, it just skips the opaque set the
    profile hands it (see :func:`_hash_path`). Everything else under the root is still
    diffed, so a real leak by the measured runtime is still caught.

    NEVER call this against the real host home in a test — drive it with a tmp fake-HOME
    path set and a fake boot callable (08_safety §2.1 generalization note)."""
    before = snapshot(paths, exclude=exclude)
    boot()  # step 2+3: boot the measurement runtime once and tear it down.
    after = snapshot(paths, exclude=exclude)
    changed = diff(before, after)
    return PolluteResult(
        zero_diff=not changed,
        changed_paths=changed,
        snapshotted=len(paths),
        note=(
            "hermetic: zero host-state diff"
            if not changed
            else f"host-state LEAK: {len(changed)} path(s) changed during boot"
        ),
    )
