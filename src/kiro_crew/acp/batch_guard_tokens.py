"""The tokens the batch write guard compares: values a write quotes that only a
call's result or an earlier message could supply.

A token is a sha or another hex id, a Kiro Crew item, session or artifact id, a
long run, review or job id, or a PR number written as one (a hash and digits,
or a ``pull/`` path). A clock time is not a token, since a model stamps its own
reports with one; a time to the second counts only as proof (:func:`times_of`).

Read with plain string checks, not regular expressions: the text is split on the
characters that cannot be part of a token, and each piece is tested with
``startswith``, ``isdigit`` and a fixed hex character set.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "MAX_SCANNED_TEXT_CHARS",
    "is_hex",
    "is_minted",
    "times_of",
    "token_keys",
    "tokens_of",
]

#: Characters of one text :func:`tokens_of` reads. A token past the bound is not
#: seen, which can only make a later write citing it look unseen; a result that
#: long is past what a harness shows the model anyway.
MAX_SCANNED_TEXT_CHARS = 256 * 1024

#: The shortest prefix a hex token is also known by.
_MIN_HEX_PREFIX = 7
#: The longest sha prefix stored for a seen sha (shortcut: a 13-39 character
#: cut is not matched; widen this if writes quote such cuts).
_MAX_HEX_PREFIX = 12

_HEX_CHARS = frozenset("0123456789abcdef")
_HEX_ANY_CASE = frozenset("0123456789abcdefABCDEF")
_LETTERS = frozenset("abcdefABCDEF")
_DIGITS = frozenset("0123456789")
#: Characters that join the parts of one token (``chat-1-2``, ``pull/7``, ``#7``).
_JOINERS = "-/#"
#: Every other ASCII character that is not a word character ends a piece.
_SPLIT = str.maketrans(
    {
        chr(c): " "
        for c in range(128)
        if not (chr(c).isalnum() or chr(c) == "_" or chr(c) in _JOINERS)
    }
)
#: ``gh pr <verb> <n>``: the verbs whose operand is a PR number.
_GH_PR_VERBS = frozenset({"merge", "edit", "comment", "ready", "close", "review", "view", "checks"})


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _ascii_digits(text: str, low: int, high: int) -> bool:
    """Whether *text* is *low* to *high* ASCII digits."""
    return low <= len(text) <= high and text.isascii() and text.isdigit()


def _plain(text: str) -> str:
    """*text*, bounded, with JSON string escapes for line breaks and tabs read as
    blanks and escaped quotes read as quotes: a JSON-encoded result keeps
    ``abc1234\\ndef5678`` as one run of word characters, which would hide the
    second id, and turns ``"pr": 1742`` into ``\\"pr\\": 1742``."""
    text = text[:MAX_SCANNED_TEXT_CHARS]
    if "\\" in text:
        text = text.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
        text = text.replace('\\"', '"')
    return text


def _pieces(text: str) -> list[str]:
    """*text* split on every character that cannot be part of a token. A piece
    keeps the joiners ``-``, ``/`` and ``#``."""
    pieces = []
    for piece in text.translate(_SPLIT).split():
        if piece.isascii():
            pieces.append(piece)
        else:
            # Non-ASCII punctuation ends a piece too; non-ASCII letters do not.
            pieces.extend(
                "".join(c if _is_word_char(c) or c in _JOINERS else " " for c in piece).split()
            )
    return pieces


def _hex_token(word: str) -> str:
    """*word* lowercased when it is a 7-64 character hex id with a digit and a
    letter, else ``""``."""
    if not 7 <= len(word) <= 64 or not set(word) <= _HEX_ANY_CASE:
        return ""
    chars = set(word)
    if chars & _DIGITS and chars & _LETTERS:
        return word.lower()
    return ""


def _piece_tokens(piece: str, out: set[str]) -> None:
    """Add the tokens of one *piece* to *out*. A piece is word runs joined by
    ``-``, ``/`` or ``#``: ``words[k]`` follows ``seps[k - 1]``."""
    words: list[str] = []
    seps: list[str] = []
    start = 0
    for i, char in enumerate(piece):
        if char in _JOINERS:
            words.append(piece[start:i])
            seps.append(char)
            start = i + 1
    words.append(piece[start:])
    for k, word in enumerate(words):
        if not word:
            continue
        before = seps[k - 1] if k else ""
        hex_id = _hex_token(word)
        if hex_id:
            out.add(hex_id)
        if word.startswith("it_") and len(word) == 11 and set(word[3:]) <= _HEX_CHARS:
            out.add(word)
        elif word.startswith("art_") and 8 <= len(word) - 4 <= 32 and set(word[4:]) <= _HEX_CHARS:
            out.add(word)
        elif word == "chat" and k + 2 < len(words) and seps[k] == "-" and seps[k + 1] == "-":
            if _ascii_digits(words[k + 1], 1, 12) and _ascii_digits(words[k + 2], 1, 12):
                out.add(f"chat-{words[k + 1]}-{words[k + 2]}")
        if not (word.isascii() and word.isdigit()):
            continue
        # Run, review, job and event ids: long numbers not glued to a dash.
        if 10 <= len(word) <= 20 and before != "-":
            out.add(word)
        # PR and issue numbers where they are named as such.
        if before == "#" and 2 <= len(word) <= 6:
            out.add(word)
        if before == "/" and words[k - 1] == "pull" and len(word) <= 7:
            out.add(word)
        if before == "/" and words[k - 1] == "runs" and len(word) <= 20:
            out.add(word)


