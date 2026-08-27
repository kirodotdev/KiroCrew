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

# Fence extraction for the "column0_fence" dialect: the opener must be exactly
# ``---`` at position 0 followed by a newline; the closer is the next line
# that *starts with* ``---`` (trailing text after the closer is tolerated).
_COLUMN0_BLOCK_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# As above, but tolerating CRLF. A steering file authored on Windows has
# ``---\r\n``, which the LF-only fence above does not match at all — so its
# declaration was invisible (the tab reported the default mode and the
# runtime skipped the document entirely) and an edit PREPENDED a second
# front-matter block instead of rewriting the first.
_COLUMN0_BLOCK_CRLF_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.DOTALL)

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


def render_frontmatter_value(value: str) -> str:
    """Render *value* as a single-line frontmatter value.

    Quotes only when a bare value would parse back as something else: an empty
    string, edge whitespace (which every dialect strips), or a character that
    starts a comment or a nested mapping. A glob like ``src/**/*.ts`` needs no
    quoting to round-trip, but Kiro's own documentation writes patterns quoted,
    so a caller that wants them quoted passes them through already-quoted-safe
    text and gets the same bytes back.

    Raises ``ValueError`` on a newline: a value carrying one would silently
    become extra frontmatter lines, or close the fence.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("a frontmatter value may not contain a newline")
    if (
        value == ""
        or value != value.strip()
        or ":" in value
        or value.lstrip()[:1] in {"#", '"', "'", ">", "|"}
    ):
        # No escaping: the parser's ``strip_quotes`` is ``strip("\"'")``, which
        # removes RUNS of quote characters and understands no escape sequence at
        # all. Emitting ``\"`` would therefore round-trip as a literal backslash.
        # Values that cannot survive quoting are rejected by the round-trip check
        # in :func:`set_frontmatter_fields` instead of being silently mangled.
        return '"' + value + '"'
    return value


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

    Only ``column0_fence`` and ``leading_ws_fence`` are supported; ``line_scan``
    does not identify its block precisely enough to rewrite it in place.
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
            f"{key}: {render_frontmatter_value(value)}"
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
        # Consume a block scalar's continuation lines with its key line, so a
        # replaced or removed folded value leaves nothing behind.
        continuation: list[str] = []
        if is_field and dialect.resolve_block_scalars:
            if line.partition(":")[2].strip() in BLOCK_SCALAR_INDICATORS:
                while index < len(lines) and (
                    not lines[index].strip() or lines[index][:1].isspace()
                ):
                    # Strip the CR here too: these lines are re-joined with the
                    # document's newline, so a retained \r would be written back
                    # as \r\r\n.
                    continuation.append(lines[index].rstrip("\r"))
                    index += 1
        if is_field and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}: {render_frontmatter_value(value)}")
            # value is None → the key (and any fold) is dropped.
            continue
        out.append(line)
        out.extend(continuation)

    out.extend(
        f"{key}: {render_frontmatter_value(value)}"
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
        return match.group(1).split("\n"), (text[match.end() :] if want_body else "")
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
        elif dialect.strip_quotes:
            value = value.strip("\"'")
        if dialect.first_key_wins and key in fields:
            continue
        fields[key] = value
    return fields
