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
``backendReadsValue`` there encode this dialect's two quirks that matter to a
writer -- quote characters are stripped off both ends rather than unquoted,
and nothing is unescaped -- and its refusal rules are derived from them. That
mirror was measured against this module, not inferred, but nothing in the
build enforces it: change the quote handling, the block-scalar folding, or the
unescaping here and the editor will keep writing for the old dialect, which is
silent corruption of a user's skill file. Update that file in the same change,
and see https://github.com/kirodotdev/KiroCrew/issues/7097 for the shared
fixture corpus that would turn this comment into a failing test.

This is deliberately NOT a YAML parser and must not grow into one: values are
single-line strings apart from the minimal block-scalar folding below, and no
YAML library is involved. Swapping the parsing technology would change every
caller's accepted-input surface at once and needs its own review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# YAML block-scalar indicators recognized as frontmatter values: folded (>) or
# literal (|), each with an optional chomping modifier. Explicit indentation
# indicators (e.g. ">2") are not supported by this minimal parser.
BLOCK_SCALAR_INDICATORS = frozenset({">", "|", ">-", "|-", ">+", "|+"})

# Any valid YAML block-scalar HEADER, including the explicit-indentation forms
# (``|2``, ``>2-``, ...) this module's READER still does not fold — see the
# limitation noted above. Used only on the WRITE path, to decide whether an
# indented tail belongs to the value being replaced. Without this, an explicit-
# indent scalar's `#`-shaped continuation line is mistaken for a real YAML
# comment (the ordinary rule a plain scalar's tail follows) and left orphaned
# in the document by a rewrite of an unrelated line above it — not invalid
# YAML, since a lone indented `#` still parses as a floating comment, but the
# author's content silently detaches from the field it was written under.
_BLOCK_SCALAR_HEADER_RE = re.compile(r"^[|>](?:[0-9][+-]?|[+-]?[0-9]?)$")

# Fence extraction for the "column0_fence" dialect: the opener must be exactly
# ``---`` at position 0 followed by a newline; the closer is the next line
# that *starts with* ``---`` (trailing text after the closer is tolerated).
_COLUMN0_BLOCK_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# As above, but tolerating CRLF. A steering file authored on Windows has
# ``---\r\n``, which the LF-only fence above does not match at all — so its
# declaration was invisible (the tab reported the default mode and the
# runtime skipped the document entirely) and an edit PREPENDED a second
# front-matter block instead of rewriting the first.
#
# The inner group is OPTIONAL: an opener immediately followed by a closer
# (``---\n---``, an empty block) has no line between them for ``(.*?)\n`` to
# consume, so the non-optional form never matched it at all — the extractor
# reported "no fence", and a mode edit then PREPENDED a brand-new fence in
# front of the empty one instead of populating it, doubling the block. A
# match with the group absent (``None``, distinct from the empty string a
# real-but-blank line would capture) is exactly that zero-line case.
_COLUMN0_BLOCK_CRLF_RE = re.compile(r"^---\r?\n(?:(.*?)\r?\n)?---", re.DOTALL)

# DEFERRED, deliberately: ``column0_fence`` (``SKILL_LOADER``) and
# ``leading_ws_fence`` (``SKILL_UPDATE``) keep the LF-only grammar, so a
# Windows-authored SKILL.md still has invisible front matter. Widening them
# changes what the skills catalog and the update merge ACCEPT — a behaviour
# change with its own review surface and its own snapshot corpus, which is
# why each dialect is a separate constant. Steering was fixed here because
# its own feature depends on the declaration being read at all.
# Fence extraction for the "leading_ws_fence" dialect: identical, except any
# whitespace (including blank lines) may precede the opening fence.
_LEADING_WS_BLOCK_RE = re.compile(r"^\s*---\n(.*?)\n---", re.DOTALL)

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
    # Resolve a bare block-scalar indicator value (see
    # BLOCK_SCALAR_INDICATORS) from the blank-or-indented lines that follow
    # it, via fold_block_scalar. Dialects without this store the indicator
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


def fold_block_scalar(indicator: str, block: list[str]) -> str:
    """Resolve a YAML block scalar's indented lines into a single value.

    ``indicator`` is one of ``BLOCK_SCALAR_INDICATORS``; ``block`` holds the
    raw continuation lines (still carrying their indentation). Literal (``|``)
    scalars keep one line per newline. Folded (``>``) scalars fold a single
    break between plain lines to a space and keep blank lines as newlines
    (k blanks -> k newlines plain-to-plain, k+1 next to a more-indented line,
    where the separator break stays literal), and never fold a break adjacent
    to a more-indented line, so nested indentation survives. Indentation is
    stripped relative to the first non-blank line, and the result is trimmed,
    so the chomping modifier (``-``/``+``) has no residual effect on the
    stored value.
    """
    # Trim trailing blank lines without mutating the caller's list and
    # without per-iteration copies (a pathological blank run stays linear).
    end = len(block)
    while end and not block[end - 1].strip():
        end -= 1
    if not end:
        return ""
    block = block[:end]
    first = next(ln for ln in block if ln.strip())
    indent = len(first) - len(first.lstrip())
    dedented = [
        ln[indent:] if ln[:indent].isspace() or not ln.strip() else ln.lstrip() for ln in block
    ]
    if indicator.startswith("|"):
        return "\n".join(dedented).strip()
    # Folded: a single line break between two plain lines becomes a space;
    # blank lines are preserved as line breaks (k blanks -> k newlines); and
    # breaks adjacent to a more-indented line are kept, so indented structure
    # (nested lists, code-ish content) survives the fold.
    parts: list[str] = []
    pending_blanks = 0
    prev_more_indented = False
    for ln in dedented:
        if not ln.strip():
            pending_blanks += 1
            continue
        more_indented = ln[:1].isspace()
        if parts:
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
        parts.append(ln.rstrip() if more_indented else ln.strip())
        prev_more_indented = more_indented
        pending_blanks = 0
    return "".join(parts)


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
            is_block = dialect.resolve_block_scalars and bool(
                _BLOCK_SCALAR_HEADER_RE.match(line.partition(":")[2].strip())
            )
            while index < len(lines):
                nxt = lines[index]
                blank = not nxt.strip()
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
        if dialect.resolve_block_scalars and value in BLOCK_SCALAR_INDICATORS:
            # Blank or indented lines up to the next column-0 line are the
            # scalar's content; trailing blanks between fields are trimmed by
            # the folder. Under a reject_indented dialect (every preset that
            # resolves scalars) consuming them hides no key — those lines
            # could not have been fields anyway. A custom dialect combining
            # resolution with accept_indented trades indented-key visibility
            # for scalar content, which is what the indicator means in YAML.
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i])
                i += 1
            value = fold_block_scalar(value, block)
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
