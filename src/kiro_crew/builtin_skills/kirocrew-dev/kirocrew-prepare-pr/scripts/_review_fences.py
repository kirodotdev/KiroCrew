"""Markdown fence readers shared by every kirocrew-prepare-pr script.

``_review_contract.py`` re-exports every name here, and callers import them from
there. ``_review_rounds.py`` loads this module, and the contract takes it from that
load, so one process reads fences through one copy of these functions.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re


# --- Markdown fences: the one reader for every kirocrew-prepare-pr script -------
#
# A fenced block opens with three or more backticks or tildes (up to three spaces
# of indent; a backtick fence's info string cannot hold a backtick, that is inline
# code) and closes only with the same character, at least as many, up to three
# spaces of indent and nothing but blanks after. A ``` line inside a ~~~ block is
# content. Those two rules live in pr_status_issue_links.py, which pr_status.py
# already loads; this module loads that file the same way and builds the line
# readers below on them, so pr_findings.py, diff_signals.py and sweep_check.py read
# fences through the one pair of functions.
class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_fence_owner():
    """pr_status_issue_links.py, loaded by path beside this file with no cache written."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr_status_issue_links.py")
    name = "_prepare_pr_fence_owner"
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import kirocrew-prepare-pr sibling script: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_fence_owner = _load_fence_owner()
fence_opening = _fence_owner._fence_opening
is_closing_fence = _fence_owner._is_closing_fence
_LINE_END_RE = re.compile(r"\r\n|\n|\r")
_QUOTE_PREFIX_RE = re.compile(r"^(?:\s*>)+")


def split_lines(text):
    """Lines of `text`, ended by CR, LF or CRLF only (not the other separators str.splitlines knows)."""
    return _LINE_END_RE.split(text)


def unfenced_lines(text, quoted=False):
    """The lines of `text` outside fenced blocks (fence lines excluded).

    With `quoted`, fence markers are read through leading `>` markers, so a fence
    inside a blockquote fences its content too. A block that never closes hides the
    rest of the text, as Markdown does.
    """
    fence = None
    for raw in split_lines(text):
        line = _QUOTE_PREFIX_RE.sub("", raw) if quoted else raw
        if fence is None:
            fence = fence_opening(line)
            if fence is None:
                yield raw
        elif is_closing_fence(line, fence[0], fence[1]):
            fence = None


def drop_fenced_blocks(text):
    """`text` without its fenced blocks, fence lines included.

    A block that never closes is kept as prose, so an open fence cannot hide the
    rest of a section from a word limit.
    """
    kept: list = []
    held: list = []
    fence = None
    for line in split_lines(text):
        if fence is None:
            fence = fence_opening(line)
            if fence is None:
                kept.append(line)
            else:
                held = [line]
            continue
        held.append(line)
        if is_closing_fence(line, fence[0], fence[1]):
            fence = None
            held = []
    return "\n".join(kept + held)
