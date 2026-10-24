"""Whether a value behind a secret label is a secret: the one shared predicate.

The batch redactor (:mod:`kiro_crew.security.redaction_labels`) and the log
filter (:mod:`kiro_crew.log_redaction`) both read a labelled value here, so the
two cannot drift apart. A value is a secret unless the WHOLE value is on a
short exemption list; nothing here guesses from a value's first characters or
from the code around it. Imports only the standard library and
:mod:`kiro_crew.credential_patterns`, so the log filter stays an import leaf.
"""

from __future__ import annotations

import contextvars
import re

from kiro_crew.credential_patterns import (
    AWS_KEY_ID_BODY,
    AWS_KEY_ID_PREFIXES,
    AWS_KEY_ID_REDACTION_ONLY_PREFIXES,
    REFERENCE_VALUE,
)

#: The shortest value a label rule redacts. Shorter values are flags, units and
#: words (``password: no``), not secrets.
MIN_VALUE_LEN = 8

#: How much of a quoted value the stream's window around a label covers: the
#: reach the view passes and the stream's tail are sized by.
QUOTED_MAX = 512

#: The longest quoted value read through to its closing quote, in every reader:
#: a long one (a session token, a password with commas) is removed whole.
QUOTED_READ_MAX = 4096

#: A one-case run of letters this long is a passphrase, not a word.
WORD_LEN_MAX = 15

_REFERENCE_RE = re.compile(REFERENCE_VALUE)

#: A whole redaction tag (``[REDACTED: credential]``): a fixed point, not a value.
#: Only the tags the redactors themselves write: any other text inside the
#: brackets is the writer's own, and may be a secret dressed as a tag.
_TAG_RE = re.compile(
    r"\[REDACTED(?:"
    r": (?:credential|encoded credential|policy request header"
    r"|unscannable JSON payload|suspicious URL)"
    r"| PRIVATE KEY|-(?:ACCESS|REFRESH|SSO))?\]"
)

#: The digest mask the MCP launch display writes for a hidden env value
#: (``KEY=<hidden sha256:0123abcd>``): a fixed point, as a tag is.
_DIGEST_MASK_RE = re.compile(r"<hidden sha256:[0-9a-f]{8}>")

#: A secret-store reference (``secret://name``, ``vault://path``, ``env:NAME``):
#: it names where a secret lives, and is not one.
_STORE_REFERENCE_RE = re.compile(r"(?i:[a-z][a-z0-9+.-]{1,15}://|(?:env|ssm|op|keychain):)")

#: A value that can only NAME something: an AWS key ID, a 12-digit account ID,
#: an ARN. Identifier tier, so it is never redacted behind a label.
NAMING_VALUE = (
    rf"(?:{AWS_KEY_ID_PREFIXES}|{AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){AWS_KEY_ID_BODY}"
    r"|[0-9]{12}|arn:[a-z0-9-]+:\S*"
)
_NAMING_RE = re.compile(NAMING_VALUE, re.ASCII)


def value_extent(text: str, start: int, quote: str) -> int:
    """End of a value opened by *quote* at *start*: its unescaped closing quote.

    The extent depends on the quote state only. A value still open at the line
    end or the text end ends there, so it is removed whole up to that point
    rather than cut at its first delimiter. Past :data:`QUOTED_READ_MAX`
    characters the rest is read in one pass to the closing quote or the line
    end: a long value is never cut short and its tail released in the clear.
    """
    pos = start
    limit = min(len(text), start + QUOTED_READ_MAX)
    while pos < limit:
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == "'" == quote and pos > start and text[pos + 1 : pos + 2] == "'":
            # A doubled single quote is one quote inside the value (YAML, SQL:
            # ``'it''s'``), not its close; an empty ``''`` still closes at once.
            pos += 2
            continue
        if char == quote or char == "\n":
            return pos
        pos += 1
    if pos >= len(text):
        return len(text)
    rest = _QUOTED_REST_RE[quote].match(text, pos)
    return rest.end() if rest is not None else pos


#: The rest of a quoted value past :data:`QUOTED_READ_MAX`: escapes and any byte
#: but its quote or a line break.
_QUOTED_REST_RE = {q: re.compile(rf"(?:[^{q}\\\n]|\\.)*") for q in ('"', "`")}
_QUOTED_REST_RE["'"] = re.compile(r"(?:[^'\\\n]|\\.|'')*")


