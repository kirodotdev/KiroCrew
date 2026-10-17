"""Shell text reader: escape and quote-literal decoding.

Decodes the escape forms a shell or a printer computes before the program sees its
argv: ``printf`` / ``echo -e`` escapes, ANSI-C (``$'...'``) and locale (``$"..."``)
quoting, and the numeric escape widths bash reads.

Part of :mod:`kiro_crew.security.shell_normalizer`, which re-exports every name a
caller imports from it; it imports nothing from that module. New escape forms belong
here.
"""

from __future__ import annotations

import re

_PRINTF_ESCAPES = (("\\n", " "), ("\\t", " "), ("\\r", " "), ("\\v", " "), ("\\f", " "))


# ``printf`` / ``$'…'`` numeric escapes: octal (``\\NNN``, ``\\0NNN``), hex
# (``\\xHH``) and Unicode (``\\uHHHH``, ``\\UHHHHHHHH``).
#
# The Unicode widths are EXACT and CASE-SENSITIVE, as bash defines them: ``\\u``
# consumes at most 4 hex digits and ``\\U`` at most 8, so ``$'\\u0072f'`` is ``r``
# followed by a literal ``f`` -- NOT a 5-digit code point.  Reading more digits than
# the spelling allows is a bypass, because the wrong character replaces the two the
# shell actually passes.  The pattern therefore carries no ``re.IGNORECASE`` (which
# would conflate the two widths) and spells its own classes; ``\\x`` keeps accepting
# either case, as it always did.
#
# This requires the caller to have preserved case: see ``_deny_segment_views``,
# which decodes BEFORE lowercasing for exactly this reason.
_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(?:[xX]([0-9a-fA-F]{1,2})"
    r"|u([0-9a-fA-F]{1,4})"
    r"|U([0-9a-fA-F]{1,8})"
    r"|0?([0-7]{1,3}))"
)


# ANSI-C quoting (``$'...'``) spells octal as ``\nnn`` -- one to three octal digits
# TOTAL, where a leading zero is simply one of the three.  The ``\0nnn`` form above
# (zero plus up to three more digits) belongs to ``echo -e``/``printf %b`` ONLY;
# sharing that pattern here consumed a fourth digit, so ``$'\06777'`` -- which bash
# reads as ``\067`` ('7') followed by the literal ``77``, i.e. ``777`` -- decoded to
# a single non-ASCII byte and the deny view diverged from what the shell runs.
# Group order matches _NUMERIC_ESCAPE_RE so
# ``_numeric_escape_code`` reads either match.
_ANSI_C_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(?:[xX]([0-9a-fA-F]{1,2})" r"|u([0-9a-fA-F]{1,4})" r"|U([0-9a-fA-F]{1,8})" r"|([0-7]{1,3}))"
)


def _escape_code_is_inert(code: int) -> bool:
    """True for a code point that must be left ENCODED rather than decoded.

    A NUL cannot appear in an argv the shell builds.  A LONE SURROGATE is refused
    for a second reason: it is not a character bash can pass either, and a decoded
    one would travel into the SEL audit record, whose JSON encoder raises on it --
    turning a denial into a crash.
    """
    return code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF


def _numeric_escape_code(match: "re.Match[str]") -> "int | None":
    """The code point an octal / hex / Unicode escape resolves to, or None.

    Split out so :func:`_numeric_escape_char` and :func:`_decode_ansi_c_body` cannot
    disagree about what a match means -- the body decoder has to recognise a NUL to
    truncate at it, and re-deriving that separately is how two code paths drift.

    Octal is masked to ONE BYTE, which is bash's semantics and was measured rather
    than assumed: ``$'\\555'`` is ``m`` (0o555 & 0xFF == 0x6D), ``$'\\777'`` is
    0xFF, and ``$'\\400'`` masks to a NUL.  Converting the full value instead gave
    ``$'r\\555'`` the character ``u``-breve where bash passes ``rm``, so
    ``$'r\\555' -rf /`` ran while the view matched nothing.
    """
    hex_digits, u4_digits, u8_digits, octal_digits = match.groups()
    digits = hex_digits or u4_digits or u8_digits
    try:
        return int(digits, 16) if digits else (int(octal_digits, 8) & 0xFF)
    except (TypeError, ValueError):  # pragma: no cover - the pattern admits only digits
        return None


