"""Detector H: credentials in HTTP headers, cookies, command lines and ``.netrc``.

Each reader anchors on a literal found in the lowered text with ``str.find`` and reads
the value after it by character sets; only the VALUE is reported, the header name, the
scheme, the cookie name, the flag, the keyword and every other field stay.

- ``header``: ``Authorization: Basic <base64>`` (a row's ``scheme``) and API-key
  headers (``x-api-key: <value>``), plain or JSON-quoted (``"x-api-key": "<value>"``);
  a Basic value whose decoded password is a placeholder (``username:password``), and a
  value the row allows (a published JWT example), is a documented example;
- ``cookie``: in a ``Cookie:`` header every ``name=value`` pair, in ``Set-Cookie:``
  the first, whose name marks a session or a token (a CSRF token grants nothing alone);
- ``curl_user``: the password of ``curl -u user:password`` (also ``--user``), only when
  ``curl`` is the command word on that line;
- ``mysql_password``: ``-p<password>`` glued to its flag (a blank after ``-p`` makes the
  client prompt, and the next word names a database) and ``--password=<password>``;
- ``docker_login``: ``-p <password>`` and ``--password <password>`` (or ``=``) of a
  ``login`` subcommand;
- ``bearer``: ``Bearer <token>`` with no ``Authorization`` name before it;
- ``netrc``: ``password <value>`` in a ``.netrc`` entry, after ``machine <host>`` or
  ``default`` with only ``login`` and ``account`` pairs between, on one line or across
  lines.

A command-line reader reads a flag only when one of its row's ``names`` is a word of
the same shell command before it (back to a line break, ``|``, ``;`` or ``&``, within
:data:`_LINE_MAX` characters). A value shorter than ``min_length``, a placeholder,
words (``checks.reads_as_words``) or one still running at the end of a stream window
(reported in ``opens``) is not a hit.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable

from .charsets import ALNUM
from .checks import is_placeholder, reads_as_words
from .model import PLAN_TAGS, Hit
from .table import HeaderRow

# A name is read where it is not the tail of a longer one: ``x-api-key`` is not ``api-key``,
# ``Set-Cookie`` not ``Cookie``. Punctuation before it is fine (``settled.Authorization``),
# and a scheme header may carry a prefix (``Proxy-Authorization``, ``X-Authorization``).
_NAME_CHARS = frozenset(ALNUM + "-_")
# A space between a scheme and its value: an ASCII blank, a no-break or any other
# Unicode space a copy from a rendered page can carry.
_BLANKS = " \t\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
_ASCII_BLANKS = " \t"
_SPACE = frozenset(" \t\r\n")
_QUOTES = "\"'"
_TOKEN = frozenset(ALNUM + "._~+/-=")
_COOKIE_VALUE_STOPS = frozenset(";, \t\r\n\"'")
_WORD_STOPS = frozenset(" \t\r\n\"'")
_SESSION_WORDS = ("session", "sess", "token", "auth")
_SESSION_NAMES = frozenset({"sid", "phpsessid", "jsessionid"})
# A request-forgery token only works beside the session cookie: it grants nothing alone.
_FORGERY_WORDS = ("csrf", "xsrf")
_CURL_FLAGS = ("-u", "--user")
_MYSQL_FLAGS = ("-p", "--password=")
_DOCKER_FLAGS = ("-p", "--password")
_DOCKER_SUBCOMMAND = "login"
# How far back a command-line reader looks for the command word, and where a shell
# command starts.
_LINE_MAX = 512
_COMMAND_STOPS = "\r\n|;&"
_NETRC_KEYWORD = "password"
_BEARER_WORD = "bearer"
_NETRC_ENTRY = "machine"
_NETRC_DEFAULT = "default"
_NETRC_PAIRS = frozenset({"login", "account"})
# Tokens read back from ``password``: ``machine``, its host, two more pairs.
_NETRC_TOKENS = 6
# A Basic value longer than this is not decoded to look for a placeholder.
_BASIC_DECODE_MAX = 512


class _Open(Exception):
    """The text ends before the unit is decided (non-final scans only)."""


def _skip(text: str, i: int, chars: str) -> int:
    while i < len(text) and text[i] in chars:
        i += 1
    return i


def _after_name(text: str, end: int) -> int | None:
    """Where the value starts after a header name ending at *end*: ``: v``, ``": "v``,
    or an assignment ``= v`` / ``="v"`` (a header set from a variable or a config)."""
    i = end
    if i < len(text) and text[i] in _QUOTES:
        i += 1
    i = _skip(text, i, _BLANKS)
    if i >= len(text) or text[i] not in ":=":
        return None
    i = _skip(text, i + 1, _BLANKS)
    if i < len(text) and text[i] in _QUOTES:
        i += 1
    return i


def _value(text: str, start: int, stops: frozenset[str] | None, final: bool) -> int:
    """End of a value run from *start*: token characters, or up to one of *stops*."""
    i = start
    n = len(text)
    while i < n and (text[i] in _TOKEN if stops is None else text[i] not in stops):
        i += 1
    if i == n and not final:
        raise _Open
    return i


def _keep(text: str, start: int, end: int, row: HeaderRow) -> bool:
    if any(text.startswith(tag, start) for tag in PLAN_TAGS):
        return False
    return end - start >= row.min_length and not is_placeholder(text[start:end])


def _basic_placeholder(value: str) -> bool:
    """A Basic credential whose decoded password is a placeholder (``username:password``)."""
    if len(value) > _BASIC_DECODE_MAX:
        return False
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    _user, colon, password = decoded.partition(":")
    return bool(colon) and is_placeholder(password)


def _header(text: str, low: str, at: int, name: str, row: HeaderRow, final: bool) -> Hit | None:
    i = _after_name(text, at + len(name))
    if i is None:
        return None
    if row.scheme:
        if low.startswith(row.scheme, i):
            j = i + len(row.scheme)
            # Blanks, or a line break: a header logged with its value on the next line.
            k = _skip(text, j, _BLANKS)
            if k < len(text) and text[k] in "\r\n":
                k = _skip(text, k, "\r\n")
                k = _skip(text, k, _BLANKS)
            if k == j:
                return None
            i = k
        elif i + len(row.scheme) >= len(text) and not final:
            raise _Open
        else:
            return None
    end = _value(text, i, None, final)
    if not _keep(text, i, end, row):
        return None
    if row.scheme == "basic" and _basic_placeholder(text[i:end]):
        return None
    if row.allow_values and text[i:end].rsplit(".", 1)[0] in row.allow_values:
        return None
    return Hit(i, end, row.id, row.tier, "header", "known_key")


def _session_name(name: str) -> bool:
    low = name.lower()
    if any(word in low for word in _FORGERY_WORDS):
        return False
    return low in _SESSION_NAMES or any(word in low for word in _SESSION_WORDS)


def _cookies(text: str, at: int, name: str, row: HeaderRow, final: bool) -> list[Hit]:
    i = _after_name(text, at + len(name))
    if i is None:
        return []
    line_end = len(text)
    for stop in "\r\n":
        found = text.find(stop, i)
        if found != -1:
            line_end = min(line_end, found)
    hits: list[Hit] = []
    first_only = name == "set-cookie"
    while i < line_end:
        eq = text.find("=", i, line_end)
        if eq == -1:
            break
        cookie = text[i:eq].strip()
        vs = eq + 1
        ve = _value(text, vs, _COOKIE_VALUE_STOPS, final or line_end < len(text))
        if _session_name(cookie) and _keep(text, vs, ve, row):
            hits.append(Hit(vs, ve, row.id, row.tier, "cookie", "known_key"))
        if first_only:
            break
        semi = text.find(";", ve, line_end)
        if semi == -1:
            break
        i = _skip(text, semi + 1, _BLANKS)
    return hits


def _curl_user(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``curl ... -u user:password``: the flag at *at* follows ``curl`` on its line."""
    line_start = max(text.rfind("\n", 0, at), text.rfind("\r", 0, at)) + 1
    head = text[line_start:at]
    if not any(
        word == "curl" or word.endswith("/curl") for word in head.replace("|", " ").split()[:3]
    ):
        return None
    flag_end = at + (2 if text.startswith("-u", at) else len("--user"))
    if flag_end < len(text) and text[flag_end] == "=":
        i = flag_end + 1
    else:
        i = _skip(text, flag_end, _BLANKS)
        if i == flag_end:
            return None
    quote = text[i] if i < len(text) and text[i] in _QUOTES else ""
    if quote:
        i += 1
    stops = frozenset(quote + "\r\n") if quote else frozenset(" \t\r\n\"'")
    end = _value(text, i, stops, final)
    colon = text.find(":", i, end)
    if colon == -1 or not _keep(text, colon + 1, end, row):
        return None
    return Hit(colon + 1, end, row.id, row.tier, "curl_user", "known_key")