def xml_value_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of the value of an XML element the label names (``<password>v</``):
    its closing ``</``, so punctuation inside the value is read as value. None
    when the label is no element or the element does not close on its line."""
    if text[value_start - 1 : value_start] != ">":
        return None
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-:"):
        pos -= 1
    if text[pos - 1 : pos] != "<":
        return None
    # Read to the closing tag at any length on its line, as a quoted value is
    # read past QUOTED_READ_MAX: a long value is never cut at its punctuation.
    line_end = text.find("\n", value_start)
    close = text.find("</", value_start, len(text) if line_end == -1 else line_end)
    if close == -1 or "<" in text[value_start:close]:
        return None
    return close


#: A YAML block-scalar header at a value: ``|`` or ``>``, an optional chomping
#: and indentation indicator, blanks and an optional comment, then the line end.
_BLOCK_HEADER_RE = re.compile(r"[|>](?:[-+][0-9]?|[0-9][-+]?)?[ \t]*(?:#[^\n]*)?\n")


def block_scalar_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of a YAML block scalar the label opens (``key: |`` and its indented
    lines): the end of its last body line. None when *value_start* holds no
    block header, or no body line follows it.

    The body is every following line that is blank or indented deeper than the
    label's own line, as YAML reads it."""
    header = _BLOCK_HEADER_RE.match(text, value_start)
    if header is None:
        return None
    line_start = text.rfind("\n", 0, label_start) + 1
    indent = len(text[line_start:label_start]) - len(text[line_start:label_start].lstrip(" "))
    pos, end = header.end(), None
    while pos < len(text):
        nl = text.find("\n", pos)
        line = text[pos : len(text) if nl == -1 else nl]
        if line.strip():
            if len(line) - len(line.lstrip(" ")) <= indent:
                break
            end = pos + len(line.rstrip())
        if nl == -1:
            break
        pos = nl + 1
    return end


#: The shell's working-directory variables (``PWD=/srv/app``): ``pwd`` there
#: names a directory, not a password.
_WORKING_DIR_VAR_RE = re.compile(r"(?<![A-Za-z0-9_])(?:OLD)?PWD[ \t]*[=:][ \t]*\Z")


def names_working_dir(label: str) -> bool:
    """Whether *label* (with its separator) is the shell's ``PWD``/``OLDPWD``."""
    return _WORKING_DIR_VAR_RE.search(label) is not None


#: What precedes a URL query parameter's name: ``?``, ``&``, their percent
#: codes, or an HTML-escaped ``&``.
_QUERY_LEAD_RE = re.compile(r"(?:[?&]|%3[Ff]|%26|&amp;|&#0{0,4}38;|&#[xX]0{0,4}26;)\Z")


def in_url_query(text: str, label_start: int) -> bool:
    """Whether the label word at *label_start* names a URL query parameter
    (``?password=...``): its value is data, a literal, never code."""
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-"):
        pos -= 1
    lead = text[max(0, pos - 8) : pos]
    prefix = LINE_PREFIX.get()
    if pos < 8 and prefix and prefix is not _DECODED:
        # A stream window that starts mid-line: what the line already emitted
        # holds the ``?`` or ``&`` before the name.
        lead = prefix[-8:] + lead
    return _QUERY_LEAD_RE.search(lead) is not None


def in_closing_tag(text: str, label_start: int) -> bool:
    """Whether the label word at *label_start* names a closing XML tag
    (``</password>``): no value follows it."""
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-:"):
        pos -= 1
    return text[max(0, pos - 2) : pos] == "</"


def closes_label_literal(text: str, label_start: int, value_start: int) -> bool:
    """Whether the quote right before *value_start* closes a string literal the
    label word sits in (``getpass("Password:")``, ``unknown key "api_key:"``)
    rather than opening a value: the same quote stands before the label's word
    and nothing between closes it. ``"password": "x"`` closes its key first."""
    quote = text[value_start - 1 : value_start]
    if quote not in ('"', "'"):
        return False
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-"):
        pos -= 1
    return text[pos - 1 : pos] == quote and quote not in text[pos : value_start - 1]


