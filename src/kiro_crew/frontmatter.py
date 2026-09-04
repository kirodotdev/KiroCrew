"""Single home for hand-rolled SKILL.md frontmatter parsing.

Four backend callers parse ``key: value`` frontmatter from markdown, and each
historically carried its own copy of the scanner. The copies drifted: they
disagree on whether the opening fence may carry trailing text or leading
whitespace, whether an indented ``key: value`` line is a field or prose,
whether surrounding quotes are stripped from values, which of two duplicate
keys wins, and whether a YAML block-scalar value is resolved from its
indented continuation lines. Those disagreements are load-bearing — the
skills loader MUST keep rejecting indented keys (an indented occurrence
belongs to a block scalar, and honoring it once broke
``set_inject_on_trigger``), while the onboarding import screen MUST keep its
leniency (it is a fail-closed gate, and narrowing what it reads as
``always:``/``triggers:`` would turn refusals into acceptances). So the
scanner logic lives here exactly once, and each caller names its accepted
grammar explicitly via a :class:`FrontmatterDialect`. The skill-provider
preview (``dashboard/handlers/discover.py``) deliberately shares
:data:`SKILL_LOADER` rather than owning a dialect: what the preview shows
must match what the skills loader computes after install.

A FIFTH consumer lives outside Python and cannot be reached from this list by
import: the skill editor's frontmatter splicer
(``website/src/components/SkillForm.tsx``) MIRRORS :data:`SKILL_LOADER`'s
grammar in TypeScript, because it must never write a value this reader would
decode differently than it wrote it. ``readerCannotDecode`` and
``backendReadsValue`` there encode this dialect's quirks that matter to a
writer -- quote characters are stripped off both ends rather than unquoted, and
nothing is unescaped -- and its refusal rules are derived from them. That
mirror was measured against this module, not inferred, but nothing in the
build enforces it: change the quote handling, the block-scalar folding, or the
unescaping here and the editor will keep writing for the old dialect, which is
silent corruption of a user's skill file. Update that file in the same change,
and see ``test_frontmatter.py``'s corpus test for the pin that now fails when
a repo-tracked SKILL.md starts reading differently.

BLOCK SCALARS follow YAML: the full header grammar (explicit indentation
indicator, either modifier ordering, a trailing comment) is resolved by
:func:`parse_block_scalar_header`, and :func:`fold_block_scalar` honours real
chomping. That half of the dialect IS a YAML subset and is pinned differentially
against ``yaml`` in the tests.

PLAIN SCALARS are deliberately NOT YAML, and this is not a gap waiting to be
closed. This reader splits a field on its first colon and keeps the remainder
verbatim, so it accepts a value a YAML parser REFUSES OUTRIGHT -- an unquoted
``": "`` inside the text::

    description: Three capture backends: playwright-cli (the session ...)

A YAML parser rejects that document whole ("mapping values are not allowed
here"), not just the field. Two skills this repo SHIPS are written that way and
are read correctly only because of it:

- ``builtin_skills/kirocrew-dev/prepare-pr/SKILL.md``
- ``builtin_skills/web-verify/SKILL.md``

So the two accepted-input surfaces CROSS rather than nest, and swapping this
scanner for a YAML parse is not a strict improvement: measured over the 56
fenced repo-tracked SKILL.md files it would break those two outright. The
quoting and unescaping rows of #7097 are tracked separately in #7063.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Any valid YAML block-scalar HEADER, matched everywhere one is recognized.
#
# There used to be a second recognizer beside this one -- a frozenset of the six
# BARE indicators (``>``, ``|``, ``>-``, ``|-``, ``>+``, ``|+``) -- and the module
# disagreed with itself about what a block scalar is: the read path tested set
# membership, the write path matched this regex, and the onboarding activation gate
# imported the set. Widening one recognizer and not the others is what turned that
# gate fail-OPEN (see ``_column0_activation_declared``). One matcher now, so a
# change to the grammar reaches every caller at once.
#
# The two halves of the alternation are YAML's two orderings of the optional
# indentation indicator and chomping modifier (``|2-`` and ``|-2``), and the
# second matches empty, which is the bare ``|`` case. A trailing ``# comment``
# is allowed because YAML allows one there: it belongs to the header line, not
# to the value.
_BLOCK_SCALAR_HEADER_RE = re.compile(
    r"^(?P<style>[|>])"
    r"(?:(?P<indent_first>[0-9])(?P<chomp_last>[+-]?)"
    r"|(?P<chomp_first>[+-]?)(?P<indent_last>[0-9]?))"
    r"(?:[ \t]+#.*)?$"
)


def parse_block_scalar_header(value: str) -> tuple[str, int | None] | None:
    """Split a block-scalar header into ``(style_with_chomp, explicit_indent)``.

    Returns ``None`` when *value* is not a block-scalar header, so a caller can
    treat it as an ordinary plain scalar. ``explicit_indent`` is the declared
    indentation indicator, or ``None`` when the header omits it and the content
    indentation is inferred from the first non-blank line.

    An explicit ``0`` is refused (``None``): YAML forbids it, and accepting it
    would hand :func:`fold_block_scalar` a zero-width indent that silently
    turns the whole block into content.
    """
    match = _BLOCK_SCALAR_HEADER_RE.match(value)
    if match is None:
        return None
    digits = match.group("indent_first") or match.group("indent_last") or ""
    if digits == "0":
        return None
    chomp = match.group("chomp_first") or match.group("chomp_last") or ""
    return match.group("style") + chomp, (int(digits) if digits else None)


# Fence extraction for the "column0_fence" dialect: the opener must be exactly
# ``---`` at position 0 followed by a newline; the closer is the next line
# that *starts with* ``---`` (trailing text after the closer is tolerated).
# An optional CR before each fence newline is tolerated. Most SKILL.md reads
# fold CRLF to LF before parsing (``Path.read_text``'s universal newlines,
# ``skills._decode_skill_text``), but the grammar must not depend on callers
# pre-folding: text reaches it VERBATIM too — the Agent SOP description
# reader decodes raw bytes, so a Windows-authored document showed no
# description at all — and through :func:`set_frontmatter_fields` a fence
# this pattern cannot see makes a field edit PREPEND a brand-new block above
# the existing one instead of rewriting it. Interior lines may still carry a
# trailing ``\r``; the line parser strips it, so a resolved value is equal to
# its LF twin's — the same break normalisation a YAML reader applies.
_COLUMN0_BLOCK_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.DOTALL)

# As above, and ADDITIONALLY matching the EMPTY block. The inner group is
# OPTIONAL: an opener immediately followed by a closer (``---\n---``) has no
# line between them for ``(.*?)\n`` to consume, so the non-optional form
# never matched it at all — the extractor reported "no fence", and a mode
# edit then PREPENDED a brand-new fence in front of the empty one instead of
# populating it, doubling the block. A match with the group absent (``None``,
# distinct from the empty string a real-but-blank line would capture) is
# exactly that zero-line case. Whether an empty block counts as a fence is a
# per-dialect accepted-input decision — steering opts in, the skill dialects
# keep their non-optional group — which is why this stays a separate pattern.
_COLUMN0_BLOCK_CRLF_RE = re.compile(r"^---\r?\n(?:(.*?)\r?\n)?---", re.DOTALL)

# Fence extraction for the "leading_ws_fence" dialect: as "column0_fence"
# (CR tolerance included), except any whitespace (including blank lines) may
# precede the opening fence.
_LEADING_WS_BLOCK_RE = re.compile(r"^\s*---\r?\n(.*?)\r?\n---", re.DOTALL)

# How the frontmatter block is located within the document. Each mode is the
# fence grammar of the dialects that name it; they are not interchangeable.
Extraction = Literal["column0_fence", "column0_fence_crlf", "line_scan", "leading_ws_fence"]

# Whether an indented ``key: value`` line is a field or prose.
# - "reject_indented": any leading whitespace (space or tab) makes the line
#   prose. The skills loader depends on this: an indented occurrence belongs
#   to the enclosing block scalar, not the frontmatter.
# - "accept_indented": indentation is ignored; the key is stripped and used.
IndentPolicy = Literal["reject_indented", "accept_indented"]


@dataclass(frozen=True)
class FrontmatterDialect:
    """One caller's accepted frontmatter grammar, named explicitly.

    A dialect is a contract, not a tuning knob: changing a field changes what
    an existing caller accepts or rejects, which is a behavior change with its
    own review surface (see the snapshot corpus in ``test/test_frontmatter.py``).
    """

    extraction: Extraction
    indent_policy: IndentPolicy
    # Strip surrounding double/single quote characters from plain values.
    # This is str.strip("\"'"), i.e. it removes *runs* of quote characters
    # from both ends and tolerates mismatched pairs — preserved from the
    # originals. Never applied to a resolved block scalar.
    strip_quotes: bool
    # True: the first occurrence of a duplicate key wins (single-value lookup
    # semantics). False: the last occurrence wins (dict-overwrite semantics).
    first_key_wins: bool = False
    # Resolve a block-scalar header value (see
    # :func:`parse_block_scalar_header`) from the blank-or-indented lines that
    # follow it, via fold_block_scalar. Dialects without this store the header
    # character verbatim and leave the continuation lines to the indent
    # policy.
    resolve_block_scalars: bool = False
    # Read a trailing ``# ...`` as a COMMENT rather than as part of the value,
    # the way YAML does. Opt-in per dialect because it CHANGES WHAT A CALLER
    # ACCEPTS: a skills author who wrote ``name: a # b`` today gets the whole
    # string, and silently shortening that is the kind of drift this file's
    # dialects exist to prevent. Steering opts in because its document is read
    # back by kiro-cli as real YAML, so a reader that disagrees makes the tab
    # report a mode the agent never sees.
    strip_inline_comments: bool = False


# ``SkillsLoader._parse_frontmatter`` — the skills catalog reader. Strict
# opener, indented lines are prose, quotes stripped from plain values, block
# scalars resolved, last duplicate wins.
SKILL_LOADER = FrontmatterDialect(
    extraction="column0_fence",
    indent_policy="reject_indented",
    strip_quotes=True,
    resolve_block_scalars=True,
)

# ``dashboard/handlers/steering._head_meta`` — the Steering tab's listing scan.
# Steering documents are the same markdown-with-front-matter family as SKILL.md,
# so they accept the same grammar; it is a SEPARATE constant rather than a second
# reference to ``SKILL_LOADER`` because the two document families have no reason
# to move together — retuning what the skills catalog accepts must not silently
# change how a steering document's declared mode is read.
STEERING_LOADER = FrontmatterDialect(
    extraction="column0_fence_crlf",
    indent_policy="reject_indented",
    strip_quotes=True,
    resolve_block_scalars=True,
    strip_inline_comments=True,
)

# ``onboarding_import._frontmatter`` — the import screen's collapsed map.
# Lenient on the opener (trailing text tolerated), the closer indentation,
# and indented keys; quotes stripped; no block-scalar resolution. The
# activation DECISION does not ride on this map alone:
# ``onboarding_import._column0_activation_declared`` mirrors the loader's
# region and key rules separately, precisely because this grammar diverges
# from the loader's (indented prose can overwrite a real value here, and a
# ``---``-prefixed closer line does not close this fence).
ONBOARDING_IMPORT = FrontmatterDialect(
    extraction="line_scan",
    indent_policy="accept_indented",
    strip_quotes=True,
)

# ``history._frontmatter_value`` — single-key lookup used by the skill-update
# merge. Tolerates leading whitespace before the opener; otherwise mirrors
# the loader's field rules (column-0 keys only, block scalars resolved) so a
# value survives the read-stage-approve round-trip, but keeps plain values
# verbatim (no quote stripping) and returns the first duplicate.
SKILL_UPDATE = FrontmatterDialect(
    extraction="leading_ws_fence",
    indent_policy="reject_indented",
    strip_quotes=False,
    first_key_wins=True,
    resolve_block_scalars=True,
)


def fold_block_scalar(
    indicator: str,
    block: list[str],
    *,
    indent: int | None = None,
) -> str:
    """Resolve a YAML block scalar's indented lines into a single value.

    ``indicator`` is a block-scalar header's style and chomping modifier (``|``,
    ``>-``, ``|+``, ...); ``block`` holds the raw continuation lines, still
    carrying their indentation. ``indent`` is the header's explicit indentation
    indicator when it declared one, and ``None`` when the content indentation is
    inferred from the first non-blank line.

    Literal (``|``) scalars keep one line per newline. Folded (``>``) scalars
    fold a single break between plain lines to a space and keep blank lines as
    newlines (k blanks -> k newlines plain-to-plain, k+1 next to a more-indented
    line, where the separator break stays literal), and never fold a break
    adjacent to a more-indented line, so nested indentation survives.

    Chomping follows YAML, and is the reason this function does not trim its
    result: ``-`` (strip) drops every trailing break, ``+`` (keep) preserves all
    of them, and the default (clip) keeps exactly one. A LEADING break is
    content under all three -- no chomping mode removes it -- so it is preserved
    too. The previous ``.strip()`` did both, which is what made agreement with a
    YAML parser depend on a block's content rather than on its header, and
    therefore what forced the skill editor to simulate this function and compare
    instead of reading the header. See #7097.

    Note that EVERY line the frontmatter fence hands over is newline-terminated in
    the document: the fence's closing ``---`` sits on its own line, so the newline
    the extractor drops before it is the last content line's own terminator. The
    break count therefore always includes it. Feeding a YAML parser the captured
    text WITHOUT that newline is what makes the two look like they disagree here --
    reconstitute it and they do not.
    """
    style = indicator[:1]
    chomp = indicator[1:2] if indicator[1:2] in ("+", "-") else ""
    all_breaks = "\n" * len(block) if chomp == "+" else ""
    if indent is None:
        # Without an explicit indicator the block's indentation IS its first content
        # line's, so a block with no content line has no indent to derive from and
        # every line really is a break. Measured against the parser: ``|+`` over one
        # blank line is one break, over two is two.
        #
        # Indentation in YAML is SPACES, never tabs, so "is this line content?" is
        # decided with ``lstrip(" ")`` rather than ``strip()``: under ``|`` a line of two
        # spaces then a tab is two columns of indent followed by a TAB OF CONTENT, and
        # a parser reads ``"\t"``. Judging it with ``strip()`` counted the tab as
        # indentation, found no content line at all, and returned "" for the scalar.
        first = next((ln for ln in block if ln.lstrip(" ")), None)
        if first is None:
            return all_breaks
        indent = len(first) - len(first.lstrip(" "))
    dedented = [
        ln[indent:] if ln[:indent].isspace() or not ln.strip() else ln.lstrip() for ln in block
    ]
    if not any(dedented):
        # An EXPLICIT indicator can still leave nothing behind, and deciding that
        # BEFORE the dedent is what erased authored whitespace: under ``|2-`` a line of
        # three spaces is one space of CONTENT, not an empty line, because the header
        # -- not the content -- fixes where the block starts. Judged on the raw line it
        # looked blank and the whole scalar came back "". This is the same
        # whitespace-is-content rule the trailing-break classifier below already
        # applies; this early exit simply predates it.
        return all_breaks
    # Classify the trailing breaks AFTER dedenting, not before. A line holding only
    # whitespace is not necessarily blank: whitespace BEYOND the block's indent is
    # content, so ``|-`` over ``  body`` then three spaces is ``body\n `` to a YAML
    # parser and was ``body`` here -- authored content silently dropped. Judged on the
    # raw line, ``"   ".strip()`` is empty and the line looks like a break; judged after
    # dedent it is the one-space content line it actually is. A whitespace-only line at
    # or inside the indent still dedents to ``""`` and is still a break.
    end = len(dedented)
    while end and not dedented[end - 1]:
        end -= 1
    # The +1 is the last line's own terminator, per the note above.
    trailing_breaks = len(dedented) - end + 1
    content = dedented[:end]
    if chomp == "-":
        suffix = ""
    elif chomp == "+":
        suffix = "\n" * trailing_breaks
    else:
        # Clip keeps exactly one break, and there is always one to keep: the
        # count includes the last line's own terminator, so it is never zero.
        suffix = "\n"
    if style == "|":
        return "\n".join(content) + suffix
    # Folded: a single line break between two plain lines becomes a space;
    # blank lines are preserved as line breaks (k blanks -> k newlines); and
    # breaks adjacent to a more-indented line are kept, so indented structure
    # (nested lists, code-ish content) survives the fold.
    parts: list[str] = []
    pending_blanks = 0
    prev_more_indented = False
    started = False
    for ln in content:
        # Judged on the DEDENTED line, an empty string is a blank and a whitespace-only
        # line is not: the whitespace it still holds sits beyond the block's indent, so
        # it is content, and a more-indented line at that. Testing ``.strip()`` here
        # folded such a line away entirely.
        if not ln:
            pending_blanks += 1
            continue
        more_indented = ln[:1].isspace()
        if started:
            if pending_blanks:
                # The separator break folds to nothing between plain lines,
                # but stays literal next to a more-indented line: k blanks
                # yield k newlines plain-to-plain, k+1 otherwise.
                extra = 1 if (more_indented or prev_more_indented) else 0
                parts.append("\n" * (pending_blanks + extra))
            elif more_indented or prev_more_indented:
                parts.append("\n")
            else:
                parts.append(" ")
        elif pending_blanks:
            # Leading blank lines are content, not a separator: they precede the
            # first line rather than joining two, so they are emitted verbatim.
            parts.append("\n" * pending_blanks)
        # The dedent above already removed the block's indentation, so a plain line
        # arrives flush and a more-indented one keeps only its extra depth -- there is
        # nothing left to trim on the left. Nothing is trimmed on the RIGHT either: a
        # folded scalar's trailing spaces are content, and the break folds to a space
        # AFTER them, so ``>`` over ``a  `` then ``b`` is ``a   b`` to a YAML parser.
        # Stripping here silently deleted whatever the author wrote at the end of every
        # folded line.
        parts.append(ln)
        prev_more_indented = more_indented
        pending_blanks = 0
        started = True
    return "".join(parts) + suffix


def parse_frontmatter(text: str, dialect: FrontmatterDialect) -> dict[str, str]:
    """Parse frontmatter fields from *text* under *dialect*; ``{}`` if absent."""
    # Field-only path: skip the body slice entirely — the skills catalog
    # calls this per SKILL.md on load, and the body would be a dead
    # allocation the size of the document.
    lines, _ = _extract_block(text, dialect.extraction, want_body=False)
    if lines is None:
        return {}
    return _parse_block_lines(lines, dialect)


def frontmatter_value(text: str, key: str, dialect: FrontmatterDialect) -> str:
    """Return one frontmatter field's value, or ``""`` when absent."""
    return parse_frontmatter(text, dialect).get(key, "")


