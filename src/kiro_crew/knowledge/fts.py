"""CJK-aware text and query helpers for the Knowledge Library FTS index."""

from __future__ import annotations

from collections.abc import Container

# Keep the query bounded like the history search: every required character and
# adjacency phrase adds work to SQLite's MATCH expression.
_MAX_CJK_QUERY_CHARS = 12


def is_cjk_char(ch: str) -> bool:
    """Return whether *ch* belongs to a script commonly written without spaces."""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        or 0x20000 <= cp <= 0x2EBEF  # CJK Extensions B..F (astral)
        or 0xF900 <= cp <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x3040 <= cp <= 0x30FF  # Hiragana + Katakana
        or 0x31F0 <= cp <= 0x31FF  # Katakana Phonetic Extensions
    )


def script_runs(text: str) -> list[tuple[str, bool]]:
    """Return maximal ``(run, is_cjk)`` segments in *text*."""
    if not text:
        return []
    runs: list[tuple[str, bool]] = []
    start = 0
    current = is_cjk_char(text[0])
    for index in range(1, len(text)):
        next_is_cjk = is_cjk_char(text[index])
        if next_is_cjk != current:
            runs.append((text[start:index], current))
            start = index
            current = next_is_cjk
    runs.append((text[start:], current))
    return runs


def normalize_fts_text(text: str) -> str:
    """Insert token boundaries around CJK characters while preserving the text.

    ``unicode61`` indexes a contiguous CJK run as one token. Spaces are added
    only to the FTS copy, so callers can keep returning the original content.
    Existing ASCII text and its tokenization remain unchanged.
    """
    if not text:
        return text
    normalized: list[str] = []
    for index, char in enumerate(text):
        if is_cjk_char(char):
            if normalized and not normalized[-1].isspace():
                normalized.append(" ")
            normalized.append(char)
            if index + 1 == len(text) or not is_cjk_char(text[index + 1]):
                normalized.append(" ")
        else:
            normalized.append(char)
    return "".join(normalized)


def _quote_fts5_literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _cjk_query_clause(run: str) -> str:
    """Build a bounded character gate with an adjacency floor for *run*."""
    required_chars = list(dict.fromkeys(run))[:_MAX_CJK_QUERY_CHARS]
    required = " AND ".join(_quote_fts5_literal(char) for char in required_chars)
    if len(run) <= 1 or len(run) > _MAX_CJK_QUERY_CHARS:
        return f"({required})"

    bigrams = list(dict.fromkeys(
        f"{run[index]} {run[index + 1]}" for index in range(len(run) - 1)
    ))
    adjacent = " OR ".join(_quote_fts5_literal(bigram) for bigram in bigrams)
    return f"({required}) AND ({adjacent})"


def _query_clauses(query: str, stopwords: Container[str] = ()) -> list[str]:
    clauses: list[str] = []
    for token in query.split():
        token_clauses: list[str] = []
        for run, is_cjk in script_runs(token):
            if not is_cjk and run.lower() in stopwords:
                continue
            token_clauses.append(
                _cjk_query_clause(run) if is_cjk else _quote_fts5_literal(run)
            )
        if token_clauses:
            clauses.append(
                token_clauses[0]
                if len(token_clauses) == 1
                else f"({' AND '.join(token_clauses)})"
            )
    return clauses


def build_fts5_query(
    query: str, *, joiner: str = "AND", stopwords: Container[str] = ()
) -> str:
    """Build an injection-safe FTS5 query for Knowledge search.

    CJK runs become required character clauses and must retain at least one
    adjacent query bigram when the run fits the bound. Other terms retain the
    caller's join semantics (``OR`` for hybrid recall, ``AND`` for store/API
    filtering). If stopword removal removes everything, the original terms are
    used so an all-stopword query remains searchable.
    """
    if joiner not in {"AND", "OR"}:
        raise ValueError(f"unsupported FTS5 joiner: {joiner}")
    clauses = _query_clauses(query, stopwords)
    if not clauses and stopwords:
        clauses = _query_clauses(query)
    return f" {joiner} ".join(clauses)


def expand_query_terms(query: str) -> list[str]:
    """Return graph lookup terms, including CJK characters and bigrams."""
    raw_terms = query.split()
    terms = list(raw_terms)
    for token in raw_terms:
        for run, is_cjk in script_runs(token):
            if not is_cjk:
                if run != token:
                    terms.append(run)
                continue
            terms.extend(dict.fromkeys(run))
            terms.extend(dict.fromkeys(
                run[index:index + 2] for index in range(len(run) - 1)
            ))
    terms.extend(
        f"{raw_terms[index]} {raw_terms[index + 1]}"
        for index in range(len(raw_terms) - 1)
    )
    return list(dict.fromkeys(terms))


__all__ = [
    "build_fts5_query",
    "expand_query_terms",
    "is_cjk_char",
    "normalize_fts_text",
    "script_runs",
]