def is_tag(value: str) -> bool:
    """Whether *value* is a whole redaction tag."""
    return _TAG_RE.fullmatch(value) is not None


def without_tags(text: str, start: int, end: int) -> str:
    """``text[start:end]`` with every whole redaction tag that starts in it
    removed: what is left is the writer's own text, a forged tag included."""
    out: list[str] = []
    pos = start
    for tag in _TAG_RE.finditer(text, start, len(text)):
        if tag.start() >= end:
            break
        out.append(text[pos : tag.start()])
        pos = max(pos, tag.end())
    if pos < end:
        out.append(text[pos:end])
    return "".join(out)


def is_naming_value(value: str) -> bool:
    """True when the WHOLE value is a key ID, an account ID or an ARN: names only."""
    return _NAMING_RE.fullmatch(value) is not None


def _is_dotted_name(value: str) -> bool:
    """A chain of two or more names of letters and ``_`` (``settings.aws_key``)."""
    parts = value.split(".")
    return len(parts) > 1 and all(p and p.isascii() and p.replace("_", "").isalpha() for p in parts)


#: Labels whose word also names something that is not a secret: ``pwd`` (a
#: working directory, a shell command), a glued ``_pass`` / ``_token`` and a bare
#: ``signature``. A quoted value behind one keeps the length and word
#: exemptions (``{"bash": {"pwd": "deny"}}``); behind a strong label it does not.
_WEAK_LABEL_RE = re.compile(r"(?i:pwd|[_.-]pass|[_.-]token|signature)[\"'*_\s:=>\uff1a\uff1d\\]*\Z")


#: The AWS labels that end like a weak one (``aws_session_token``): never weak.
_AWS_TAIL_RE = re.compile(r"(?i:(?:session|security)[ _.-]?token)[\"'*_\s:=>\uff1a\uff1d\\]*\Z")


def is_weak_label(label: str) -> bool:
    """Whether *label* (with its separator) is a weak label (:data:`_WEAK_LABEL_RE`);
    an AWS label is never weak."""
    return _WEAK_LABEL_RE.search(label) is not None and _AWS_TAIL_RE.search(label) is None


#: The shortest digit run behind a secret label that is a PIN.
MIN_PIN_LEN = 4

_UNICODE_ESCAPES_RE = re.compile(r"(?:\\u[0-9A-Fa-f]{4})+")

#: A code name that carries a secret label's word (``new_password``,
#: ``password_hash``, ``userApiKey``): lower-case first, letters and ``_`` only,
#: with a ``_`` or a capital inside, so it reads as an identifier.
_LABEL_WORD_NAME_RE = re.compile(r"[a-z_][A-Za-z_]*")
_LABEL_WORDS_RE = re.compile(r"(?i:passw(?:or)?d|passphrase|secret|token|api_?key|pwd)")


def _is_label_word_name(value: str) -> bool:
    """Whether *value*, written unquoted, is a variable named after the secret
    it holds (``password = new_password``): a name for the value, not the value."""
    return (
        _LABEL_WORD_NAME_RE.fullmatch(value) is not None
        and ("_" in value or not value.islower())
        and _LABEL_WORDS_RE.search(value) is not None
    )


def _is_word(value: str) -> bool:
    """A single-case word of letters at most :data:`WORD_LEN_MAX` long."""
    return value.isalpha() and (value.islower() or value.isupper()) and len(value) <= WORD_LEN_MAX