def split_frontmatter(text: str, dialect: FrontmatterDialect) -> tuple[dict[str, str], str]:
    """Parse frontmatter and split off the body under *dialect*.

    Returns ``(fields, body)``. When no complete frontmatter block is found
    the fields are ``{}`` and the body is *text* unchanged. The body is only
    contractually meaningful for the ``line_scan`` extraction (the one caller
    that consumes it: the stripped text after the closing fence line). The
    other modes return an arbitrary unconsumed remainder — ``column0_fence``
    and ``leading_ws_fence`` cut immediately after the ``---`` closer token
    (mid-line when the closer carries trailing text) — so do not render it
    as a document body.
    """
    lines, body = _extract_block(text, dialect.extraction)
    if lines is None:
        return {}, body
    return _parse_block_lines(lines, dialect), body


def _first_newline_is_crlf(text: str) -> bool:
    """Does *text* use CRLF? Judged on its FIRST line break, not on any of them.

    A document with mixed endings is already inconsistent; the first break is
    what its author's editor produced, so matching it is the least surprising
    choice and keeps a pure-CRLF file pure.
    """
    index = text.find("\n")
    return index > 0 and text[index - 1] == "\r"


# A value this writer cannot spell so that BOTH readers agree on it. A
# double-quoted YAML scalar processes escape sequences; this module's reader
# understands none of them (its ``strip_quotes`` is ``str.strip("\"'")``, which
# removes RUNS of quote characters and nothing else). So a value carrying a
# double quote or a backslash, or sitting against a single quote at either end,
# has no representation the two agree on and is refused rather than mangled.
_UNSPELLABLE_CHARS = ('"', "\\")

