"""``--squash``: one commit of HEAD's tree on the base, through the message hooks.

It finds the default message file and the branch being squashed, decides whether the
repository's hooks can run, applies the commit message cleanup and the
``prepare-commit-msg``/``commit-msg`` hooks, writes the commit with ``commit-tree`` and
moves the branch onto it in one compare-and-swap. New work on how the squash commit is
made goes here.

Loaded by ``push_guard.py``, which rebinds every function defined here onto its own
globals, so the code runs as if it were written there and a name a test replaces on
``push_guard.py`` reaches it. This module therefore defines functions only: no module
state, no class, and nothing its functions read at import time.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from push_guard import (
        _HOOK_RUN_GIT,
        _MESSAGE_PREFIX,
        Refused,
        _ask,
        _changed,
        _commit_info,
        _git_paths,
        _head_ref,
        _interactive,
        _move_ref,
        _outcome,
        _refuse_unrecorded,
        _sha,
        _unlink,
        _write_git_file,
        err,
    )
    from push_guard_record import _unrecorded, record_built


def squash_message_path(git_dir, branch):
    """Where --squash reads the message for ``branch`` (``/`` becomes ``-``)."""
    return os.path.join(git_dir, _MESSAGE_PREFIX + branch.replace("/", "-") + ".txt")


def _branch_ref():
    """refs/heads/<b> that HEAD points at (refuses a detached HEAD)."""
    ref = _head_ref()
    if not ref.startswith("refs/heads/"):
        err("REFUSED: HEAD is detached; check out your feature branch first.")
        raise Refused()
    return ref


def _git_version():
    out = _ask(["git", "version"])[1]
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _hooks_runnable():
    """True when this git runs hooks by name; refuses when it cannot but hooks exist."""
    version = _git_version()
    if version >= _HOOK_RUN_GIT:
        return True
    # --git-path resolves through core.hooksPath (relative to this directory),
    # so it names the hook git would run, wherever the hooks live.
    present = [
        name
        for name in ("prepare-commit-msg", "commit-msg")
        if os.path.exists(_git_paths("hooks/" + name)[0])
    ]
    if present:
        err(
            "REFUSED: git {}.{} cannot run the repository's commit message hooks for a "
            "squash (git hook run needs git {}.{} or later). Upgrade git and re-run.".format(
                *version, *_HOOK_RUN_GIT
            )
        )
        raise Refused()
    return False


def _stripspace(data, comments):
    args = ["git", "stripspace"] + (["--strip-comments"] if comments else [])
    return _ask(args, raw=True, input=data)[1]


def _cleanup_mode():
    rc, mode = _ask(["git", "config", "--get", "commit.cleanup"], answers=(0, 1))
    mode = mode if rc == 0 and mode else "default"
    if mode not in ("default", "strip", "whitespace", "verbatim", "scissors"):
        err("REFUSED: commit.cleanup is {!r}, which git commit rejects too.".format(mode))
        raise Refused()
    return mode


def _squash_message_file(git_dir):
    """The guard's own copy of the squash message while it is hooked and committed."""
    return os.path.join(git_dir, "push_guard-squash-{}.msg".format(os.getpid()))


def _hook_message(git_dir, message, tree):
    """``message`` (bytes) through the message hooks and cleaned as ``git commit -F`` does.

    prepare-commit-msg (source ``message``) and commit-msg run on it with an
    index of the squash's tree and GIT_EDITOR=":", as for any commit; then
    commit.cleanup applies (comments are stripped only when it is ``strip``).
    Returns the final bytes, or raises Refused (a hook rejected it, or it is
    empty).
    """
    mode = _cleanup_mode()
    text = message if mode == "verbatim" else _stripspace(message, comments=False)
    if not text.strip():
        err("REFUSED: the squash message is empty.")
        raise Refused()
    path = _squash_message_file(git_dir)
    index = os.path.join(git_dir, "push_guard-squash-{}.index".format(os.getpid()))
    _write_git_file(path, text)
    try:
        if _hooks_runnable():
            env = {"GIT_INDEX_FILE": index}
            _ask(["git", "read-tree", tree], env=env)
            env["GIT_EDITOR"] = ":"
            for hook, hook_args in (
                ("prepare-commit-msg", [path, "message"]),
                ("commit-msg", [path]),
            ):
                res = _interactive(
                    ["git", "hook", "run", "--ignore-missing", hook, "--", *hook_args], env
                )
                if res.timed_out or res.rc != 0:
                    err(
                        "REFUSED: the {} hook rejected the squash message ({}); its "
                        "output is above. Nothing was moved.".format(hook, _outcome(res))
                    )
                    raise Refused()
        with open(path, "rb") as handle:
            hooked = handle.read()
    except OSError as exc:
        err("REFUSED: cannot read the hooked squash message: {}".format(exc))
        raise Refused()
    finally:
        _unlink(path)
        _unlink(index)
    final = hooked if mode == "verbatim" else _stripspace(hooked, comments=mode == "strip")
    if not final.strip():
        err("REFUSED: the squash message is empty after its hooks and commit.cleanup.")
        raise Refused()
    return final