def is_exempt_value(
    value: str, *, aws: bool = False, quoted: bool = False, weak: bool = False
) -> bool:
    """Whether the WHOLE *value* names or masks a secret instead of holding one.

    The list: shorter than :data:`MIN_VALUE_LEN` (not a PIN of
    :data:`MIN_PIN_LEN` or more digits); a code name carrying the label's word
    (``new_password``, ``userApiKey``); a reference or a mask
    (``$VAR``, ``${VAR}``, ``%VAR%``, ``{{ x }}``, ``<your key>``, ``****``); a
    secret-store reference; a redaction tag; a single-case word of letters no
    longer than :data:`WORD_LEN_MAX` (``password: required``); a dotted chain of
    letter names (``settings.aws_key``); a path (``/srv/app``, ``~/x``, ``./x``).
    Every test is on the whole value. A *quoted* value is a literal, so only a
    reference, a store reference or a tag exempts it, whatever its length.
    """
    if not value or is_tag(value):
        return True
    if _REFERENCE_RE.fullmatch(value) is not None or _STORE_REFERENCE_RE.match(value):
        return True
    if quoted:
        # A quoted value behind a secret label is a literal the writer chose to
        # quote: no length, word, dotted-name or path test applies to it.
        return False
    if not any(c.isalnum() for c in value):
        # No letter or digit at all (``==``, ``->``): an operator, not a value.
        return True
    if _NULL_OR_BOOL_RE.fullmatch(value):
        # A language's null or boolean literal (``password = null``, ``"token":
        # false``): no value is set. A config line reads its value as a literal
        # before this is reached, so ``PASSWORD=false`` there is still redacted.
        return True
    if weak:
        # A weak label (``pwd``, a glued ``_token``) also names things that are
        # not secrets: a short value or a plain word behind it stays.
        if len(value) < MIN_VALUE_LEN:
            # A run of four or more digits is a PIN, not a flag or a unit.
            return not (len(value) >= MIN_PIN_LEN and value.isdigit())
        if _is_word(value) or _is_label_word_name(value):
            return True
    # Behind a strong label only a value that is visibly code names the secret
    # instead of holding it (``load_key()``, ``settings.key``, ``env["K"]``,
    # ``new_password``, ``userKey``, ``$VAR``): a plain word is a password at any
    # length or case. Behind an AWS label not even an identifier is code.
    if aws:
        return _is_dotted_name(value)
    return _CODE_VALUE_RE.fullmatch(value) is not None or (
        _PATH_VALUE_RE.fullmatch(value) is not None
    )


#: The null and boolean literals of the languages and formats a label sits in.
_NULL_OR_BOOL_RE = re.compile(r"null|NULL|None|nil|undefined|true|false|True|False")

#: A value that is visibly code, not a secret: a call (``load_key()``), a dotted
#: or indexed name (``settings.key``, ``env["K"]``), an identifier with a ``_``
#: or a capital inside (``new_password``, ``userKey``), or a variable reference.
_CODE_VALUE_RE = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|[A-Za-z_]+(?:\.[A-Za-z_]+)+"
    r"|[A-Za-z_][A-Za-z0-9_.]*(?:\[[^\]\n]{0,256}\]|\([^)\n]{0,256}\))"
    # snake_case or SNAKE_CASE words, and camelCase words: a capital is followed
    # by lower case, so a random token's mixed case does not read as one.
    r"|_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+|_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r"|[a-z]+(?:[A-Z][a-z]+)+"
)

#: The only literal a config line exempts: a variable reference, exactly.
_CONFIG_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

#: A config line up to its value: indentation, an ``export`` or a YAML list
#: dash, the key, and ``=`` or ``:`` with blanks around it (dotenv, ini, shell,
#: YAML, TOML).
_CONFIG_PREFIX_RE = re.compile(r"[ \t]*(?:export[ \t]+|-[ \t]+)?")
#: The key and its separator: ``NAME=`` with nothing around the ``=``, or
#: ``key:`` and blanks (YAML).
_CONFIG_KEY_SEP_RE = re.compile(r"[A-Za-z0-9_-]+(?:=|:[ \t]+)")
#: Where a config value's trailing comment starts: a blank, then ``#``.
_CONFIG_COMMENT_RE = re.compile(r"[ \t]#")


#: What a line already holds before the text being read starts: ``""`` when
#: the text starts a line (the default), the part of the line a stream already
#: emitted, or ``None`` when that is not known. A config line's start is judged
#: on it, so a stream window reads the line as the whole text would.
LINE_PREFIX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "config_line_prefix", default=""
)

#: :data:`LINE_PREFIX` while a decoded view is read (``password%3Da,b``): its
#: separators came from an encoding, so no line in it is a config file's line.
_DECODED = "\x00decoded"


def reading_decoded() -> contextvars.Token[str | None]:
    """Mark the text read until the returned token is reset as a decoded view."""
    return LINE_PREFIX.set(_DECODED)