# Characters a YAML reader either REFUSES outright or reads as a line break, so
# neither survives being written into a scalar. Quoting does not help: this
# writer emits no escape sequences (see above), so the byte goes in raw.
#
# Both halves matter, and the second is the worse one. A C0 control, ``DEL`` or a
# C1 control makes the whole document unloadable — loud, and the author sees it.
# ``NEL`` (U+0085) and the line/paragraph separators are line breaks to a YAML
# reader, so the document still parses and the pattern silently comes back as
# something the author never wrote. TAB is the one control YAML allows; LF and CR
# are refused above, with their own message, because here they would close the
# fence rather than merely be illegal.
#
# The surrogate range is here for a third reason: a lone surrogate is not
# encodable as UTF-8 at all, so it never reaches a YAML reader — it raises
# ``UnicodeEncodeError`` at the first ``.encode()`` on the write path and turns a
# malformed request into a 500. JSON hands one over willingly (``"\ud800"``), so
# the API can be given one even though no editor can type it.
_YAML_UNWRITABLE_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")

# Values safe to emit BARE. An allowlist, not a denylist: what a bare YAML scalar
# means is decided by the reader's resolver, so enumerating the dangerous spellings
# is a game this module loses every time a YAML version retypes another plain
# scalar. A letter-led word of letters, digits, ``_`` and ``-`` is a plain string
# under every resolver — which is exactly the closed mode vocabulary
# (``always``/``fileMatch``/``manual``/``auto``), so a mode stays unquoted and the
# author's document is not churned. Everything else, globs included, is quoted.
_PLAIN_SAFE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")

