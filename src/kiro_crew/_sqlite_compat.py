"""SQLite import shim + FTS5 capability probe.

KiroCrew's memory and knowledge stores require SQLite's FTS5 full-text
extension. We prefer ``pysqlite3`` (which bundles a recent SQLite with FTS5)
when present, and fall back to the stdlib ``sqlite3``.

``pysqlite3-binary`` only ships wheels for Linux x86_64 (see setup.cfg), so on
macOS and Linux aarch64 we rely on the host's stdlib SQLite having FTS5 built
in. Modern macOS and mainstream Linux distros do, but minimal container images
occasionally compile SQLite without ``SQLITE_ENABLE_FTS5``. When that happens,
``CREATE VIRTUAL TABLE ... USING fts5`` raises ``no such module: fts5`` — and a
naive delete-and-retry self-heal loops on the same failure forever. This module
provides a one-shot probe so callers can fail loudly with an actionable message
instead.
"""

from __future__ import annotations

import re
from functools import lru_cache


def fts5_quote_tokens(query: str) -> list[str]:
    """Quote each whitespace-separated token of ``query`` for FTS5 MATCH.

    One escaping dialect for every FTS5 reader in the tree. A token becomes a
    quoted string, so FTS5 reads it as text rather than syntax: unquoted, ``-``
    ``.`` and a bare ``AND`` are operators, which makes the everyday queries
    (``PROJ-123``, ``hooks.py``) raise inside the driver. Internal double quotes
    are doubled, the escape FTS5 defines for its own string literals.

    Returns the tokens rather than a finished expression: how they are joined is
    a per-surface product decision, not an escaping one. Memory search ANDs them
    (the user typed every word deliberately); knowledge retrieval drops stopwords
    and ORs them (natural-language recall).
    """
    return ['"' + token.replace('"', '""') + '"' for token in query.split()]


# Scripts that write words with no space between them, so whitespace carries no
# word boundary and `query.split()` yields one token per *phrase* rather than per
# word. Mirrors `history_search._is_cjk_char`, the character set the session
# search's CJK gate already ships; Hangul is deliberately absent from both,
# because modern Korean is space-separated and needs no segmentation.
_CJK_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2EBEF),  # CJK Unified Ideographs Extensions B..F (astral)
)


