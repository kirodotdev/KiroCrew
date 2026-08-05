#!/usr/bin/env python3
"""
_pathcheck.py -- shared path-safety helper for demo-recording reference scripts.

These scripts are agent-invoked utilities that accept file paths from
LLM-influenced CLI arguments. Direct open() would bypass kiro_crew path
controls, so every path is vetted through the CENTRALIZED sensitive-path
gate (kiro_crew.security.is_sensitive_path) -- never a local copy of the
denylist, which would drift (miss .docker, .npmrc, .netrc, trust roots...).

Fail-closed: these scripts record KiroCrew demos, so the worktree venv they
run in has kiro_crew installed. If the import fails, path safety cannot be
established and the script refuses to run.

Policy:
  - INPUT paths are refused when the centralized gate marks them sensitive.
  - OUTPUT paths must resolve inside the explicit workdir (default: CWD)
    and pass the same sensitive gate.
  - All paths are canonicalized via os.path.realpath before checks.
"""
import os
import stat as _stat
import sys


def _sensitive_gate(real_path, write=False):
    """Centralized sensitive-path verdict; fail CLOSED when unavailable.

    write=True uses the write-oriented gate (a SUPERSET of the read gate:
    it additionally covers write-protected runtime config files such as
    the gateway's own config.json / security policy, which stay readable
    but must never be modified by agent-invoked tooling).
    """
    try:
        from kiro_crew.security import is_sensitive_path, is_sensitive_write_path
    except ImportError:
        print(
            "FATAL: kiro_crew is not importable -- path safety cannot be "
            "established. Run these scripts from a provisioned worktree venv "
            "(.venv/bin/python) so the centralized sensitive-path gate is "
            "available.",
            file=sys.stderr,
        )
        sys.exit(78)  # EX_CONFIG
    if write:
        return is_sensitive_write_path(real_path)
    return is_sensitive_path(real_path)


def _is_inside(real_path, real_base):
    """Return True if real_path is equal to or a descendant of real_base."""
    base = real_base.rstrip(os.sep) + os.sep
    return real_path == real_base or real_path.startswith(base)


def safe_input_path(path):
    """Validate and return a canonicalized input path.

    Raises SystemExit with a clear message if the path is sensitive.
    NOTE: for actually READING the file, use read_json_input() or
    open_media_input() below -- returning a checked pathname for a later
    open() reintroduces the check-then-open race this module exists to close.
    """
    real = os.path.realpath(os.path.expanduser(path))
    if _sensitive_gate(real):
        print(
            f"FATAL: refusing to open sensitive path: {path}\n"
            f"  (resolved: {real})",
            file=sys.stderr,
        )
        sys.exit(78)
    return real


def read_json_input(path):
    """Read and parse a JSON input through the centralized hooks read gate.

    hooks.safe_read_file_bytes_nolink opens with O_NOFOLLOW and validates the
    OPENED descriptor's inode -- the file that is vetted is exactly the file
    that is read (no check-then-open window). Fail closed on any refusal.
    """
    import json

    real = safe_input_path(path)
    try:
        from kiro_crew import hooks
    except ImportError:
        print(
            "FATAL: kiro_crew is not importable -- input reads require the "
            "centralized hooks read gate. Run from a provisioned worktree "
            "venv (.venv/bin/python).",
            file=sys.stderr,
        )
        sys.exit(78)
    data = hooks.safe_read_file_bytes_nolink(real)
    if data is None:
        print(
            f"FATAL: hooks read gate refused input: {path}\n"
            f"  (resolved: {real})",
            file=sys.stderr,
        )
        sys.exit(78)
    return json.loads(data.decode("utf-8"))