def config_value_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of an unquoted value on a config line: ``NAME=value`` at the line
    start with nothing around the ``=`` (dotenv, ini, shell, an ``export``
    allowed), or ``key: value`` (YAML, a list ``- `` allowed). The value runs to
    the line end or to a ``#`` comment after a blank, through ``,`` and ``;``,
    trailing blanks dropped. None when the label does not start such a line or
    the value is quoted. ``key = value`` with blanks around the ``=`` is code
    (``password = new_password``), not this form."""
    if text[value_start - 1 : value_start] in ('"', "'") or value_start >= len(text):
        return None
    if LINE_PREFIX.get() is _DECODED:
        return None
    line_start = text.rfind("\n", 0, label_start) + 1
    key_start = label_start
    while key_start > line_start and (text[key_start - 1].isalnum() or text[key_start - 1] in "_-"):
        key_start -= 1
    if line_start == 0:
        prefix = LINE_PREFIX.get()
        if prefix is None or _CONFIG_PREFIX_RE.fullmatch(prefix + text[:key_start]) is None:
            return None
    elif _CONFIG_PREFIX_RE.fullmatch(text, line_start, key_start) is None:
        return None
    if _CONFIG_KEY_SEP_RE.fullmatch(text, key_start, value_start) is None:
        return None
    line_end = text.find("\n", value_start)
    line_end = len(text) if line_end == -1 else line_end
    comment = _CONFIG_COMMENT_RE.search(text, value_start, line_end)
    end = comment.start() if comment is not None else line_end
    end = value_start + len(text[value_start:end].rstrip())
    return end if end > value_start else None


#: A file path a label names (``/usr/local/bin``, ``./config.yaml``,
#: ``~/.ssh/id_rsa``): lower-case segments only. A base64 secret that merely
#: starts with ``/`` carries upper case too, so it is no path.
_PATH_VALUE_RE = re.compile(r"(?:~|\.{1,2})?(?:/[a-z0-9._-]+)+/?")


def labelled_value_is_secret(
    value: str,
    *,
    quoted: bool,
    next_char: str,
    aws: bool = False,
    weak: bool = False,
    config: bool = False,
) -> bool:
    """Whether a value behind a secret label is a secret.

    It is, unless the whole value is exempt (:func:`is_exempt_value`), it can
    only name something (:func:`is_naming_value`), or, written unquoted, it is
    called or subscripted (*next_char* is ``(`` or ``[``: ``load_key(path)``,
    ``os.environ[...]``). Behind an AWS secret label (*aws*) a value shaped
    like a key ID is still the secret the label names, so it is not vetoed.
    A run of digits is never a name behind a secret label: it can be a PIN or
    a numeric password, and an account ID keeps its own label (``account``).
    """
    # Every secret label is strong on a config line (``DB_PASS=...``,
    # ``PWD=...``): the form says the value is the field's literal.
    if config and not quoted:
        # On a config line (:func:`config_value_end`) behind a strong label the
        # value is a literal, always Secret: only an exact variable reference
        # names it instead. A key ID, account ID or ARN keeps its Identifier tier.
        if (
            not value
            or is_tag(value)
            or _DIGEST_MASK_RE.fullmatch(value) is not None
            or _CONFIG_REFERENCE_RE.fullmatch(value) is not None
        ):
            return False
        return aws or value.isdigit() or not is_naming_value(value)
    if quoted and weak and _UNICODE_ESCAPES_RE.fullmatch(value):
        # A literal written only as ``\\uXXXX`` escapes (``'\\u7eff\\u706f'``) is
        # judged by the characters it spells.
        value = value.encode("ascii").decode("unicode_escape")
    pin = len(value) >= MIN_PIN_LEN and value.isdigit()
    if quoted and weak and not pin and (len(value) < MIN_VALUE_LEN or _is_word(value)):
        # A quoted value behind a weak label keeps ONLY the length and word
        # tests (``{"pwd": "deny"}``); no path or dotted-name test applies.
        return False
    if is_exempt_value(value, aws=aws, quoted=quoted, weak=weak):
        return False
    if not aws and not value.isdigit() and is_naming_value(value):
        return False
    return quoted or next_char not in ("(", "[")