# ...except the words a YAML resolver reads as a bool or null even bare-word-shaped.
_YAML_KEYWORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "y", "n"})


def _render_frontmatter_value(value: str) -> str:
    """Render *value* as a single-line frontmatter value, quoting unless provably safe.

    The set of values safe BARE is not the set this module's own reader would
    accept. The document is written for kiro-cli, which loads it as real YAML, and
    there a bare scalar is retyped and re-cut by rules a line-wise reader never
    sees:

    - ``true``, ``no``, ``on``, ``null``, ``~``, ``123``, ``1.5`` resolve to a
      bool, ``None`` or a number — the value stops being a string at all;
    - ``src # old/*.ts`` is truncated at the ``#`` — the rest is a comment;
    - a leading ``*``, ``[``, ``{``, ``!``, ``&`` opens an alias, a flow
      collection, a tag or an anchor, and the whole document stops parsing.
      ``fileMatchPattern: *.ts`` is the most ordinary glob a user can type.

    So bare output is gated on :data:`_PLAIN_SAFE_RE` rather than on a list of
    known-bad spellings. Quoting a glob also matches how Kiro's own documentation
    writes patterns.

    Raises ``ValueError`` on a newline (a value carrying one would become extra
    frontmatter lines, or close the fence) and on a value this writer cannot
    escape; :func:`set_frontmatter_fields` maps that to a caller-visible refusal
    rather than writing a document neither reader can load.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("a frontmatter value may not contain a newline")
    if any(c in value for c in _UNSPELLABLE_CHARS):
        raise ValueError("a frontmatter value may not contain a double quote or a backslash")
    if _YAML_UNWRITABLE_RE.search(value):
        raise ValueError("a frontmatter value may not contain a control character")
    # ``strip_quotes`` eats runs of quote characters from BOTH ends, so a value
    # against a single quote would come back short of what was written.
    if value[:1] == "'" or value[-1:] == "'":
        raise ValueError("a frontmatter value may not begin or end with a single quote")
    if _PLAIN_SAFE_RE.fullmatch(value) and value.lower() not in _YAML_KEYWORDS:
        return value
    return '"' + value + '"'


def split_inline_comment(raw: str) -> tuple[str, str]:
    """Split a frontmatter value's text from its trailing ``#`` comment.

    Returns ``(value_text, comment)``; ``comment`` keeps the whitespace that
    preceded it so a rewritten line reproduces the original spacing, and is
    ``""`` when there is none.

    YAML starts a comment at a ``#`` PRECEDED BY WHITESPACE, so ``a#b`` is a
    value and ``a #b`` is a value plus a comment. Inside a quoted scalar the
    ``#`` is content, which is why the quoted form is scanned to its closing
    quote first.
    """
    body = raw.lstrip()
    lead = raw[: len(raw) - len(body)]
    if body[:1] in ('"', "'"):
        quote = body[0]
        end = body.find(quote, 1)
        if end != -1:
            rest = body[end + 1 :]
            cut = rest.find("#")
            if cut != -1 and (cut == 0 or rest[cut - 1].isspace()):
                return lead + body[: end + 1] + rest[:cut].rstrip(), rest[cut:].rjust(
                    len(rest[:cut]) - len(rest[:cut].rstrip()) + len(rest[cut:])
                )
            return raw, ""
    index = 0
    while True:
        cut = body.find("#", index)
        if cut == -1:
            return raw, ""
        # ``cut == 0`` means the value text is empty and the whitespace before
        # the ``#`` is the space after the colon — still a comment.
        if cut == 0 or body[cut - 1].isspace():
            value = body[:cut]
            return lead + value.rstrip(), value[len(value.rstrip()) :] + body[cut:]
        index = cut + 1


def set_frontmatter_fields(
    text: str, updates: dict[str, str | None], dialect: FrontmatterDialect
) -> str:
    """Return *text* with its frontmatter fields updated.

    *updates* maps key to new value; a ``None`` value REMOVES the key. Keys are
    written in the order given when they are new, and in place when they already
    exist, so a document's existing field order survives an edit.

    The BODY is preserved byte for byte. That is the whole point of doing this
    server-side rather than having an editor splice YAML into a textarea: the
    body is the user's document, and a rewrite that reflows it — or that eats a
    trailing newline — is data loss disguised as a settings change.

    Three shapes are handled:

    - No frontmatter yet → one is created above the existing text.
    - A block that ends up empty (every key removed) → the fence goes with it,
      rather than leaving a bare ``---`` the renderer would draw as a rule.
    - An existing key whose value is a YAML block scalar → its indented
      continuation lines are removed along with the key line, so replacing a
      folded value cannot orphan the fold.

    Only ``column0_fence``, ``column0_fence_crlf``, and ``leading_ws_fence`` are
    supported; ``line_scan`` does not identify its block precisely enough to
    rewrite it in place.
    """
    if dialect.extraction == "line_scan":
        raise ValueError("set_frontmatter_fields cannot rewrite a line_scan block")
    # Write the fence with the newline the DOCUMENT already uses. Emitting LF
    # into a CRLF file would leave it mixed — the same class of damage as
    # reflowing the body, and just as invisible in a diff viewer.
    nl = "\r\n" if _first_newline_is_crlf(text) else "\n"
    lines, body = _extract_block(text, dialect.extraction)
    if lines is None:
        additions = [
            f"{key}: {_render_frontmatter_value(value)}"
            for key, value in updates.items()
            if value is not None
        ]
        if not additions:
            return text
        _verify_round_trip(additions, updates, dialect)
        return f"---{nl}" + nl.join(additions) + f"{nl}---{nl}" + text

    remaining = dict(updates)
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        line = line.rstrip("\r")
        key = line.partition(":")[0].strip() if ":" in line else ""
        indented = line[:1].isspace()
        is_field = bool(key) and not (dialect.indent_policy == "reject_indented" and indented)
        # Consume a field's continuation lines with its key line, so a replaced
        # or removed value leaves nothing behind.
        #
        # This follows YAML's reading, not this module's. Under
        # ``reject_indented`` an indented line is PROSE to the parser above, so
        # ``inclusion:`` followed by an indented ``manual`` reads here as an
        # EMPTY inclusion — while a YAML reader folds the two into
        # ``inclusion: manual``. Leaving the orphan behind on a write turns a
        # new ``auto`` into ``auto manual`` for kiro-cli while this tab still
        # reports ``auto``: the document and the dashboard then disagree,
        # silently, about the mode its author declared.
        #
        # Where the two shapes END differs, so the walk does too. A block scalar
        # owns the blank lines inside it; a plain multi-line scalar is ended BY a
        # blank line, and consuming past one would swallow a separator belonging
        # to the document rather than to this field.
        continuation: list[str] = []
        if is_field:
            header = (
                parse_block_scalar_header(line.partition(":")[2].strip())
                if dialect.resolve_block_scalars
                else None
            )
            is_block = header is not None
            # The declared indentation indicator when the header carries one, so the
            # walk's boundary below matches the read path's for the same document.
            block_indent = header[1] if header is not None else None
            blank_floor = 0
            while index < len(lines):
                nxt = lines[index]
                # Judged on the CR-STRIPPED line, and in SPACES for a block scalar, so
                # this walk keeps agreeing with the read path: there, a line of spaces then
                # a tab is a content line whose content is the tab, and a CRLF blank line
                # is still a blank. If only one of the two paths counted the tab as
                # indentation, or saw ``"\r"`` as content, they would disagree about where
                # the block ends and a rewrite of an unrelated field would move the
                # author's lines.
                probe = nxt.rstrip("\r")
                blank = not probe.strip() if not is_block else not probe.lstrip(" ")
                if blank and not is_block:
                    # A blank line does NOT end a plain multi-line scalar: YAML keeps
                    # folding while an indented line follows, so `a\n\n  b` is one
                    # value. Breaking here left the tail attached to a REPLACED key —
                    # the stored mode then differed from the one the author picked.
                    # Look past the run of blanks and decide on what actually follows:
                    # a column-0 line ends the scalar, and an indented COMMENT is not
                    # part of it either (see the comment rule below).
                    ahead = index
                    while ahead < len(lines) and not lines[ahead].strip():
                        ahead += 1
                    continues = (
                        ahead < len(lines)
                        and lines[ahead][:1].isspace()
                        and not lines[ahead].lstrip().startswith("#")
                    )
                    if not continues:
                        break
                if not blank and not nxt[:1].isspace():
                    break
                # An indented ``#`` line is a COMMENT to YAML unless it is inside a
                # block scalar, where it is content. Consuming one would delete the
                # author's own note from their document on an unrelated mode edit —
                # a silent, permanent loss, and the reason this walk checks the shape
                # of each line rather than just its indentation.
                if not is_block and nxt.lstrip().startswith("#"):
                    break
                # Inside a block scalar, "indented" is not enough: the scalar ends at
                # the first non-blank line indented LESS than its content, so a
                # less-indented line is not the field's to consume. Without this the
                # walk stayed on the READ path's old rule and swallowed a
                # less-indented trailing comment when the block's own field was
                # replaced — the same silent note deletion the rule above prevents for
                # a plain value. The boundary is the declared indent when the header
                # gives one, and otherwise the first content line's own indent -- with a
                # leading all-space line's own depth as a floor on that, because YAML
                # detects the indent from the leading blanks too and ends the scalar
                # rather than accept a shallower content line. Kept identical to the read
                # path's walk: if only one of them applied the floor they would disagree
                # about where the block ends, and rewriting the field would delete a line
                # the reader had left in the surrounding document.
                if is_block and not blank:
                    nxt_indent = len(probe) - len(probe.lstrip(" "))
                    if block_indent is None:
                        if nxt_indent < blank_floor:
                            break
                        block_indent = nxt_indent
                    if nxt_indent < block_indent:
                        break
                elif is_block and block_indent is None:
                    blank_floor = max(blank_floor, len(probe))
                # Strip the CR here too: these lines are re-joined with the
                # document's newline, so a retained CR would be written back
                # doubled.
                continuation.append(nxt.rstrip("\r"))
                index += 1
        if is_field and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                # Carry the author's inline comment across the rewrite. It is
                # theirs, it is not part of the value to either reader, and a
                # mode edit silently deleting the rationale beside a declaration
                # is exactly the kind of quiet loss this writer must not cause.
                _, comment = split_inline_comment(line.partition(":")[2])
                out.append(f"{key}: {_render_frontmatter_value(value)}{comment}")
            # value is None → the key (and any fold) is dropped.
            continue
        out.append(line)
        out.extend(continuation)

    out.extend(
        f"{key}: {_render_frontmatter_value(value)}"
        for key, value in remaining.items()
        if value is not None
    )
    if not any(line.strip() for line in out):
        # An empty block: drop the fence, and with it the ONE newline that
        # separated it from the body. `lstrip` would eat every leading blank
        # line the document itself opens with — a silent reflow of text this
        # function promises to preserve byte for byte.
        if body.startswith("\r\n"):
            return body[2:]
        return body[1:] if body.startswith("\n") else body
    _verify_round_trip(out, updates, dialect)
    return f"---{nl}" + nl.join(out) + f"{nl}---" + body


def _verify_round_trip(
    block: list[str], updates: dict[str, str | None], dialect: FrontmatterDialect
) -> None:
    """Raise unless re-parsing *block* yields exactly what was written.

    The single-line grammar here has no escape sequence — ``strip_quotes`` is
    ``str.strip("\"'")``, so a value that itself ends in a quote character comes
    back shorter than it went in. Rather than mangle the caller's value, refuse
    it: every caller of this writer is serving a user edit, where a rejection is
    recoverable and a silent truncation is not.
    """
    parsed = _parse_block_lines(block, dialect)
    for key, value in updates.items():
        if value is None:
            if key in parsed:
                raise ValueError(f"frontmatter key {key!r} could not be removed")
        elif parsed.get(key) != value:
            raise ValueError(
                f"frontmatter value for {key!r} cannot be represented in this "
                f"document format (it would read back as {parsed.get(key)!r})"
            )


def _extract_block(
    text: str, extraction: Extraction, *, want_body: bool = True
) -> tuple[list[str] | None, str]:
    """Locate the frontmatter block; ``(None, text)`` when there isn't one.

    With ``want_body=False`` the second element is ``""`` whenever a block
    was found (the caller promises not to read it); the no-block case still
    returns *text* so ``({}, text)`` stays uniform.
    """
    if extraction in ("column0_fence", "column0_fence_crlf", "leading_ws_fence"):
        pattern = {
            "column0_fence": _COLUMN0_BLOCK_RE,
            "column0_fence_crlf": _COLUMN0_BLOCK_CRLF_RE,
            "leading_ws_fence": _LEADING_WS_BLOCK_RE,
        }[extraction]
        match = pattern.match(text)
        if not match:
            return None, text
        # ``group(1)`` is ``None`` only for the CRLF pattern's zero-line case
        # (see its comment) — distinct from "" (one real blank line) since
        # ``"".split("\n")`` is ``[""]``, one line, not zero.
        captured = match.group(1)
        lines = [] if captured is None else captured.split("\n")
        return lines, (text[match.end() :] if want_body else "")
    if extraction == "line_scan":
        if not text.startswith("---"):
            return None, text
        # splitlines() preserved from the original: the closer test's
        # .strip() already absorbs a trailing \r, so the visible differences
        # vs split("\n") are the wider line-boundary set (\r, \v, \f,
        # \x1c-\x1e, \x85, \u2028, \u2029 also split) and interior \r removal
        # in the joined body. The opener line itself is skipped, tolerating
        # trailing text.
        lines = text.splitlines()
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[index + 1 :]).strip() if want_body else ""
                return lines[1:index], body
        return None, text
    # A new Extraction literal must get its own branch: falling through to
    # any existing mode would silently hand it that mode's grammar.
    raise ValueError(f"unknown frontmatter extraction mode: {extraction!r}")


def _parse_block_lines(lines: list[str], dialect: FrontmatterDialect) -> dict[str, str]:
    """Scan block lines into a field dict under *dialect*'s line rules."""
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line:
            continue
        # line[:1] is "" for an empty line and "".isspace() is False, so the
        # guards only fire on genuinely indented lines.
        if dialect.indent_policy == "reject_indented" and line[:1].isspace():
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        value = raw.strip()
        header = parse_block_scalar_header(value) if dialect.resolve_block_scalars else None
        if header is not None:
            style_chomp, explicit_indent = header
            # Blank or indented lines are the scalar's content, but only DOWN TO the
            # block's indentation: YAML ends a block scalar at the first non-blank line
            # indented less than its content, so a less-indented ``#`` line is a comment
            # in the surrounding document, not part of the value. Collecting purely on
            # "is it indented" absorbed one -- ``|- `` + ``  body`` + `` # note`` read
            # back as ``body\n# note`` -- and the explicit-indicator support added here
            # would have widened that to every declared-indent block. The boundary is
            # the declared indent when the header gives one, and otherwise the first
            # non-blank line's own indent, which is what the folder infers too.
            #
            # Trailing blank lines are still collected: they are the block's trailing
            # breaks, and chomping is what decides their fate.
            #
            # "Blank" and "indented" are both judged in SPACES, never tabs, because that
            # is what YAML counts as indentation. A line of two spaces then a tab is a
            # content line whose content is the tab, so it is what fixes the block's
            # indent; judging it with ``strip()`` made it look blank, let a following
            # more-indented line set a deeper boundary, and silently truncated the
            # scalar at the next line that sat at the real indent.
            block: list[str] = []
            content_indent = explicit_indent
            # Leading BLANK lines take part in detecting the block's indentation: YAML
            # sets the content indent from the first non-empty line, but a leading
            # all-space line may not be deeper than that line, and a parser resolves the
            # conflict by ending the scalar rather than by taking the shallower line as
            # content. So a blank line's own depth is a floor. Without it, `description:
            # |` over three spaces then ` # note` read the note as CONTENT where a parser
            # reads an empty scalar and a document comment -- and the write path then
            # replaced the note away with the field.
            blank_floor = 0
            while i < len(lines):
                # Drop the trailing CR before classifying. A CRLF document's blank line is
                # ``"\r"``, which is not empty once "blank" is judged in SPACES, so it
                # counted as a content line at indent 0 and ENDED the scalar -- truncating
                # a steering description at its first interior blank, or losing it whole
                # when the blank came first. Stripping it also puts the value where a YAML
                # parser puts it: line breaks are normalised, so a CRLF document resolves
                # to exactly what its LF twin does, with no stray CR left inside.
                nxt = lines[i].rstrip("\r")
                if nxt.lstrip(" "):
                    nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                    if nxt_indent == 0:
                        break
                    if content_indent is None:
                        if nxt_indent < blank_floor:
                            break
                        content_indent = nxt_indent
                    if nxt_indent < content_indent:
                        break
                elif content_indent is None:
                    blank_floor = max(blank_floor, len(nxt))
                block.append(nxt)
                i += 1
            value = fold_block_scalar(style_chomp, block, indent=explicit_indent)
        else:
            if dialect.strip_inline_comments:
                value, _ = split_inline_comment(value)
                value = value.strip()
            if dialect.strip_quotes:
                value = value.strip("\"'")
        if dialect.first_key_wins and key in fields:
            continue
        fields[key] = value
    return fields