def _command_words(text: str, at: int) -> list[str]:
    """The words of the shell command before *at* (module docstring)."""
    lo = max(0, at - _LINE_MAX)
    start = max(text.rfind(ch, lo, at) for ch in _COMMAND_STOPS) + 1
    return text[max(start, lo) : at].split()


def _runs(words: list[str], names: tuple[str, ...]) -> bool:
    return any(w.rsplit("/", 1)[-1] in names for w in words)


def _word_value(text: str, i: int, row: HeaderRow, final: bool, detector: str) -> Hit | None:
    """The word at *i*, or the text inside the quotes there, as a Secret hit."""
    if i >= len(text):
        if not final:
            raise _Open
        return None
    quote = text[i] if text[i] in _QUOTES else ""
    if quote:
        i += 1
    end = _value(text, i, frozenset(quote + "\r\n") if quote else _WORD_STOPS, final)
    if not _keep(text, i, end, row) or reads_as_words(text[i:end]):
        return None
    return Hit(i, end, row.id, row.tier, detector, "known_key")


def _flag(text: str, at: int, flags: tuple[str, ...], final: bool) -> str | None:
    """The flag of *flags* at *at* (case as written), and the text after it is there."""
    for flag in flags:
        if text.startswith(flag, at):
            if at + len(flag) >= len(text):
                if not final:
                    raise _Open
                return None
            return flag
    return None


