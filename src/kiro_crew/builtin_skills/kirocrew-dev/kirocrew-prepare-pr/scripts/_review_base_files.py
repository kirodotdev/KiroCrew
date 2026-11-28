"""Base-side file reads of one review, through one ``git cat-file --batch`` process.

``local_review.py`` reads each reviewer's contract and the rule and prompt files it
names out of the base commit. One ``git show <base>:<path>`` a file is one process
start a file, and a start is what a Windows user waits for. ``BaseFiles.show``
answers with exactly what ``run(["git", "show", "<base>:<path>"], cwd=worktree)``
returns, and starts git once for all the files of a review.

Only a blob that was found is answered from the batch process. Everything else is
handed to ``git show`` itself, so its exit status and its words stay git's own: a
path that is not in the base commit, a tree, a name ``git show`` refuses as
ambiguous (a glob character, or a file of that name in the work tree), a name with
a line break (the batch input is one name a line; NUL-terminated input needs git
2.38), and every read after the batch process gave an answer this reader does not
know. ``git cat-file --batch`` with ``<rev>:<path>`` names is in git since 1.5.6.

A name that is not an object is answered in one line, and the process stays in step:
``<name> missing``, ``<name> ambiguous``, and from a newer git ``<id> submodule`` for
a submodule entry whose commit is not in the repository (an older git says
``missing``).
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable, Optional

_Run = Callable[..., "tuple[int, str, str]"]
_Resolve = Callable[["list[str]"], "tuple[list[str], Optional[tuple[int, str, str]]]"]

# The header of an object that was found: its name, its type and its size in bytes.
_FOUND = re.compile(rb"^[0-9a-f]{40,64} ([a-z]+) ([0-9]+)\n$")
# The one line git prints, after its id, for a submodule entry it cannot read.
_SUBMODULE = re.compile(rb"^[0-9a-f]{40,64} submodule\n$")
# ``git show`` reads an argument with one of these as a pathspec and refuses it as ambiguous.
_GLOB_SPECIAL = frozenset("*?[\\")
_LINE_BREAKS = frozenset("\n\r")


def _as_text(body: bytes) -> str:
    """``body`` as ``run`` hands back a command's output: UTF-8 with replacement, text mode."""
    return body.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


class BaseFiles:
    """The base-side reads of one review. Close it when the review is assembled."""

    def __init__(self, run: _Run, resolve: _Resolve, worktree: str) -> None:
        self._run = run
        self._resolve = resolve
        self._worktree = worktree
        self._process: Optional["subprocess.Popen[bytes]"] = None
        self._usable = True

    def __enter__(self) -> "BaseFiles":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def show(self, base_sha: str, relpath: str) -> "tuple[int, str, str]":
        """What ``git show <base_sha>:<relpath>`` run in the work tree returns."""
        name = "{}:{}".format(base_sha, relpath)
        body = self._blob(name)
        if body is None:
            return self._run(["git", "show", name], cwd=self._worktree)
        return 0, _as_text(body), ""

    def close(self) -> None:
        """End the batch process, if one was started. Later reads start git one by one."""
        process, self._process, self._usable = self._process, None, False
        if process is None:
            return
        for pipe in (process.stdin, process.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        process.wait()

    def _blob(self, name: str) -> Optional[bytes]:
        """The bytes of the blob ``name``, or None when ``git show`` has to answer."""
        if (
            not self._usable
            or not _GLOB_SPECIAL.isdisjoint(name)
            or not _LINE_BREAKS.isdisjoint(name)
        ):
            return None
        if os.path.lexists(os.path.join(self._worktree, name)):
            return None
        process = self._started()
        if process is None or process.stdin is None or process.stdout is None:
            return None
        try:
            asked = os.fsencode(name)
            process.stdin.write(asked + b"\n")
            process.stdin.flush()
            header = process.stdout.readline()
            found = _FOUND.match(header)
            if found is None:
                not_an_object = (
                    header in (asked + b" missing\n", asked + b" ambiguous\n")
                    or _SUBMODULE.match(header) is not None
                )
                if not not_an_object:
                    self.close()
                return None
            size = int(found.group(2))
            body = process.stdout.read(size)
            # Each object is followed by one line feed.
            end = process.stdout.read(1)
        except (OSError, ValueError):
            self.close()
            return None
        if len(body) != size or end != b"\n":
            self.close()
            return None
        return body if found.group(1) == b"blob" else None

    def _started(self) -> Optional["subprocess.Popen[bytes]"]:
        if self._process is None:
            argv, refused = self._resolve(["git", "cat-file", "--batch"])
            if refused:
                self._usable = False
                return None
            try:
                self._process = subprocess.Popen(
                    argv,
                    cwd=self._worktree,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                self._usable = False
                return None
        return self._process