def _numeric_escape_char(match: "re.Match[str]") -> str:
    """One decoded character for an octal, hex or Unicode escape."""
    code = _numeric_escape_code(match)
    if code is None or _escape_code_is_inert(code):
        return match.group(0)
    return chr(code)


def _decode_printf_escapes(text: str) -> str:
    """Turn literal ``\\n``-style escapes into whitespace.

    ``printf 'kirocrew token\\n' | bash`` carries the newline as two characters, so
    re-tokenizing the payload glues the escape onto the verb and the comparison misses.
    ``printf`` (and ``echo -e``) expand these before the shell sees them, so the payload
    is decoded the same way first.

    Numeric escapes are decoded too, not just the named ones: ``\\040`` and ``\\x20`` are
    both a SPACE, so leaving them literal reopens exactly the separator gap the named
    escapes closed, and ``\\x6b`` can spell a character of the program name itself.  What
    the shell will actually run is the decoded text, so the comparison is made against
    that.

    The Unicode forms (``\\uHHHH``, ``\\UHHHHHHHH``) are decoded for the same reason and
    were missing: ``$'\\u0074\\u006f\\u006b\\u0065\\u006e'`` is the same word as the
    ``\\x``-spelled form this already caught, so the argv-structural floors compared
    against an encoded string and a spelling of a self-protection verb slipped past
    while its hex twin was refused.
    """
    for esc, sub in _PRINTF_ESCAPES:
        text = text.replace(esc, sub)
    return _NUMERIC_ESCAPE_RE.sub(_numeric_escape_char, text)


# ANSI-C (``$'…'``) and locale (``$"…"``) quoting.  Both are QUOTING forms whose
# value the shell computes before the program sees it, so they are resolved as part
# of tokenization.  Matched on the RAW text rather than after ``shlex``, which is
# what makes it safe: ``shlex`` removes the quotes but leaves the ``$`` glued to the
# content, and at that point ``$'/'`` -> ``$/`` is indistinguishable from a variable
# reference like ``$HOME``, so stripping the ``$`` post-hoc would eat real variables.
# Requiring the quote character here means a bare ``$HOME`` never matches.
#
# The negated classes EXCLUDE the backslash, and that is a ReDoS fix, not a style
# choice.  With ``[^']`` a backslash could match either alternative -- ``\\.`` (two
# characters) or the class (one) -- the textbook ambiguous quoted-string pattern, so
# an unterminated ``$'`` followed by a run of backslashes forces the engine through
# ~1.618**n tilings of that run.  This regex runs inside the PreToolUse gate on the
# full, uncapped command, so that is a hang, not a slowdown (measured: 9 ms at 24
# backslashes, growing ~1.6x per character).  Excluding the backslash makes the
# alternation unambiguous -- a backslash is always consumed by ``\\.`` -- while
# accepting exactly the same language.
_ANSI_C_QUOTE_RE = re.compile(r"\$'((?:\\.|[^'\\])*)'|\$\"((?:\\.|[^\"\\])*)\"", re.DOTALL)

# Single-character ANSI-C escapes that stand for a LITERAL character.  These are
# the ones bash resolves and a matcher must therefore see resolved: without them
# ``$'rm -rf \"/\"'`` keeps its backslashes and the rule does not match, while bash
# passes the plain quotes.
_ANSI_C_LITERAL_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "?": "?"}
# Escapes that stand for a control character.  Mapped to a SPACE rather than the
# character itself, which is what ``_decode_printf_escapes`` has always done for
# this family: the value of resolving them here is that a token boundary appears
# where the shell puts one, and a literal control byte in a matched view would
# only travel into the audit record.  Deliberate, and the reason this decoder is
# not simply "what bash produces".
_ANSI_C_SPACE_ESCAPES = frozenset("abefnrtvE")