def open_media_input(path, workdir=None):
    """Copy a binary media input into a PRIVATE temp file via the centralized
    descriptor-pinned gate and return a path libraries can consume.

    Delegates to kiro_crew.hooks.safe_copy_file_nolink: open(O_NOFOLLOW) ->
    fstat (regular, nlink==1) -> the OPENED descriptor's resolved real path
    must pass the centralized sensitive-path gate. O_NOFOLLOW only protects
    the FINAL path component; validating the fd's real path closes the
    ancestor-directory symlink-swap race (a pre-open path check alone can be
    invalidated by a concurrent directory replacement). Media libraries such
    as imageio spawn ffmpeg in a SUBPROCESS with an independent descriptor
    table, so a /proc/self/fd alias would not resolve there -- the private
    0600 copy in workdir is readable by any child process while a same-user
    swap of the original can never retarget what was already copied.

    Fails closed when kiro_crew is not importable. Returns
    (copy_path, cleanup) where cleanup() removes the temp copy.
    """
    try:
        from kiro_crew import hooks
    except ImportError:
        print(
            "FATAL: kiro_crew is not importable -- media input reads require "
            "the centralized descriptor-pinned copy gate. Run from a "
            "provisioned worktree venv (.venv/bin/python).",
            file=sys.stderr,
        )
        sys.exit(78)
    if workdir is None:
        workdir = os.getcwd()
    tmp_path = hooks.safe_copy_file_nolink(
        os.path.expanduser(path), dest_dir=workdir
    )
    if tmp_path is None:
        print(
            f"FATAL: media input refused by hooks copy gate: {path}",
            file=sys.stderr,
        )
        sys.exit(78)

    def cleanup():
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return tmp_path, cleanup


def safe_output_path(path, workdir=None):
    """Validate and return a canonicalized output path confined to workdir.

    workdir defaults to CWD if not specified. Raises SystemExit if the output
    would escape the workdir boundary.
    """
    if workdir is None:
        workdir = os.getcwd()
    real = os.path.realpath(os.path.expanduser(path))
    real_wd = os.path.realpath(os.path.expanduser(workdir))
    if not _is_inside(real, real_wd):
        print(
            f"FATAL: output path escapes workdir boundary.\n"
            f"  path:    {path}\n"
            f"  resolved: {real}\n"
            f"  workdir:  {real_wd}",
            file=sys.stderr,
        )
        sys.exit(78)
    if _sensitive_gate(real, write=True):
        print(
            f"FATAL: refusing to write to sensitive path: {path}\n"
            f"  (resolved: {real})",
            file=sys.stderr,
        )
        sys.exit(78)
    return real


def safe_open_output(path, workdir=None, mode="w"):
    """Validate an output path and open it without following any symlink.

    Closes the check-then-open TOCTOU gap in safe_output_path(): after
    pathname validation, the file is opened by walking each component
    relative to a held descriptor of the approved workdir (dir_fd) with
    O_NOFOLLOW, so neither the final component nor any ancestor directory
    can be swapped for a symlink between validation and open.

    Create-only (O_EXCL): the destination must not already exist, so this
    helper can never truncate or overwrite pre-existing content — callers
    always emit fresh recording artifacts under new names.
    Returns an open file object.
    """
    if workdir is None:
        workdir = os.getcwd()
    real = safe_output_path(path, workdir=workdir)
    real_wd = os.path.realpath(os.path.expanduser(workdir))
    rel = os.path.relpath(real, real_wd)
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    def _fail(detail):
        print(
            f"FATAL: refusing to open output (symlink or unwritable): {path}\n"
            f"  (resolved: {real}, {detail})",
            file=sys.stderr,
        )
        sys.exit(78)

    if not parts or ".." in parts:
        _fail("path resolves to workdir itself or escapes it")
    fd = -1
    dfd = -1
    try:
        dfd = os.open(real_wd, os.O_RDONLY | os.O_DIRECTORY | nofollow)
        st_dfd = os.fstat(dfd)
        st_path = os.lstat(real_wd)
        if (st_dfd.st_dev, st_dfd.st_ino) != (st_path.st_dev, st_path.st_ino):
            _fail("workdir was replaced after validation")
        for comp in parts[:-1]:
            nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        fd = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o644,
            dir_fd=dfd,
        )
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            os.close(fd)
            _fail("destination is not a regular file")
    except OSError as e:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _fail(f"error: {e}")
    finally:
        if dfd >= 0:
            try:
                os.close(dfd)
            except OSError:
                pass
    if "b" in mode:
        return os.fdopen(fd, "wb")
    return os.fdopen(fd, "w")
