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

# Fence extraction for the "leading_ws_fence" dialect: identical, except any
# whitespace (including blank lines) may precede the opening fence.
_LEADING_WS_BLOCK_RE = re.compile(r"^\s*---\n(.*?)\n---", re.DOTALL)

# How the frontmatter block is located within the document. Each mode is the
# fence grammar of the dialects that name it; they are not interchangeable.
Extraction = Literal["column0_fence", "line_scan", "leading_ws_fence"]

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


def _extract_block(
    text: str, extraction: Extraction, *, want_body: bool = True
) -> tuple[list[str] | None, str]:
    """Locate the frontmatter block; ``(None, text)`` when there isn't one.

    With ``want_body=False`` the second element is ``""`` whenever a block
    was found (the caller promises not to read it); the no-block case still
    returns *text* so ``({}, text)`` stays uniform.
    """
    if extraction == "column0_fence" or extraction == "leading_ws_fence":
        pattern = _COLUMN0_BLOCK_RE if extraction == "column0_fence" else _LEADING_WS_BLOCK_RE
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
