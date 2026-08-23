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

from kiro_crew import pinned_fs

from .errors import BenchRefusal


class UnsafePathError(BenchRefusal):
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


def _supports_pinned_walk() -> bool:
    """Whether this platform can open relative to a directory descriptor.

    Delegates to :func:`kiro_crew.pinned_fs.supports_pinned_walk`. Kept as a
    module-level function rather than an alias because the Windows-simulation tests
    replace this attribute to exercise the fallback branch.
    """
    return pinned_fs.supports_pinned_walk()


def _open_in_pinned_parent(
    resolved_parent: str, name: str, *, flags: int, mode: int, what: str
) -> int:
    """Open *name* under *resolved_parent* with the parent chain pinned.

    Thin wrapper over :func:`kiro_crew.pinned_fs.open_in_pinned_parent` that keeps
    this harness's refusal type. See that module for what pinning buys and why the
    name is opened as given.
    """
    return pinned_fs.open_in_pinned_parent(
        resolved_parent,
        name,
        flags=flags,
        mode=mode,
        what=what,
        refusal=UnsafePathError,
    )


def _pin_parent(resolved_parent: str, *, what: str) -> int:
    """Return a descriptor for *resolved_parent*, refusing a component that is now a link.

    Thin wrapper over :func:`kiro_crew.pinned_fs.pin_parent`, which owns the
    invariants: one ``openat`` per component with ``O_NOFOLLOW``, *resolved_parent*
    resolved by the caller exactly once, and the descriptor handed back OPEN so a
    create-then-rename publishes through the same pinned directory.
    """
    return pinned_fs.pin_parent(resolved_parent, what=what, refusal=UnsafePathError)


def _refuse_hardlink_alias(fd: int, *, what: str, name: str) -> None:
    """Reject a descriptor that is one of several names for the same inode.

    Thin wrapper over :func:`kiro_crew.pinned_fs.refuse_hardlink_alias`, which owns
    the reasoning: a hardlink is invisible to every path-based guard, so it is judged
    on the DESCRIPTOR, which is what makes the check race-free. The descriptor is
    closed before the refusal is raised, so a caller's ``except BaseException:
    os.close(fd)`` must not run for it.
    """
    pinned_fs.refuse_hardlink_alias(fd, what=what, name=name, refusal=UnsafePathError)