def _decode_ansi_c_body(body: str) -> str:
    """Resolve the escapes inside one ``$'…'`` body, in a SINGLE left-to-right pass.

    One pass is the whole point.  Sequential ``str.replace`` calls let one
    substitution's OUTPUT be re-read as another's input: ``$'\\\\n'`` is an escaped
    backslash followed by the letter ``n`` (two characters), but a chain that
    resolves ``\\\\`` first and then looks for ``\\n`` collapses it to whitespace and
    invents a separator bash never passed.  Consuming each escape atomically here
    makes that impossible -- the same failure mode as the Unicode-width guess this
    file already carries a note about.

    Numeric forms keep bash's exact widths (``\\xHH``, ``\\nnn`` octal, ``\\uHHHH``,
    ``\\UHHHHHHHH``) and the inert guard, so a NUL or lone surrogate stays encoded.
    An unrecognised escape keeps both characters, as bash does.
    """
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_C_LITERAL_ESCAPES:
            out.append(_ANSI_C_LITERAL_ESCAPES[nxt])
            i += 2
            continue
        if nxt in _ANSI_C_SPACE_ESCAPES:
            out.append(" ")
            i += 2
            continue
        if nxt == "c" and i + 2 < n and body[i + 2].isascii():
            # ``\cX`` is a CONTROL character, and ``\cI`` is a TAB -- so
            # ``bash -c $'rm\\cI-rf /'`` hands the inner shell a tab-separated
            # ``rm -rf /`` and it runs (measured,
            # the inner shell does split on it).  The mapping is MEASURED rather
            # than derived: ``ord(upper(X)) & 0x1F``, with ``?`` special-cased to
            # 0x7F -- an XOR-0x40 guess gets ``\\c0`` wrong (bash gives 0x10, not
            # ``p``).  The result is always a control character, so it takes the
            # same normalization to a SPACE as the named family above, which is
            # what puts a token boundary where the shell puts one.
            #
            # Restricted to a single ASCII character, because ``str.upper()`` is
            # not length-preserving outside it: ``"ß".upper()`` is ``"SS"``, and
            # ``ord`` of that raised ``TypeError`` straight out of the permission
            # gate on ``echo $'\\cß'`` -- a crash where a security decision belongs
            # (also BLOCKING, same lane).  A non-ASCII target falls through to the
            # unrecognised branch and keeps both characters, as bash does for a
            # spelling it does not define.
            target = body[i + 2]
            code = 0x7F if target == "?" else (ord(target.upper()) & 0x1F)
            if code == 0:
                # ``\c@`` is a NUL, and bash TRUNCATES the word there -- see the
                # numeric branch below for the measurement.
                return "".join(out)
            out.append(" ")
            i += 3
            continue
        match = _ANSI_C_NUMERIC_ESCAPE_RE.match(body, i)
        if match:
            # A NUL TRUNCATES the word -- bash cannot place one in an argv, and what
            # it does instead is stop there.  Measured on every spelling that can
            # reach zero: ``$'AA\\0junk'``, ``$'AA\\400junk'``, ``$'AA\\x00junk'``,
            # ``$'AA\\u0000j'`` and ``$'AA\\c@junk'`` all yield ``AA``, and
            # ``$'\\0AA'`` yields the empty word.  Leaving the escape encoded instead
            # was a bypass: ``$'dd\\0junk' if=/dev/zero of=/dev/sda`` ran the
            # destructive command while the view held ``dd\\0junk if=`` and matched
            # nothing.  The OTHER inert codes -- out
            # of range, lone surrogate -- keep the escape rather than truncating,
            # because bash does not produce them at all and guessing what it would do
            # is what the measurements above exist to avoid.
            numeric_code = _numeric_escape_code(match)
            if numeric_code == 0:
                return "".join(out)
            out.append(_numeric_escape_char(match))
            i = match.end()
            continue
        # Unrecognised: bash keeps the backslash and the character.
        out.append(body[i : i + 2])
        i += 2
    return "".join(out)