def _mysql_password(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``mysql ... -p<password>`` or ``--password=<password>`` (module docstring)."""
    flag = _flag(text, at, _MYSQL_FLAGS, final)
    if flag is None:
        return None
    i = at + len(flag)
    if text[i] in _SPACE or not _runs(_command_words(text, at), row.names):
        return None
    return _word_value(text, i, row, final, "cli_password")


def _docker_login(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``docker login ... -p <password>`` or ``--password <password>`` (also ``=``)."""
    flag = _flag(text, at, _DOCKER_FLAGS, final)
    if flag is None:
        return None
    i = at + len(flag)
    if text[i] == "=":
        i += 1
    elif text[i] in _ASCII_BLANKS:
        i = _skip(text, i, _ASCII_BLANKS)
    elif flag != "-p":
        return None  # a longer flag (``--password-stdin``)
    words = _command_words(text, at)
    if _DOCKER_SUBCOMMAND not in words or not _runs(words, row.names):
        return None
    if i < len(text) and text[i] in _SPACE:
        return None
    return _word_value(text, i, row, final, "cli_password")


def _netrc_entry(text: str, at: int) -> bool:
    """Whether the tokens before *at* are a ``.netrc`` entry's (module docstring)."""
    tokens = text[max(0, at - _LINE_MAX) : at].split()[-_NETRC_TOKENS:]
    k = len(tokens)
    while k > 0:
        if tokens[k - 1] == _NETRC_DEFAULT:
            return True
        if k < 2:
            return False
        keyword = tokens[k - 2]
        if keyword == _NETRC_ENTRY:
            return True
        if keyword not in _NETRC_PAIRS:
            return False
        k -= 2
    return False


def _netrc(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``password <value>`` in a ``.netrc`` entry (module docstring)."""
    if not text.startswith(_NETRC_KEYWORD, at) or (at > 0 and text[at - 1] not in _SPACE):
        return None
    i = at + len(_NETRC_KEYWORD)
    if i >= len(text):
        if not final:
            raise _Open
        return None
    if text[i] not in _ASCII_BLANKS:
        return None
    i = _skip(text, i, _ASCII_BLANKS)
    if not _netrc_entry(text, at):
        return None
    if i < len(text) and text[i] in _SPACE:
        return None
    return _word_value(text, i, row, final, "netrc")


def _bearer_bare(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``Bearer <token>`` where no ``Authorization`` name leads it (module docstring)."""
    i = at + len(_BEARER_WORD)
    if i >= len(text):
        if not final:
            raise _Open
        return None
    k = _skip(text, i, _ASCII_BLANKS)
    if k == i:
        return None
    end = _value(text, k, None, final)
    # A percent escape (``%2F``) is part of the token: stopping at it would leave the tail of
    # an encoded credential visible.
    while end < len(text) and text[end] == "%":
        end = _value(text, end + 1, None, final)
    value = text[k:end]
    if not _keep(text, k, end, row) or reads_as_words(value) or _capitalised_word(value):
        return None
    if row.allow_values and value.rsplit(".", 1)[0] in row.allow_values:
        return None
    return Hit(k, end, row.id, row.tier, "header", "known_key")


def _capitalised_word(value: str) -> bool:
    """Letters only, upper case at most in the first place or throughout (``Credentials``,
    ``AUTHORIZATION``): prose after the word ``bearer``, not a token."""
    if not value.isalpha() or not value.isascii():
        return False
    return not any(ch.isupper() for ch in value[1:]) or value.isupper()


_Reader = Callable[[str, int, HeaderRow, bool], "Hit | None"]
#: Readers that anchor on their own fixed literals, and those literals.
_FIXED: dict[str, tuple[tuple[str, ...], _Reader]] = {
    "curl_user": (_CURL_FLAGS, _curl_user),
    "mysql_password": (_MYSQL_FLAGS, _mysql_password),
    "docker_login": (_DOCKER_FLAGS, _docker_login),
    "netrc": ((_NETRC_KEYWORD,), _netrc),
    "bearer": ((_BEARER_WORD,), _bearer_bare),
}


def detect_headers(
    text: str, rows: tuple[HeaderRow, ...], final: bool, low: str
) -> tuple[list[Hit], list[int]]:
    hits: list[Hit] = []
    opens: list[int] = []
    for row in rows:
        fixed = _FIXED.get(row.reader)
        names = fixed[0] if fixed is not None else row.names
        for name in names:
            at = low.find(name)
            while at != -1:
                left_ok = (
                    at == 0
                    or text[at - 1] not in _NAME_CHARS
                    or (bool(row.scheme) and text[at - 1] == "-")
                )
                try:
                    if left_ok and row.reader == "header":
                        found = _header(text, low, at, name, row, final)
                        if found is not None:
                            hits.append(found)
                    elif left_ok and row.reader == "cookie":
                        hits += _cookies(text, at, name, row, final)
                    elif left_ok and fixed is not None:
                        found = fixed[1](text, at, row, final)
                        if found is not None:
                            hits.append(found)
                except _Open:
                    opens.append(at)
                at = low.find(name, at + 1)
    return hits, opens