def _leading_word(text: str) -> str:
    end = 0
    while end < len(text) and _is_word_char(text[end]):
        end += 1
    return text[:end]


def _gh_pr_numbers(text: str, out: set[str]) -> None:
    """``gh pr <verb> <n>``, with single spaces and ``gh`` starting a word."""
    at = text.find("gh pr ")
    while at >= 0:
        if at == 0 or not _is_word_char(text[at - 1]):
            verb, space, rest = text[at + 6 : at + 6 + 64].partition(" ")
            number = _leading_word(rest)
            if space and verb in _GH_PR_VERBS and _ascii_digits(number, 1, 7):
                out.add(number)
        at = text.find("gh pr ", at + 1)


def _json_pr_numbers(value: Any, out: set[str], depth: int = 0) -> None:
    """Every ``"pr"`` field of a parsed JSON *value* that holds a 1-7 digit
    number, looking into string values that hold JSON themselves."""
    if depth > 32:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "pr" and isinstance(item, int) and not isinstance(item, bool):
                if _ascii_digits(str(item), 1, 7):
                    out.add(str(item))
            _json_pr_numbers(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_pr_numbers(item, out, depth + 1)
    elif isinstance(value, str) and value[:1] in ("{", "["):
        try:
            _json_pr_numbers(json.loads(value), out, depth + 1)
        except (ValueError, RecursionError):
            pass


def _pr_field_numbers(raw: str, plain: str, out: set[str]) -> None:
    """``"pr": <n>``: from the parsed JSON when *raw* is JSON, and found in the
    *plain* text with one optional space after the colon (a JSON value can sit
    in prose, or in a cut that does not parse)."""
    if raw.lstrip()[:1] in ("{", "["):
        try:
            _json_pr_numbers(json.loads(raw), out)
        except (ValueError, RecursionError):
            pass
    at = plain.find('"pr":')
    while at >= 0:
        rest = plain[at + 5 : at + 5 + 16]
        number = _leading_word(rest[1:] if rest.startswith(" ") else rest)
        if _ascii_digits(number, 1, 7):
            out.add(number)
        at = plain.find('"pr":', at + 1)


def tokens_of(text: Any) -> set[str]:
    """The tokens in *text* (its first :data:`MAX_SCANNED_TEXT_CHARS`)."""
    if not isinstance(text, str) or not text:
        return set()
    raw = text[:MAX_SCANNED_TEXT_CHARS]
    plain = _plain(raw)
    out: set[str] = set()
    for piece in _pieces(plain):
        _piece_tokens(piece, out)
    _gh_pr_numbers(plain, out)
    if '"pr' in plain:
        _pr_field_numbers(raw, plain, out)
    return out


def _is_two_digits(text: str) -> bool:
    return len(text) == 2 and set(text) <= _DIGITS


def times_of(text: Any) -> set[str]:
    """The to-the-second clock times in *text* (its first :data:`MAX_SCANNED_TEXT_CHARS`):
    ``07:30:45``, from ``07:30:45Z`` or ``07:30:45.120Z``, not glued to another
    digit or colon. Only ever proof: a write citing one is refused only when a
    returned call of its batch printed it."""
    if not isinstance(text, str) or not text:
        return set()
    text = _plain(text)
    out: set[str] = set()
    colon = text.find(":")
    while colon >= 0:
        start = colon - 2
        candidate = text[start : start + 8] if start >= 0 else ""
        if (
            len(candidate) == 8
            and candidate[5] == ":"
            and _is_two_digits(candidate[0:2])
            and _is_two_digits(candidate[3:5])
            and _is_two_digits(candidate[6:8])
            and (start == 0 or text[start - 1] not in _DIGITS and text[start - 1] != ":")
            and (
                start + 8 == len(text)
                or (text[start + 8] not in _DIGITS and text[start + 8] != ":")
            )
        ):
            out.add(candidate)
        colon = text.find(":", colon + 1)
    return out


def is_hex(token: str) -> bool:
    """Whether *token* is a hex id (a sha or a cut of one): 7-64 lowercase hex."""
    return 7 <= len(token) <= 64 and set(token) <= _HEX_CHARS


def token_keys(token: str) -> list[str]:
    """The keys a SEEN *token* is stored under: itself and, for a hex token, its
    prefixes of 7 to 12 characters (the lengths git abbreviates a sha to), so a
    write that shortens a seen sha that way matches. Seven keys per sha at most,
    so one result of :data:`MAX_BATCH_TOKENS` shas fits the session's seen set.

    Matching runs one way. A quoted token is looked up as itself, never by its own
    prefixes, so a seen short prefix never makes a longer quoted sha seen."""
    if is_hex(token):
        end = min(len(token), _MAX_HEX_PREFIX + 1)
        return [token] + [token[:n] for n in range(_MIN_HEX_PREFIX, end)]
    return [token]


def is_minted(token: str) -> bool:
    """Whether *token* is a session key (``chat-N-N``) or a ledger item id
    (``it_`` and 8 hex). A resumed session still refuses one its resumed text
    never showed: unlike a sha, such an id is new when a same-batch create
    returns it."""
    if token.startswith("it_"):
        return len(token) == 11 and set(token[3:]) <= _HEX_CHARS
    if token.startswith("chat-"):
        first, dash, second = token[5:].partition("-")
        return bool(dash) and _ascii_digits(first, 1, 12) and _ascii_digits(second, 1, 12)
    return False
