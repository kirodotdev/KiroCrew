"""One filesystem guard for every caller-influenced path in the benchmark harness.

Why a shared helper rather than a check at each call site: the first two rounds of
review on this code found the same class of hole twice, in mirror image. Round one
gated the report *read* (``bench compare <path>``); round two found the report
*write* (``--out-dir`` + ``--stem``) still ungated, which is strictly worse — a read
discloses, a write destroys. Fixing that one site would have left three more:
``--out-dir``'s ``mkdir``, and the corpus cache root, which ``KIROCREW_BENCH_CACHE``
can point anywhere. Point-wise patching is how the second hole survived the first
fix, so the guard lives in one place and every argv- or env-influenced path calls it.

The threat model is the same one that justifies the read gate. These values arrive
from argv and the environment, and in this product neither is necessarily set by the
human who owns the machine: an agent can run any CLI command. So

    kirocrew bench retrieval --out-dir ~/.kiro/crew --stem security_policy

is a reachable invocation that would overwrite a governance policy file with a
benchmark report. Nothing about the benchmark needs to write there, so it is refused
rather than made careful.

Write protection is deliberately stricter than read protection. ``is_sensitive_path``
answers "is this path inside a protected location"; for a directory that is about to
receive files, the question is also "does a protected location lie *under* it", which
is what ``path_contains_sensitive`` answers. A ``--out-dir`` of ``~`` is not itself
sensitive, but writing a tree there is not something this command should do.
"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(RuntimeError):
    """Raised instead of touching a protected location. Carries an actionable message."""


def _resolve(path: str | Path) -> Path:
    # Canonicalize before checking, so a symlink cannot launder the target. The
    # gate helpers do their own resolution too; doing it here keeps the message
    # honest about what was actually going to be touched.
    return Path(path).expanduser().resolve()


def guard_read_path(path: str | Path, *, what: str) -> Path:
    """Refuse to read *path* when it resolves into a protected location."""
    from kiro_crew.security import is_sensitive_path

    resolved = _resolve(path)
    if is_sensitive_path(str(resolved)):
        raise UnsafePathError(
            f"refusing to read the {what}: it resolves into a protected location "
            "(a credential store or the governance trust root). Nothing the "
            "benchmark needs lives there."
        )
    return resolved


def guard_write_path(path: str | Path, *, what: str) -> Path:
    """Refuse to write *path* when it is protected, or sits under a protected root."""
    from kiro_crew.security import is_sensitive_path

    resolved = _resolve(path)
    if is_sensitive_path(str(resolved)):
        raise UnsafePathError(
            f"refusing to write the {what} to {resolved.name!r}: the destination "
            "resolves into a protected location (a credential store or the "
            "governance trust root). Choose an --out-dir outside it."
        )
    return resolved


def guard_output_dir(path: str | Path, *, what: str) -> Path:
    """Refuse an output directory that is protected OR that contains a protected tree.

    The second half is why this is not just :func:`guard_write_path`. ``~`` is not a
    sensitive path, but it *contains* ``~/.ssh`` and the crew data home, and a
    command that creates directories and files under it is doing something no
    benchmark run needs to do.
    """
    from kiro_crew.security import is_sensitive_path, path_contains_sensitive

    resolved = _resolve(path)
    if is_sensitive_path(str(resolved)):
        raise UnsafePathError(
            f"refusing to use {resolved} as the {what}: it resolves into a "
            "protected location (a credential store or the governance trust root)."
        )
    if path_contains_sensitive(str(resolved)):
        raise UnsafePathError(
            f"refusing to use {resolved} as the {what}: a protected location lies "
            "under it, so writing a tree there could reach a credential store or "
            "the governance trust root. Choose a narrower directory."
        )
    return resolved


# ── Check-to-use: guarding a path by NAME is not enough ──────────────────────
#
# Guarding a directory does not guard the files derived from it. The corpus cache
# root is checked, but the download's ``.part`` staging file and the ``.sha256``
# sidecar are separate final components inside it, and a symlink planted at either
# name redirects the open to wherever it points. Reading the sidecar through such a
# link puts the target's bytes into the "expected checksum" mismatch message —
# printing a credential file to stdout — and writing through one truncates whatever
# it points at.
#
# Anything that can plant that link is anything running as this user, which by this
# harness's own threat model includes an agent. And resolving the name then opening
# the name leaves a window in which the final component can be swapped between the
# two, so the guard has to be paired with an open that refuses to follow a link
# rather than repeated more carefully.


def open_write_nofollow(path: str | Path, *, what: str) -> int:
    """Open *path* for writing, guarded, and refusing to follow a final symlink.

    ``O_NOFOLLOW`` fails with ``ELOOP`` when the final component is a link, which
    closes both the pre-planted link and the check-to-use swap: the guard decides
    what the name means, and the flag guarantees the descriptor is that file and not
    a redirect to another one.

    Returns a raw fd; wrap it with :func:`os.fdopen`. Mode is 0o600 because a corpus
    cache file has no reason to be group- or world-readable.

    Note which path is opened: the guard resolves in order to answer "does this name
    mean somewhere protected", but the ``open`` must use the path **as given**, not the
    resolved one. Opening the resolved path makes ``O_NOFOLLOW`` a no-op -- resolution
    has already followed the link, so the flag inspects the target's final component
    instead of the link, and the write lands on the redirect exactly as if there were
    no flag at all. ``O_NOFOLLOW`` only covers the final component; ancestor
    directories are covered by guarding the containing root.
    """
    import errno
    import os

    guard_write_path(path, what=what)
    as_given = Path(path).expanduser()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(as_given, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise UnsafePathError(
                f"refusing to write the {what}: {as_given.name!r} is a symbolic "
                "link. A link at that name redirects the write to whatever it "
                "points at, so it is refused rather than followed. Delete it and "
                "re-run."
            ) from exc
        raise


def read_text_nofollow(path: str | Path, *, what: str) -> str:
    """Read *path* as UTF-8, guarded, refusing to follow a final symlink.

    Routed through the repo's own ``hooks.safe_read_file``, which canonicalizes,
    re-checks the RESOLVED target against ``is_sensitive_path``, and opens with
    ``O_NOFOLLOW``. Its ``PermissionError`` is translated so callers here handle one
    exception type.
    """
    from kiro_crew.hooks import safe_read_file

    guard_read_path(path, what=what)
    try:
        return safe_read_file(str(path))
    except PermissionError as exc:
        raise UnsafePathError(f"refusing to read the {what}: {exc}") from exc