def is_cjk_char(ch: str) -> bool:
    """True if ``ch`` belongs to a script written without word spacing."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


# Derived from _CJK_RANGES rather than spelled out, so the two cannot drift apart.
_CJK_PATTERN = re.compile("([" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES) + "])")


def script_runs(text: str) -> list[tuple[str, bool]]:
    """Split ``text`` into maximal (chunk, is_cjk) runs, preserving order.

    Public because session search needs the same split: keeping a second copy
    there is what let the CJK character ranges drift apart in the first place.
    Returns a list rather than a generator so the result can be re-iterated, and
    tolerates an empty string instead of raising.
    """
    runs: list[tuple[str, bool]] = []
    current = ""
    flag: bool | None = None
    for ch in text:
        cjk = is_cjk_char(ch)
        if flag is not None and cjk != flag:
            runs.append((current, bool(flag)))
            current = ""
        current += ch
        flag = cjk
    if current:
        runs.append((current, bool(flag)))
    return runs


def fts5_segment_for_index(text: str) -> str:
    """Insert token boundaries around every CJK character.

    FTS5's default ``unicode61`` tokenizer classifies CJK ideographs as letters,
    so it stores a whole spaceless run as ONE token -- an entire clause becomes a
    single term and no query can address a word inside it. Padding each CJK
    character with spaces makes the tokenizer emit one term per character, which
    lets a query address an adjacent character pair as an FTS5 *phrase* (see
    :func:`fts5_cjk_match_groups`).

    Only the copy handed to the FTS index is transformed. On an external-content
    table (``content=items``) the stored column values are never read from the
    index, so ``snippet()`` and ``highlight()`` still see the original text.

    Non-CJK text is returned byte-identical, so Latin, Cyrillic and numeric
    matching keep their present behaviour exactly.

    Every writer of a segmented index MUST route through this function --
    including the ``'delete'`` command, which is matched against the terms that
    were originally indexed. Deleting with un-segmented text leaves the old
    terms in place, and FTS5's ``'integrity-check'`` does NOT report it.

    Substitution is a single C-level pass, so a document with no CJK -- the
    common case, and possibly megabytes of it on this write path -- costs one
    scan and returns the original object rather than rebuilding it.
    """
    if not text:
        return text
    return _CJK_PATTERN.sub(r" \1 ", text)


def _cjk_run_alternatives(run: str) -> list[str]:
    """Quoted FTS5 phrases matching ``run`` inside a character-segmented index.

    A run of n characters yields its n-1 overlapping bigrams, each as a phrase
    over the segmented characters -- two characters separated by a space, quoted
    as one FTS5 phrase. Bigrams are alternatives rather than requirements: it is
    the same adjacency floor the session search applies -- a hit needs at least
    one adjacent character pair, which keeps a document that merely scatters the
    same characters out of the result set, while still matching a document that
    spells the words apart. A single-character run has no bigram and contributes
    the character itself.
    """
    if len(run) == 1:
        return ['"' + run + '"']
    return ['"' + run[i] + " " + run[i + 1] + '"' for i in range(len(run) - 1)]


def fts5_cjk_match_groups(query: str) -> list[str]:
    """Build one FTS5 sub-expression per whitespace token of ``query``.

    Returns finished, parenthesised sub-expressions; how they are joined stays a
    per-surface recall decision, exactly as with :func:`fts5_quote_tokens`.
    Memory-style surfaces AND the groups (every word was typed deliberately),
    knowledge retrieval ORs them (natural-language recall).

    Within one token, script runs are ANDed -- a token that mixes Latin and CJK
    must match both halves -- and the bigrams of a CJK run are ORed, per
    :func:`_cjk_run_alternatives`.

    A query with no CJK produces exactly the quoted tokens
    :func:`fts5_quote_tokens` would produce, so callers keep their present
    behaviour on non-CJK input.
    """
    groups: list[str] = []
    for token in query.split():
        if not token:
            continue
        parts: list[str] = []
        for chunk, cjk in script_runs(token):
            if not cjk:
                parts.append('"' + chunk.replace('"', '""') + '"')
                continue
            alts = _cjk_run_alternatives(chunk)
            parts.append(alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")")
        if not parts:
            continue
        groups.append(parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")")
    return groups


try:
    import pysqlite3 as sqlite3  # type: ignore
except ImportError:  # pragma: no cover - exercised on platforms without pysqlite3
    import sqlite3  # type: ignore

__all__ = [
    "sqlite3",
    "fts5_available",
    "FTS5_UNAVAILABLE_HINT",
    "require_fts5",
    "fts5_quote_tokens",
    "fts5_segment_for_index",
    "fts5_cjk_match_groups",
    "is_cjk_char",
    "script_runs",
]

FTS5_UNAVAILABLE_HINT = (
    "SQLite FTS5 full-text extension is not available in this Python's sqlite3 "
    "build. KiroCrew memory and knowledge search require it.\n"
    "  - Linux x86_64: pip install pysqlite3-binary (KiroCrew depends on it here).\n"
    "  - Linux aarch64 / minimal images: install a python3 whose libsqlite3 was "
    "built with SQLITE_ENABLE_FTS5, or `pip install pysqlite3-binary` if a wheel "
    "exists for your platform.\n"
    "  - macOS: the system Python and Homebrew Python both ship FTS5; reinstall "
    "Python from python.org or Homebrew if this fails."
)


@lru_cache(maxsize=1)
def fts5_available() -> bool:
    """Return True if the resolved sqlite3 module supports FTS5.

    Probes an in-memory database once and caches the result for the process.
    """
    try:
        conn = sqlite3.connect(":memory:")
    except Exception:  # pragma: no cover - sqlite itself broken
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def require_fts5() -> None:
    """Raise RuntimeError with an actionable hint if FTS5 is unavailable."""
    if not fts5_available():
        raise RuntimeError(FTS5_UNAVAILABLE_HINT)
