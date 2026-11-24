"""Shell text reader: the empty-quote-pair collapse the ssh-to-self descent tokenizes with.

Part of :mod:`kiro_crew.security.shell_normalizer`, which re-exports every name here and
calls them from ``_shell_tokens``; it imports nothing from that module.
"""

from __future__ import annotations

import re

#: Bare empty-quote pairs (``""`` / ``''``) a POSIX shell itself drops when they touch a
#: word: ``g""it`` -> ``git``, ``rm""`` -> ``rm``, ``""ssh`` -> ``ssh``. Matched with a word
#: char on EITHER side so a standalone empty argument (``rm -rf "" /x``, space-flanked on
#: both sides) is preserved as its own token.
#:
#: Each pattern matches a MAXIMAL run of pairs and carries NO trailing look-around, so the
#: scan is linear: the greedy ``+`` never backtracks (there is nothing after it to satisfy),
#: and the drop/keep decision is made from the run's neighbours in a callback. An earlier
#: ``(?:…)+(?=\w)`` form was O(n^2) on a long run of pairs -- the ``+`` ate the run at every
#: start position and then failed the look-ahead -- which stalled the synchronous deny gate.
_BARE_PAIR_RUN_RE = re.compile(r"(?<!\\)(?:\"\"|'')+")

#: Escaped empty-quote pairs (``\"\"`` / ``\'\'``) the shell keeps LITERAL. The floor
#: over-approximates them as droppable so an obfuscated verb (``s\"\"sh``, ``rm\"\"``,
#: ``rm -\"\"rf``) still reads as the verb -- the coverage ``main`` relied on. A run is
#: dropped UNLESS immediately adjacent to another quote or a backslash on either side: a
#: nested ``bash -c "…"`` script closes with a run of structural escaped quotes (``…id\"\"``
#: beside the ``"`` that closes the frame, or beside a deeper ``\``), and that adjacency marks
#: the pair as structure rather than a respelling, so it is left intact.
_ESC_PAIR_RUN_RE = re.compile(r"(?:\\\"\\\"|\\'\\')+")


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def _is_quote_or_backslash(ch: str) -> bool:
    return ch in ('"', "'", "\\")


def _collapse_pair_runs(text: str, run_re: "re.Pattern[str]", keep) -> str:
    """Drop each maximal run *run_re* matches unless ``keep(before, after)`` is true, where
    *before* / *after* are the single chars flanking the run (``""`` past the string ends).
    Linear in ``len(text)`` -- the pattern has no backtracking tail and each run is visited
    once."""

    def _replace(match: "re.Match[str]") -> str:
        start, end = match.start(), match.end()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        return match.group(0) if keep(before, after) else ""

    return run_re.sub(_replace, text)


def _drop_empty_quote_pairs(cmd: str) -> str:
    """Remove empty quote pairs from *cmd* BEFORE tokenizing, so a verb respelled with them
    (``s""sh``, ``s''sh``, ``rm""``, ``s\\"\\"sh``, ``rm\\"\\"``) reads as the verb. A bare
    pair the shell drops is collapsed wherever it touches a word; an escaped pair the shell
    keeps literal is collapsed unless it is adjacent to another quote or a backslash (the mark
    of a nested script's structural closing quotes). Repeated until stable so a run of pairs
    and a mix of spellings all collapse. Doing this before tokenizing (not on the dequoted
    token) keeps it from truncating a nested ``bash -c "..."`` script, whose structural
    closing quotes are escaped pairs beside another quote rather than verb respellings."""
    prev = None
    while prev != cmd:
        prev = cmd
        cmd = _collapse_pair_runs(
            cmd, _BARE_PAIR_RUN_RE, lambda b, a: not (_is_word_char(b) or _is_word_char(a))
        )
        cmd = _collapse_pair_runs(
            cmd,
            _ESC_PAIR_RUN_RE,
            lambda b, a: _is_quote_or_backslash(b) or _is_quote_or_backslash(a),
        )
    return cmd


def _drop_token_empty_pairs(token: str) -> str:
    """Drop empty-quote pairs a DEQUOTED *token* still carries -- a pair that surfaces only
    after ``shlex`` strips one quoting layer (``rm'""'`` -> ``rm""``, ``rm -rf '""'/`` ->
    ``""/``). Both the bare and the escaped run are dropped unless adjacent to a quote or a
    backslash (a token that is itself a nested ``bash -c "..."`` script keeps its structural
    quotes); the space-flanked standalone ``rm -rf "" /x`` is already an EMPTY token here, so
    it carries no literal pair for this to match. Repeated until stable."""
    prev = None
    keep = lambda b, a: _is_quote_or_backslash(b) or _is_quote_or_backslash(a)  # noqa: E731
    while prev != token:
        prev = token
        token = _collapse_pair_runs(token, _BARE_PAIR_RUN_RE, keep)
        token = _collapse_pair_runs(token, _ESC_PAIR_RUN_RE, keep)
    return token


def _modes_may_diverge(cmd: str) -> bool:
    """The structure-preserving and default tokenizers can produce different tokens only on
    an empty-quote pair -- bare (``""`` / ``''``) or escaped (``\\"\\"`` / ``\\'\\'``). Such a
    pair needs a quote character, so a *cmd* with no quote at all is split identically by both
    modes, and the scanner mode may reuse the default-mode tokens from the shared memo."""
    return '"' in cmd or "'" in cmd