def _squash(base, ref, pre, pinned, ahead, git_dir, message, vouched):
    """Squash the branch to one commit of ``pre``'s tree on ``pinned``; return its sha.

    ``pre``, ``pinned`` and ``ahead`` (the commits between them) are what the
    history checks vetted.  Every path the squash changes must be accounted
    for (_unrecorded) or named (``vouched``).  The commit object is made by
    ``commit-tree`` from ``pre``'s tree (never an index), signed when
    commit.gpgSign asks, with the message through the message hooks.  It is
    recorded first; then the branch moves exactly once, by compare-and-swap
    from ``pre`` to the new commit, so an interrupt anywhere leaves it at one
    or the other.
    Why: references/rationale.md.
    """
    if pre == pinned:
        err("REFUSED: HEAD is origin/{} itself — there is nothing to squash.".format(base))
        raise Refused()
    tree = _commit_info(pre)[0]
    if tree == _commit_info(pinned)[0]:
        err(
            "REFUSED: the branch changes nothing against origin/{}; nothing to squash.".format(base)
        )
        raise Refused()
    changed = _changed(pinned, tree)
    unknown = _unrecorded(git_dir, ref, changed, ahead, pinned, pre, vouched)
    if unknown:
        _refuse_unrecorded(unknown, "the branch", git_dir, base, "squash")
    if _sha(ref) != pre:
        err("REFUSED: {} moved while the guard ran; re-run.".format(ref))
        raise Refused()

    text = _hook_message(git_dir, message, tree)
    rc, sign = _ask(["git", "config", "--bool", "commit.gpgSign"], answers=(0, 1))
    argv = ["git", "commit-tree", tree, "-p", pinned]
    if rc == 0 and sign == "true":
        argv.append("-S")
    path = _squash_message_file(git_dir)
    _write_git_file(path, text)
    try:
        res = _interactive(argv + ["-F", path])
    finally:
        _unlink(path)
    squashed = res.out.decode("ascii", "replace").strip()
    if res.timed_out or res.rc != 0 or not squashed:
        err(
            "REFUSED: git commit-tree failed ({}); its output is above. Nothing was moved.".format(
                _outcome(res)
            )
        )
        raise Refused()

    record_built(git_dir, squashed, changed)
    now = _move_ref(ref, squashed, pre, "push_guard: squash onto origin/" + base)
    if now != squashed:
        err(
            "REFUSED: {} could not be moved from {} to the squash (it is now {}); "
            "it was left alone, and the squash commit {} is unreferenced.".format(
                ref, pre[:12], now[:12], squashed[:12]
            )
        )
        raise Refused()
    rc, still = _ask(["git", "symbolic-ref", "--quiet", "HEAD"], answers=(0, 1))
    if rc != 0 or still != ref:
        err(
            "WARNING: HEAD moved to {} during the squash; your branch {} was "
            "updated — check it out.".format(still or "a detached commit", ref)
        )
    print("before squash:   " + pre[:12])
    print("HEAD:            " + squashed[:12])
    return squashed


def _read_message(path):
    """The message bytes (a UTF-8 BOM dropped), or raises Refused naming the file and the fix."""
    remedy = "Write the message (subject line first) to {} and re-run.".format(path)
    try:
        with open(path, "rb") as handle:
            message = handle.read()
    except OSError as exc:
        err("REFUSED: cannot read the squash message ({}). {}".format(exc, remedy))
        raise Refused()
    if message.startswith(b"\xef\xbb\xbf"):
        message = message[3:]
    if not message.strip():
        err("REFUSED: the squash message {} is empty. {}".format(path, remedy))
        raise Refused()
    return message