def open_write_nofollow(path: str | Path, *, what: str) -> int:
    """Create *path* for writing, guarded, refusing to follow or reuse an existing name.

    Creates a NEW file and nothing else. ``O_EXCL`` is the load-bearing flag: creation
    is atomic, so a name that already holds a file, a symlink, a hardlink alias or a
    directory fails with ``EEXIST`` and is refused rather than written through. That is
    what makes this safe where ``O_NOFOLLOW`` does not exist — see below — and it is
    also why nothing here truncates: there is never anything to truncate.

    A caller that needs to REPLACE an existing file wants
    :func:`write_text_atomic_nofollow`, which publishes by rename. This one is for
    the temporary files a rename publishes from, and for artifacts written once.

    ``O_NOFOLLOW`` (where it exists) fails with ``ELOOP`` when the final component is
    a link, which makes the refusal specific rather than a bare ``EEXIST``.

    Returns a raw fd; wrap it with :func:`os.fdopen`. Mode is 0o600 because a corpus
    cache file has no reason to be group- or world-readable.

    Note which path is opened: the guard resolves in order to answer "does this name
    mean somewhere protected", but the ``open`` must use the path **as given**, not the
    resolved one. Opening the resolved path makes ``O_NOFOLLOW`` a no-op -- resolution
    has already followed the link, so the flag inspects the target's final component
    instead of the link, and the write lands on the redirect exactly as if there were
    no flag at all. ``O_NOFOLLOW`` only covers the final component; ancestor
    directories are covered by guarding the containing root.

    **Windows has no ``O_NOFOLLOW``.** ``getattr(os, "O_NOFOLLOW", 0)`` returns 0
    there, so the flag contributes nothing and ``O_EXCL`` carries the protection
    instead. An earlier version relied on an ``is_symlink()`` pre-check and then
    opened the name for writing, which refused a link that was already there and
    followed one planted between the check and the open -- a window a reviewer was
    right to call a defect rather than a documented limit. Exclusive creation has no
    such window on any platform, because there is no moment at which an existing name
    is opened at all. The ``is_symlink()`` check is kept only so the refusal says
    "this is a symbolic link" instead of "something exists here".

    A ctypes ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` is no longer worth
    considering here: it would buy the same property exclusive creation already has,
    at the price of security code that cannot be exercised on the machine this
    harness is developed on.
    """
    import errno
    import os

    # The guard answers "does this name mean somewhere protected" and its return value
    # is deliberately NOT used as the open path: it has the final symlink already
    # followed, so opening it would undo the final-component protection. The pinned
    # walk below resolves the PARENT chain and opens the final name as given.
    guard_write_path(path, what=what)
    as_given = Path(path).expanduser()
    _refuse_link_at_name(as_given, what=what)
    # O_EXCL is what makes this safe on a platform without O_NOFOLLOW. Creation is
    # atomic: either this call makes a brand-new file at that name or it fails, so
    # there is no window in which a link swapped in after the check above gets
    # followed, and nothing that already exists is ever truncated. The previous
    # version opened O_CREAT and truncated after inspecting the descriptor, which
    # covered the link that was already there and not the one planted a microsecond
    # later.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        if _supports_pinned_walk():
            # Parent pinned by a descriptor, final name opened relative to it. After
            # the parent is open, swapping its name for a symlink cannot redirect
            # this write. The helper re-validates where the parent resolves to now,
            # because the guard above judged an older resolution.
            dir_fd = _pin_parent_for(as_given, what=what)
            try:
                fd = os.open(as_given.name, flags, 0o600, dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
        else:
            # No dir_fd here, so the parent cannot be pinned to a descriptor. The
            # re-validation below is the platform's stand-in; see its docstring for
            # what it does and does not buy.
            _revalidate_unpinned(as_given, what=what)
            fd = os.open(as_given, flags, 0o600)
        # Vacuous on a file this call just created, and kept because it is the only
        # check that would notice if that ever stopped being true.
        _refuse_hardlink_alias(fd, what=what, name=as_given.name)
        return fd
    except OSError as exc:
        refusal = _link_refusal(exc, what=what, name=as_given.name)
        if refusal is not None:
            raise refusal from exc
        if exc.errno == errno.EEXIST:
            raise _existing_name_refusal(as_given, what=what) from exc
        raise


def _existing_name_refusal(as_given: Path, *, what: str) -> UnsafePathError:
    """Say WHY exclusive creation refused, not merely that something was there.

    ``O_CREAT|O_EXCL`` reports ``EEXIST`` for a symlink too -- it never follows it, so
    ``ELOOP`` does not arrive -- and "something already exists" would be a worse
    message than the two this used to give. The name is inspected only to phrase the
    refusal; the write has already been refused by then, so nothing here can be raced
    into permitting anything.
    """
    import os

    if as_given.is_symlink():
        return UnsafePathError(
            f"refusing to write the {what}: {as_given.name!r} is a symbolic link. "
            "A link at that name redirects the write to whatever it points at, so "
            "it is refused rather than followed. Delete it and re-run."
        )
    try:
        links = os.stat(as_given, follow_symlinks=False).st_nlink
    except OSError:  # pragma: no cover - the name vanished between the two calls
        links = 1
    if links > 1:
        return UnsafePathError(
            f"refusing to write the {what}: {as_given.name!r} has {links} hard "
            "links, so it is another name for a file this command was not pointed "
            "at. A path guard cannot see that -- the alias shares the target's "
            "inode -- so the name is refused. Remove the extra link or use a "
            "different path."
        )
    return UnsafePathError(
        f"refusing to write the {what}: something already exists at "
        f"{as_given.name!r}. This writer only ever creates a new file, because "
        "opening an existing name is what lets a link planted after the check "
        "redirect the write on a platform without O_NOFOLLOW. A caller that means "
        "to replace a file should publish it by rename instead. Delete it and re-run."
    )


def _refuse_link_at_name(as_given: Path, *, what: str) -> None:
    """Windows stand-in for the missing ``O_NOFOLLOW``: refuse a link already there.

    Does nothing where the flag exists, because there the ``open`` itself refuses and
    has no check-to-use window. See ``open_write_nofollow``'s docstring for the window
    this does NOT close.
    """
    import os

    if hasattr(os, "O_NOFOLLOW") or not as_given.is_symlink():
        return
    raise UnsafePathError(
        f"refusing to write the {what}: {as_given.name!r} is a symbolic link. "
        "A link at that name redirects the write to whatever it points at, so "
        "it is refused rather than followed. Delete it and re-run.\n"
        "(Detected by an explicit check: this platform has no O_NOFOLLOW, so a "
        "link swapped in after the check would still be followed.)"
    )


def _link_refusal(exc: OSError, *, what: str, name: str) -> UnsafePathError | None:
    """Translate the errno ``O_NOFOLLOW`` raises into the refusal, or ``None``.

    Returns rather than raises so the caller re-raises the original ``OSError`` for
    every other errno: a full disk and a planted link must not read the same.
    """
    import errno

    if exc.errno not in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
        return None
    return UnsafePathError(
        f"refusing to write the {what}: {name!r} is a symbolic "
        "link. A link at that name redirects the write to whatever it "
        "points at, so it is refused rather than followed. Delete it and "
        "re-run."
    )


def write_text_atomic_nofollow(path: str | Path, text: str, *, what: str) -> None:
    """Replace *path* with *text* so a failed write cannot destroy what was there.

    ``open_write_nofollow`` truncates the destination and hands back a descriptor,
    which is right for a staging file and wrong for a durable artifact. A report
    written with a reused ``--stem`` is destroyed the instant that truncation lands,
    and an interruption or ENOSPC part-way through the write leaves an empty or
    half-written file where a valid baseline used to be -- and the baseline is the
    only thing a benchmark report is for.

    Here the bytes go to a sibling temporary in the SAME directory, are flushed and
    ``fsync``-ed, and only then replace the destination. A reader sees either the old
    file or the new one, never a partial one. The sibling matters: a rename is only
    atomic within a filesystem, so a temporary under ``/tmp`` would silently become a
    copy across devices.

    The destination's own refusals still run FIRST and are unchanged. A symlink or a
    hardlink alias at that name is refused rather than replaced -- ``os.replace``
    would unlink the link and drop a file in its place, which writes nothing through
    the link but does quietly discard a name the caller never asked to touch.
    """
    import os

    guard_write_path(path, what=what)
    as_given = Path(path).expanduser()
    _refuse_link_at_name(as_given, what=what)
    data = text.encode("utf-8")
    # PID in the name so two concurrent runs against one --out-dir cannot adopt each
    # other's partial file; O_EXCL below turns any collision into an error rather
    # than a silent overwrite.
    tmp_name = f".{as_given.name}.{os.getpid()}.tmp"
    tmp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        if _supports_pinned_walk():
            dir_fd = _pin_parent_for(as_given, what=what)
            try:
                _refuse_alias_at(dir_fd, as_given.name, what=what)
                fd = os.open(tmp_name, tmp_flags, 0o600, dir_fd=dir_fd)
                try:
                    _write_and_sync(fd, data)
                except BaseException:
                    os.unlink(tmp_name, dir_fd=dir_fd)
                    raise
                # Both ends relative to the pinned directory, so the destination
                # cannot be redirected between the check above and this swap.
                os.replace(tmp_name, as_given.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                _sync_dir(dir_fd)
            finally:
                os.close(dir_fd)
        else:
            # Windows: no dir_fd support in the stdlib, so the destination is checked
            # and the temporary is created by path. The publish is still a rename, so
            # the residual swap window cannot redirect a write or truncate a target --
            # the worst an attacker gets is their own planted name replaced.
            tmp = as_given.parent / tmp_name
            _revalidate_unpinned(as_given, what=what)
            _refuse_alias_at_path(as_given, what=what)
            fd = os.open(tmp, tmp_flags, 0o600)
            try:
                _write_and_sync(fd, data)
                os.replace(tmp, as_given)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
    except OSError as exc:
        refusal = _link_refusal(exc, what=what, name=as_given.name)
        if refusal is not None:
            raise refusal from exc
        raise


def _refuse_alias_at(dir_fd: int, name: str, *, what: str) -> None:
    """Refuse *name* under *dir_fd* if it is a link or a hardlink alias.

    Opened WITHOUT ``O_CREAT``: this only inspects what is already there, so a first
    run leaves no empty file behind when the write that follows fails. A missing
    destination is the normal case and is not a refusal. ``O_WRONLY`` is deliberate --
    it makes a directory at that name fail with ``EISDIR`` exactly as the in-place
    writer does, instead of opening it and reporting its link count as an alias.
    """
    import errno
    import os

    try:
        fd = os.open(name, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise
    try:
        _refuse_hardlink_alias(fd, what=what, name=name)
    except UnsafePathError:
        raise  # the helper closed the descriptor before raising
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)


def _revalidate_unpinned(as_given: Path, *, what: str) -> None:
    """The no-``dir_fd`` platform's stand-in for :func:`_pin_parent_for`.

    Round 16 put the re-validation in the pinned branch and left the fallback reading
    the verdict of a resolution taken earlier -- the same rule, applied at one of two
    sites, which is the defect class this module keeps having to close. Both branches
    now re-check.

    Two things happen here, and only the first is available on the pinned path:

    1. the composed destination is re-validated against the CURRENT resolution, so a
       retargeted ancestor cannot inherit a verdict given before it moved;
    2. any reparse point (symlink, or a Windows junction, which ``islink`` does not
       report) in the ancestor chain is refused outright.

    (2) exists because a descriptor cannot be held here. A link in the chain is the
    component an attacker retargets, so where the write cannot be pinned to an inode,
    the presence of that component is refused instead. The cost is honest: an
    ``--out-dir`` reached through a junction is rejected on Windows and the message
    says so. The alternative -- refusing every write on the platform, which is what a
    literal "fail closed when the parent cannot be pinned" would mean -- withdraws the
    command from Windows entirely, and that is a bigger claim than the threat needs.

    What remains: an attacker who creates a reparse point in the chain in the window
    between this scan and the write still wins. That window is microseconds and needs
    the attacker to already be able to replace a directory component of the caller's
    own output path; before this, retargeting a junction at ANY point during the run
    was enough.
    """
    import os

    resolved_parent = os.path.realpath(as_given.parent or Path("."))
    guard_write_path(Path(resolved_parent) / as_given.name, what=what)
    probe = Path(os.path.abspath(as_given.parent or Path(".")))
    for prefix in (probe, *probe.parents):
        if _is_reparse_point(prefix):
            raise UnsafePathError(
                f"refusing to write the {what}: {str(prefix)!r} on the way to it is a "
                "link or junction, and this platform cannot pin a directory by "
                "descriptor. Re-pointing that component would redirect the write "
                "after it was checked, so the path is refused rather than followed. "
                "Choose an --out-dir whose ancestors are real directories."
            )


def _is_reparse_point(path: Path) -> bool:
    """True for a symlink or a Windows junction.

    Thin wrapper over :func:`kiro_crew.pinned_fs.is_reparse_point`, which explains
    why the reparse tag is checked as well as ``islink`` and why comparing
    ``realpath`` against ``abspath`` would be wrong on Windows.
    """
    return pinned_fs.is_reparse_point(path)


def _pin_parent_for(as_given: Path, *, what: str) -> int:
    """Resolve the parent, re-check where it NOW points, then pin that.

    ``guard_write_path`` decided the name was acceptable using the resolution at the
    moment it ran. The pinned walk then uses a parent resolved later, so an ancestor
    swapped in between moved the write into whatever the newer resolution names --
    including the governance trust root, which is the one place the guard exists to
    keep a benchmark out of. Checking the composed destination against the resolution
    the walk is ABOUT to use closes that: the string validated here is the same string
    pinned below, and nothing re-resolves after the check.

    Resolved ONCE and passed down. Re-resolving inside the walk would re-follow
    whatever an ancestor points at by then, which is the mistake that made an earlier
    version of this defensible-looking and useless.
    """
    import os

    resolved_parent = os.path.realpath(as_given.parent or Path("."))
    guard_write_path(Path(resolved_parent) / as_given.name, what=what)
    return _pin_parent(resolved_parent, what=what)


def _refuse_alias_at_path(as_given: Path, *, what: str) -> None:
    """Path-based sibling of :func:`_refuse_alias_at`, for the no-dir_fd platform.

    Exists because the atomic writer's fallback branch skipped the hardlink check
    entirely, so on Windows a planted alias at the report name was replaced silently
    while the same write was refused everywhere else. A platform without ``dir_fd``
    still has ``fstat``, so the check that matters is still available -- only the
    race-freedom of addressing the parent by descriptor is not.
    """
    import errno
    import os

    try:
        fd = os.open(as_given, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise
    try:
        _refuse_hardlink_alias(fd, what=what, name=as_given.name)
    except UnsafePathError:
        raise  # the helper closed the descriptor before raising
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)


def _write_and_sync(fd: int, data: bytes) -> None:
    """Write *data* to *fd* and force it to the device, closing *fd* either way.

    ``fsync`` is the difference between "the rename published the new bytes" and "the
    rename published a name whose contents were still in the page cache when the
    machine went down".
    """
    import os

    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _sync_dir(dir_fd: int) -> None:
    """Persist the rename itself. Best effort: some filesystems refuse this."""
    import os

    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - filesystem-dependent
        pass


def read_text_nofollow(path: str | Path, *, what: str) -> str:
    """Read *path* as UTF-8, guarded, refusing to follow a final symlink.

    Does what ``hooks.safe_read_file`` does -- canonicalize, re-check the RESOLVED
    target against ``is_sensitive_path``, open the canonical path with ``O_NOFOLLOW``
    -- and adds a hardlink rejection on the open descriptor.

    Inlined rather than delegated for exactly that last part: a hardlink alias is only
    recognisable from the fd (``st_nlink``), and a helper that returns text has
    already read the bytes by the time it could be judged. ``hooks.safe_read_file``
    is used repo-wide, so the check lives here instead of widening its contract.
    """
    import os

    from kiro_crew.security import is_sensitive_path

    guard_read_path(path, what=what)
    # Mirrors `safe_read_file` -- resolve, re-check the RESOLVED target, open the
    # canonical path with O_NOFOLLOW -- and adds the hardlink rejection, which has to
    # happen on the descriptor and therefore cannot be delegated to a helper that
    # returns text. Opening the resolved path (not the path as given) is deliberate:
    # a link to an ordinary file stays readable, which is the documented read/write
    # asymmetry.
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
        if is_sensitive_path(resolved):
            raise UnsafePathError(
                f"refusing to read the {what}: {resolved} is a protected location."
            )
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except UnsafePathError:
        raise
    except OSError as exc:
        raise UnsafePathError(f"refusing to read the {what}: {exc}") from exc
    _refuse_hardlink_alias(fd, what=what, name=os.path.basename(resolved))
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()
