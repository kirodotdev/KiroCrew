#!/usr/bin/env python3
"""check_focus_cue.py — gate keyboard focus cues on elements a change touches.

A control a keyboard user can reach with Tab must show *something* when it holds
focus (WCAG 2.4.7 Focus Visible, Level AA). ``index.css`` is where that comes from
for free, via a global ``:focus-visible`` outline, so the way this breaks in
practice is an element opting out of the outline — ``outline-none``,
``focus:outline-none`` — and putting nothing in its place. Nothing else fails when
that happens: the element still works, and a pointer user never sees the
difference.

Whether "inherit the global outline" is a working remedy depends on that rule
being live, so ``global_ring_is_live()`` reads it out of the stylesheet rather than
assuming it. If the rule is ever disabled, removing a suppressor would turn this
gate green while a keyboard user still saw nothing — a gate certifying a fix that
does not render — so the remedy list drops that option and says so.

## Why diff-scoped and not whole-tree

The rule reads an element's whole ``className``, so it is precise, but the tree
can still carry pre-existing holes and a whole-tree gate would charge them to
whoever pushed next. The enforcing check therefore reads only elements whose
``className`` (or opening tag) this change *touched* (``FOCUS_CUE_BASE_REF``),
which is complete for regression: a hole can only reach ``main`` through a diff
that wrote its className. The whole-tree number is still printed, as a
non-failing report, so the backlog stays visible without being a build break.

## What is exempt, and why

* An element Tab cannot land on — no natively focusable tag, no ``tabIndex={0}``,
  no ``onClick``, no ``role``.
* ``tabIndex={-1}``: a programmatic focus target (a route heading, a dialog
  shell). Focus is moved there by script to reposition a screen reader, so a
  ring would appear for a navigation the user did not perform.
* An element that supplies its own cue — any ``focus-visible:``/``focus:``
  utility that paints (ring, border, background, shadow, text), or the repo's
  ``focus-ring`` class.
* An element whose cue is carried by an **ancestor** through ``focus-within:`` —
  the search-pill idiom, where a wrapper gets ``focus-within:border-accent`` and
  the input inside suppresses its own outline so the two indicators do not stack.
  The wrapper's utility must pass ``is_cue`` (a suppressor like
  ``focus-within:ring-0``, or a non-visual one like ``focus-within:w-auto``, does
  not exempt) and its ``className`` must be a **static string literal**: a cue
  inside a ternary or template literal is present on only some branches, and
  taking it as proof would silence the branches that have none.
* An element whose cue is carried by a **descendant** through Tailwind's
  ``group`` mechanism: the element itself has the ``group`` marker class and a
  child has ``group-focus-visible:ring-2`` or similar. Such an element suppresses
  its own outline *correctly* — the child's ring is the replacement, and
  frequently the better one, since a slider knob's ring points at the current
  value where an outline round the whole track does not. Reading only the
  element's own ``className`` scores it as an uncued suppressor; acting on that
  report means deleting a correct ``outline-none`` and painting two focus
  indicators on one control, which is exactly what happened on the
  ``role="slider"`` track in ``components/ui.tsx`` before this exemption
  existed. Both halves are required: the marker alone is not a cue, and a
  ``group-*`` utility that is itself a suppressor is not a cue. ``peer`` is
  deliberately NOT recognised — the tree has zero ``peer-`` cues, so it would be
  a branch with no consumer; such an element uses the ``focus-cue-ok`` hatch
  until real usage exists.

``focus:outline-none`` is a *suppressor*, not a cue. Matching only the
``focus:outline`` prefix promotes it to a cue and hides the hole it creates,
which is why the self-test plants that exact case.

## Usage

    # enforce on the elements this branch touched (exit 1 on any violation)
    FOCUS_CUE_BASE_REF=origin/main python3 scripts/check_focus_cue.py

    # report the whole-tree backlog, enforce nothing (exit 0)
    python3 scripts/check_focus_cue.py

    # self-test: plant one probe per rule family, assert each verdict
    python3 scripts/check_focus_cue.py --test

## Escape hatch

An element whose cue this rule cannot see (one supplied by a parent, or by CSS
keyed on something other than a class) can opt out with a ``focus-cue-ok``
comment on the line the ``className`` starts. Use it sparingly, and say where
the real cue lives.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOT = "website/src"
MARKER = "focus-cue-ok"

# A Tailwind utility, arbitrary-value brackets included.
UTIL = re.compile(r"\b(focus-visible|focus):([a-z0-9-]+(?:\[[^\]]*\])?)")
BARE_OUTLINE_NONE = re.compile(r"(?<![-:\w])outline-none\b")
TAG = re.compile(r"<([A-Za-z][A-Za-z0-9.]*)")
NEG_TABINDEX = re.compile(r"tabIndex\s*=\s*\{\s*-1\s*\}")
POS_TABINDEX = re.compile(r"tabIndex\s*=\s*\{?\s*[\"']?0")

# Utilities that REMOVE a focus cue rather than add one. Everything else with a
# focus variant is treated as a cue.
SUPPRESSORS = frozenset({
    "outline-none", "outline-0", "outline-transparent",
    "ring-0", "ring-transparent", "shadow-none", "border-transparent",
})
NATIVE_FOCUSABLE = frozenset({"input", "textarea", "select", "button", "a"})

# A cue has to be something a person can SEE change. Treating "any focus:
# utility that is not a suppressor" as a cue lets a purely non-visual one --
# `focus:cursor-pointer`, `focus:z-10`, `focus:px-2` -- exempt an element that
# still shows the user nothing, which is a false NEGATIVE: the gate passes the
# exact defect it exists to catch, and the module contract above already says the
# cue must "paint". So a cue must belong to a family that changes appearance.
# Families are derived from what this repo actually writes (`grep -o
# 'focus(-visible)?:[a-z0-9-]+' website/src`), not guessed, and the list is
# deliberately generous: over-accepting only risks missing a hole, while
# over-rejecting invents a blocking failure on correct code -- and this gate
# already shipped one of those.
CUE_FAMILIES = (
    "ring", "border", "outline", "shadow", "bg", "text", "opacity", "rounded",
    "underline", "decoration", "not-sr-only", "from", "to", "via",
)
# `text-` also spells size and alignment, which change nothing about focus.
NON_VISUAL_TEXT = re.compile(
    r"^text-(xs|sm|base|lg|xl|[0-9]|left|right|center|justify|wrap|nowrap|"
    r"ellipsis|clip|balance|pretty)"
)


def is_cue(util: str) -> bool:
    """Whether a focus-variant utility visibly signals the focus state."""
    if util in SUPPRESSORS:
        return False
    if NON_VISUAL_TEXT.match(util):
        return False
    return any(
        util == family or util.startswith(family + "-") or util.startswith(family + "[")
        for family in CUE_FAMILIES
    )


# Tailwind's group/peer mechanism puts the CUE on a descendant and the marker
# class on the ancestor: the ancestor carries `group`, a child carries
# `group-focus-visible:ring-2`. The ancestor therefore legitimately suppresses
# its own outline -- the child's ring IS the replacement cue, and often a better
# one (a slider knob's ring points at the current value; an outline round the
# whole track does not). Reading only the ancestor's own className scores that
# ancestor as an uncued suppressor, which is a FALSE POSITIVE -- and because this
# gate hard-fails a diff, a false positive here does real damage: it tells the
# author to delete a correct `outline-none`, which then paints two focus
# indicators on one control. That is not hypothetical; it is the defect this
# check's sibling PR shipped and had to revert on the `role="slider"` track in
# `components/ui.tsx`.

GROUP_MARKER = re.compile(r"(?<![-:\w])group(?![-\w])")
DESCENDANT_CUE = re.compile(r"\bgroup-(?:focus-visible|focus):([a-z0-9-]+)")
# `group` only, deliberately NOT `(group|peer)`: `grep -r 'peer-(focus|checked|hover)'
# website/src` returns 0 and `group-focus-visible` returns 1, so a peer branch would
# be a generalization with no consumer — the same reason the explicit-paths branch
# was removed from main() earlier in this PR. A future peer-cued element has the
# `focus-cue-ok` hatch until real usage exists, and adding the alternation back is
# one token.
# How far past an element's opening tag to look for the descendant that carries
# the cue. A window rather than real subtree matching: JSX close-tag matching in
# a regex scanner is fragile, and the direction of the error matters -- an
# over-generous window can only ever SILENCE a report, never invent one, and for
# a blocking gate a missed violation is cheaper than a wrongly failed build.
DESCENDANT_CUE_WINDOW = 60

# The mirror image, and the more common one in this tree: the cue sits on the
# WRAPPER via `focus-within:`, and the control inside legitimately suppresses its
# own outline so the two indicators do not stack. A bordered search pill --
# `border border-border focus-within:border-accent` on the div, `outline-none` on
# the input -- is the dominant search-field idiom here, and reading only the
# input's own className scores it as an uncued suppressor.
#
# That is the same FALSE POSITIVE class as the group mechanism above, and it does
# the same damage: the remedy this gate prints tells the author to add a ring on
# top of the wrapper's existing border change, painting two focus indicators on
# one control -- which is exactly what the sibling check shipped and reverted.
ANCESTOR_CUE = re.compile(r"\bfocus-within:([a-z0-9-]+(?:\[[^\]]*\])?)")
# Four lines, deliberately tight. A wrapper's cue is on the line immediately
# above its child, or within a short attribute list; every one of the wrapper-cued
# elements in this tree sits at distance 1-4. A wider window starts exempting
# elements whose nearest `focus-within:` belongs to a DIFFERENT wrapper -- at 20
# it silences the composer textarea in `components/ChatInput.tsx`, whose
# `focus-within:border-accent/50` lives only in the final else-branch of a
# ternary, so three persistent modes really do render with no regional cue.
ANCESTOR_CUE_WINDOW = 4


@dataclass
class Violation:
    path: str
    line: int
    tag: str
    suppressors: tuple[str, ...]

    def render(self) -> str:
        return (f"  {self.path}:{self.line}  <{self.tag}> suppresses the focus "
                f"outline ({', '.join(self.suppressors)}) with no cue in its place")


def blank_comments(src: str) -> str:
    """Replace comment and string bodies' comment markers, keeping offsets.

    Offsets and newlines are preserved so a reported line number still points at
    the real line, and a `className` written inside a commented-out block cannot
    be reported as live code.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif src[i] in "\"'`":
            quote = src[i]
            i += 1
            while i < n and src[i] != quote:
                i += 2 if src[i] == "\\" else 1
            i += 1
        else:
            i += 1
    return "".join(out)


def region_span(src: str, start: int) -> tuple[int, int] | None:
    """Char span of the className value that begins at ``start``.

    A line-level match is not enough: `className` is routinely a template
    literal or a `cn()` call spanning several lines, and the cue can sit on any
    of them.
    """
    i = start
    while i < len(src) and src[i] in " \n\t":
        i += 1
    if i >= len(src):
        return None
    if src[i] in "\"'":
        j = src.find(src[i], i + 1)
        return (i + 1, j) if j > 0 else None
    if src[i] == "{":
        depth, j = 0, i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return (i + 1, j)
            j += 1
    return None


def owning_tag(src: str, pos: int) -> tuple[str, str, int]:
    """The element whose opening tag contains ``pos``: name, tag text, offset."""
    at = src.rfind("<", 0, pos)
    while at > 0 and not TAG.match(src, at):
        at = src.rfind("<", 0, at)
    # `at == 0` exits the loop WITHOUT having matched, so the match must be
    # re-checked rather than assumed: a `<` at offset 0 that does not start a tag
    # (a bare `<` in text, a truncated file) would otherwise raise
    # AttributeError on None mid-scan and take the whole gate down with it.
    tag = TAG.match(src, at) if at >= 0 else None
    if tag is None:
        return "?", "", pos
    close = src.find(">", pos)
    return tag.group(1), src[at:close if close > 0 else pos], at


def descendant_supplies_cue(src: str, value: str, tag_end: int) -> bool:
    """Whether a descendant carries the cue via Tailwind's `group-*` variants.

    True only when BOTH halves of the mechanism are present: the element itself
    carries the `group` marker class, and a `group-focus-visible:` /
    `group-focus:` cue that is not itself a suppressor appears within the
    lookahead window after the element's opening tag.
    """
    if GROUP_MARKER.search(value) is None:
        return False
    lines = src[tag_end:].split("\n")[:DESCENDANT_CUE_WINDOW]
    return any(
        is_cue(util)
        for util in DESCENDANT_CUE.findall("\n".join(lines))
    )


def still_open_at(src: str, opened_at: int, pos: int) -> bool:
    """Whether the element whose opening tag ends at ``opened_at`` encloses ``pos``.

    Proximity alone is not enclosure: a CLOSED sibling wrapper sitting a line or
    two above would otherwise lend its cue to an element it does not contain, and
    the gate would accept a keyboard control with no visible cue -- a false
    NEGATIVE, which is the one direction an exemption must never introduce.

    Depth is counted by tag, not parsed: over the few lines this is asked about,
    every `<tag` opens, `/>` and `</tag>` close, and reaching depth 0 means the
    candidate wrapper ended before ``pos``. Fragments and expression braces do not
    carry focus utilities, so they cannot change the answer.
    """
    depth = 1
    for m in re.finditer(r"</\s*[A-Za-z][\w.-]*\s*>|/>|<\s*[A-Za-z][\w.-]*",
                         src[opened_at:pos]):
        depth += -1 if m.group().startswith(("</", "/>")) else 1
        if depth <= 0:
            return False
    return True


def ancestor_supplies_cue(src: str, tag_at: int) -> bool:
    """Whether an ENCLOSING element carries the cue via `focus-within:`.

    Two conditions, and dropping either one turns this exemption into a false
    negative:

    * The wrapper must actually still be open at the element -- see
      ``still_open_at``. A nearby closed sibling is not an ancestor.
    * The wrapper's `className` must be a STATIC STRING LITERAL. A cue inside a
      template literal or ternary is present on only some branches, and taking it
      as proof would silence the branches that have none:
      `components/SearchBar.tsx` carries `focus-within:border-accent` one line
      above its input, but only in the undocked branch -- docked renders no border
      and no cue at all.
    """
    lines = src[:tag_at].split("\n")
    window_at = len("\n".join(lines[:-(ANCESTOR_CUE_WINDOW + 1)])) if (
        len(lines) > ANCESTOR_CUE_WINDOW + 1) else 0
    for m in re.finditer(r'\bclassName\s*=\s*"([^"]*)"', src[window_at:tag_at]):
        if not any(is_cue(util) for util in ANCESTOR_CUE.findall(m.group(1))):
            continue
        at = window_at + m.end()
        close = src.find(">", at)
        if close == -1:
            continue
        # A self-closed wrapper (`<div ... />`) encloses nothing, so its cue can
        # never reach the element below it.
        if src[close - 1] == "/":
            continue
        if still_open_at(src, close + 1, tag_at):
            return True
    return False


def scan_source(path: str, raw: str) -> list[tuple[Violation, set[int]]]:
    """Violations in ``raw``, each with the line span the element occupies."""
    src = blank_comments(raw)
    found: list[tuple[Violation, set[int]]] = []
    for m in re.finditer(r"\bclassName\s*=", src):
        span = region_span(src, m.end())
        if span is None:
            continue
        value = src[span[0]:span[1]]
        utils = UTIL.findall(value)
        suppressors = [f"{v}:{u}" for v, u in utils if u in SUPPRESSORS]
        cues = [f"{v}:{u}" for v, u in utils if is_cue(u)]
        if BARE_OUTLINE_NONE.search(value):
            suppressors.append("outline-none")
        if not suppressors or cues or "focus-ring" in value:
            continue
        tag, tag_text, tag_at = owning_tag(src, m.start())
        if NEG_TABINDEX.search(tag_text):
            continue
        reachable = (
            tag.lower() in NATIVE_FOCUSABLE
            or bool(POS_TABINDEX.search(tag_text))
            or "onClick" in tag_text
            or "role=" in tag_text
        )
        if not reachable:
            continue
        if descendant_supplies_cue(src, value, tag_at + len(tag_text)):
            continue
        if ancestor_supplies_cue(src, tag_at):
            continue
        first = raw[:tag_at].count("\n") + 1
        last = raw[:span[1]].count("\n") + 1
        if MARKER in "\n".join(raw.splitlines()[first - 1:last]):
            continue
        found.append((
            Violation(path, raw[:m.start()].count("\n") + 1, tag,
                      tuple(sorted(set(suppressors)))),
            set(range(first, last + 1)),
        ))
    return found


def read_text(path: str) -> str | None:
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout


def in_scope(path: str) -> bool:
    return path.startswith(SCAN_ROOT) and path.endswith(".tsx")


_SCOPE_MODULE = None


def _scope():
    """The shared diff plumbing (see ``scripts/ratchet_scope.py``).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]``. Shared so the
    env-base gates and the merge-ref ratchets agree on what a change added; a
    private copy per gate is how they would come to disagree. Loaded lazily so
    the ``--test`` mode's RULE probes do not require it — the deletion probe
    does, since it exercises the real parsing chain through this loader.
    """
    global _SCOPE_MODULE
    if _SCOPE_MODULE is None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratchet_scope.py")
        spec = importlib.util.spec_from_file_location("ratchet_scope", script)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCOPE_MODULE = module
    return _SCOPE_MODULE


# One decision, made once: a deletion-only hunk is ANCHORED, not dropped. Both
# consumers below reference this constant so the self-test's deletion probe
# (which goes through ``hunk_touched_lines``) covers the production path
# (``touched_lines``) — two independent keyword arguments would let the
# production one be dropped with the probe still green, silently restoring the
# exact failure the anchoring exists to prevent.
_ANCHOR_DELETIONS = True


def diff_base(base: str) -> str:
    """The commit to measure against; a shallow CI clone may have no merge-base."""
    return _scope().resolve_base(base)


def changed_paths(frm: str) -> list[str]:
    """In-scope paths this change touches (``ratchet_scope.changed_paths_at``)."""
    try:
        paths = _scope().changed_paths_at(frm)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::focus-cue gate: cannot diff against {frm} — the base commit "
            f"is not present. Fetch it before running, or unset FOCUS_CUE_BASE_REF "
            f"to report whole-tree counts without enforcing.\n{exc.stderr}"
        )
    return [p for p in paths if in_scope(p)]


def hunk_touched_lines(diff: str) -> set[int]:
    """1-based line numbers the hunk headers in ``diff`` mark as touched.

    Delegates to ``ratchet_scope.parse_added_lines`` with
    ``_ANCHOR_DELETIONS``, and stays a named seam so the self-test's
    deletion-only probe exercises the REAL parsing chain without a repository.
    The anchoring is this gate's one semantic difference from its siblings: a
    deletion-only hunk reads ``+<start>,0`` — the change removed lines and
    added none — and the edit that most often removes a focus cue is exactly a
    deletion. Delete the line carrying ``focus-visible:ring-2`` from a
    multi-line className and the element's own lines are all unchanged, so no
    purely-added line intersects it and the gate that exists to catch a lost
    cue would let the loss straight through. ``start`` is where the removed
    lines sat, so the hunk is anchored there.
    """
    return _scope().parse_added_lines(diff, anchor_deletions=_ANCHOR_DELETIONS)


def touched_lines(frm: str, path: str) -> set[int]:
    """1-based line numbers this change touched in ``path`` (base to working tree)."""
    return _scope().added_lines_at(frm, path, anchor_deletions=_ANCHOR_DELETIONS)


def global_ring_is_live() -> bool:
    """Whether index.css currently declares an ACTIVE global :focus-visible outline.

    The gate's most natural remedy — "drop the suppressor and inherit the global
    outline" — only works if that rule is live. An author who follows it while the
    rule is disabled turns this gate green while the keyboard user still sees
    nothing, so the gate would certify a fix that does not render. The remedy list
    is therefore generated from the stylesheet's real state rather than assumed,
    which also means it needs no edit when the rule is toggled either way.
    """
    raw = read_text(os.path.join(SCAN_ROOT, "index.css"))
    if raw is None:
        return False  # fail closed: do not advertise a remedy we cannot verify
    active = blank_comments(raw)
    rule = re.search(r"(?:^|[};])\s*:focus-visible\s*\{([^}]*)\}", active, re.M)
    if rule is None:
        return False
    outline = re.search(r"outline\s*:\s*([^;}]+)", rule.group(1))
    return outline is not None and not re.match(r"^(none|0)\b", outline.group(1).strip())


def remedies() -> str:
    """The fix guidance shown on a gate failure, ordered by what actually works."""
    inherit = (
        "drop the suppressor and inherit the global `:focus-visible` outline from "
        "index.css; or "
        if global_ring_is_live()
        else ""
    )
    unavailable = (
        ""
        if global_ring_is_live()
        else "\n\nNOTE: dropping the suppressor to \"inherit the global "
             "`:focus-visible` outline\" is NOT a fix at this commit — that rule is "
             "currently commented out in index.css, so removing a suppressor turns "
             "this gate green while a keyboard user still sees nothing. Use one of "
             "the options above until the global rule is restored."
    )
    return (
        f"\nPick whichever fits the element: {inherit}add the `focus-ring` "
        "class for the repo's border+glow treatment; or add your own "
        "`focus-visible:` utility. Prefer `focus-visible:` over `focus:` so a "
        "pointer click does not paint a ring — except on a roving-focus surface "
        "(a menu item, a skip link), where the cue must survive programmatic "
        "focus and `focus:` is correct."
        f"\n\nIf the cue genuinely lives somewhere this rule cannot see, put a "
        f"`{MARKER}` comment on the element **and say in it where the real cue "
        f"lives**, so the opt-out stays auditable."
        f"{unavailable}"
    )


def report(violations: Iterable[Violation], *, enforcing: bool,
           base: str | None) -> int:
    violations = list(violations)
    if not violations:
        scope = f"elements touched since {base}" if enforcing else "whole tree"
        print(f"focus-cue gate: every Tab-reachable element in the {scope} "
              f"keeps a focus cue OK")
        return 0
    if enforcing:
        print(f"::error::focus-cue gate: {len(violations)} element(s) this change "
              f"touched drop the focus outline and put nothing in its place, so a "
              f"keyboard user cannot see them take focus.")
    else:
        print(f"::notice::focus-cue gate report: {len(violations)} pre-existing "
              f"element(s) drop the focus outline with no cue in its place. Not "
              f"enforced here; only elements a change touches are gated.")
    shown = 200 if enforcing else 40
    for violation in violations[:shown]:
        print(violation.render())
    if len(violations) > shown:
        print(f"... and {len(violations) - shown} more")
    if enforcing:
        print(remedies())
    return 1 if enforcing else 0


def enforce_diff(base: str) -> int:
    """Enforce on elements this change touched. Fails closed on unreadable files."""
    frm = diff_base(base)
    found: list[Violation] = []
    unreadable: list[str] = []
    for path in changed_paths(frm):
        lines = touched_lines(frm, path)
        if not lines:
            continue
        raw = read_text(path)
        if raw is None:
            unreadable.append(path)
            continue
        # An element counts as touched when the change wrote ANY line of its
        # opening tag or className, because that is where a cue is added or lost.
        found.extend(v for v, span in scan_source(path, raw) if span & lines)
    if unreadable:
        print("::error::focus-cue gate: cannot read these changed files as UTF-8, "
              "so their focus cues were never checked:")
        for path in unreadable:
            print(f"  {path}")
        return 1
    return report(found, enforcing=True, base=base)


def report_tree() -> int:
    found: list[Violation] = []
    for path in git(["ls-files", "-z", "--", SCAN_ROOT]).split("\0"):
        if not path or not in_scope(path):
            continue
        raw = read_text(path)
        if raw is not None:
            found.extend(v for v, _ in scan_source(path, raw))
    return report(found, enforcing=False, base=None)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

# (name, source, should_be_caught). One probe per rule family: a typo that
# silently disables an exemption, or promotes a suppressor to a cue, fails here
# instead of shipping green.
PROBES: list[tuple[str, str, bool]] = [
    ("input suppresses, no cue",
     '<input className="border outline-none" />', True),
    ("focus:outline-none is a suppressor, not a cue",
     '<button className="px-2 focus:outline-none">x</button>', True),
    ("template literal className, no cue",
     '<input className={`border outline-none ${wide ? "w-4" : "w-2"}`} />', True),
    ("cn() className, no cue",
     '<input className={cn("border", "outline-none")} />', True),
    ("div made focusable by tabIndex={0}",
     '<div tabIndex={0} className="outline-none">x</div>', True),
    ("div made focusable by onClick",
     '<div onClick={go} className="outline-none">x</div>', True),
    ("cue via focus-visible: utility",
     '<input className="outline-none focus-visible:border-accent" />', False),
    ("cue via focus: utility",
     '<input className="outline-none focus:border-accent" />', False),
    ("cue via the focus-ring class",
     '<input className="outline-none focus-ring" />', False),
    ("tabIndex={-1} is a programmatic focus target",
     '<h1 tabIndex={-1} className="outline-none">x</h1>', False),
    ("plain div Tab cannot reach",
     '<div className="outline-none">x</div>', False),
    ("cue on a later line of a className that spans lines",
     '<input\n  aria-label="q"\n  className={`outline-none\n'
     '    focus-visible:ring-accent`}\n/>', False),
    ("commented-out className is not live code",
     '{/* <input className="outline-none" /> */}', False),
    (f"{MARKER} marker opts the element out",
     f'<input className="outline-none" />  // {MARKER}: parent paints it', False),
    ("no suppressor at all",
     '<input className="border focus-visible:border-accent" />', False),
    # --- the group/peer mechanism: the cue is on a DESCENDANT ----------------
    ("group ancestor whose child carries the cue is already cued",
     '<div role="slider" tabIndex={0} className="group outline-none">\n'
     '  <div className="h-1 bg-border" />\n'
     '  <div className="w-4 h-4 group-focus-visible:ring-2 '
     'group-focus-visible:ring-[var(--ring)]" />\n'
     '</div>', False),
    ("peer marker is NOT a recognised carrier — zero consumers in the tree",
     '<label className="peer outline-none" tabIndex={0}>\n'
     '  <span className="peer-focus-visible:border-accent" />\n'
     '</label>', True),
    # ...but neither half alone is a cue: the mechanism needs both, and a
    # `group-*` utility that is itself a suppressor is not a replacement.
    ("group marker with NO group-keyed descendant cue is still a violation",
     '<div role="slider" tabIndex={0} className="group outline-none">\n'
     '  <div className="w-4 h-4 bg-white" />\n'
     '</div>', True),
    ("descendant group cue without the group marker on the ancestor",
     '<div role="slider" tabIndex={0} className="outline-none">\n'
     '  <div className="group-focus-visible:ring-2" />\n'
     '</div>', True),
    ("a group-keyed SUPPRESSOR on the descendant is not a cue",
     '<div role="slider" tabIndex={0} className="group outline-none">\n'
     '  <div className="group-focus-visible:ring-0" />\n'
     '</div>', True),
    ("peer- cue does not satisfy a group marker",
     '<div role="slider" tabIndex={0} className="group outline-none">\n'
     '  <div className="peer-focus-visible:ring-2" />\n'
     '</div>', True),
    # --- a cue must be something the user can SEE change --------------------
    ("focus:cursor-pointer is not a cue — nothing visible changes",
     '<input className="outline-none focus:cursor-pointer" />', True),
    ("focus:z-10 is not a cue",
     '<button className="outline-none focus:z-10">x</button>', True),
    ("focus:text-sm is a size, not a focus cue",
     '<input className="outline-none focus:text-sm" />', True),
    ("focus-visible:text-danger IS a cue — a colour change is visible",
     '<input className="outline-none focus-visible:text-danger" />', False),
    ("focus:not-sr-only IS a cue — it reveals a hidden element",
     '<a href="#main" className="sr-only outline-none focus:not-sr-only">skip</a>',
     False),
    ("focus-visible:opacity-100 IS a cue",
     '<button className="outline-none opacity-0 focus-visible:opacity-100">x</button>',
     False),
    ("a group descendant cue must also be visible, not just non-suppressing",
     '<div role="slider" tabIndex={0} className="group outline-none">\n'
     '  <div className="group-focus-visible:cursor-pointer" />\n'
     '</div>', True),

    # The wrapper (`focus-within:`) mechanism. The search-pill idiom: the cue is
    # on the parent and the child suppresses its own outline so the two do not
    # stack, which is correct and must not be reported.
    ("a wrapper focus-within cue exempts the input inside it",
     '<div className="border border-border focus-within:border-accent">\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', False),
    ("a focus-within SUPPRESSOR is not a cue",
     '<div className="border focus-within:ring-0">\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', True),
    ("focus-within:border-transparent is not a cue",
     '<div className="border focus-within:border-transparent">\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', True),
    ("a non-visual focus-within utility is not a cue",
     '<div className="flex focus-within:w-auto">\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', True),
    ("a wrapper cue beyond the window does not reach the input",
     '<div className="border focus-within:border-accent">\n'
     '  <span />\n  <span />\n  <span />\n  <span />\n  <span />\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', True),
    # Both conditional shapes: a cue on only some branches proves nothing about
    # the branches that lack it, so it must not exempt.
    ("a wrapper cue inside a template literal does not exempt",
     '<div className={`border ${docked ? "" : "focus-within:border-accent"}`}>\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', True),
    ("a wrapper cue inside a ternary does not exempt",
     "<div className={docked ? 'flex w-full' : 'border focus-within:border-accent'}>\n"
     '  <input className="bg-transparent outline-none" />\n'
     '</div>', True),
    # Proximity is not enclosure. A CLOSED sibling must not lend its cue, or the
    # exemption becomes a false NEGATIVE -- the one direction it must never take.
    ("a closed sibling wrapper does not exempt the element after it",
     '<div className="border focus-within:border-accent">\n'
     '  <input className="bg-transparent outline-none" />\n'
     '</div>\n'
     '<input className="bg-transparent outline-none" />', True),
    ("a self-closed sibling wrapper does not exempt the element after it",
     '<div className="border focus-within:border-accent" />\n'
     '<input className="bg-transparent outline-none" />', True),
    ("a cue two levels up still exempts",
     '<div className="border focus-within:border-accent">\n'
     '  <span className="flex">\n'
     '    <input className="bg-transparent outline-none" />\n'
     '  </span>\n'
     '</div>', False),
]


def self_test() -> int:
    failures: list[str] = []
    for name, source, expected in PROBES:
        hits = scan_source("probe.tsx", source)
        if bool(hits) != expected:
            verb = "was not caught" if expected else "was caught but should not be"
            failures.append(f"  {name}: {verb}")

    # The cue must be found in the className, not merely somewhere in the tag —
    # a data attribute holding the same text must not exempt the element.
    decoy = '<input data-hint="focus-visible:ring-accent" className="outline-none" />'
    if not scan_source("probe.tsx", decoy):
        failures.append("  a cue-looking string outside className exempted the element")

    # The remedy list must match the stylesheet's REAL state, in both directions:
    # a gate that recommends inheriting a commented-out rule certifies a fix that
    # does not render. This assertion holds whichever state index.css is in, so it
    # keeps working after the global ring is restored.
    text = remedies()
    live = global_ring_is_live()
    if live and "inherit the global" not in text:
        failures.append("  global ring is live but the remedy list omits inheriting it")
    if not live:
        first_option = text.split("element:", 1)[1][:60]
        if "inherit" in first_option:
            failures.append("  ring is commented out yet the remedy still leads with "
                            "the inherit option")
        if "NOT a fix at this commit" not in text:
            failures.append("  ring is commented out but the remedy does not say so")
    if "say in it where the real cue lives" not in text:
        failures.append("  the focus-cue-ok hatch is offered without its audit "
                        "requirement in the same sentence")

    # A deletion-only hunk (`+N,0`) must still mark line N as touched. The edit
    # that most often removes a focus cue IS a deletion, so a zero-count hunk that
    # reported nothing touched would let the loss the gate exists to catch straight
    # through. The probe drives the REAL parser, and it uses a hunk header produced
    # by git rather than a hand-written one, so it cannot pass against a stale
    # notion of the format.
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "r")
        os.makedirs(repo)

        def run(*args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout

        run("init", "-q")
        run("config", "user.email", "probe@example.invalid")
        run("config", "user.name", "probe")
        target = os.path.join(repo, "a.tsx")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('<input\n  className="outline-none\n'
                         '    focus-visible:ring-accent\n    text-sm"\n/>\n')
        run("add", "a.tsx")
        run("commit", "-qm", "base")
        base_sha = run("rev-parse", "HEAD").strip()
        # remove ONLY the line carrying the cue, leaving its neighbours byte-identical
        # -> git emits a pure `+N,0` deletion hunk
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('<input\n  className="outline-none\n    text-sm"\n/>\n')
        run("add", "a.tsx")
        run("commit", "-qm", "drop the cue line")
        diff = run("diff", "--unified=0", "--no-color", base_sha, "HEAD", "--",
                   "a.tsx")
        if not any(re.search(r"\+\d+,0", ln)
                   for ln in diff.splitlines() if ln.startswith("@@")):
            failures.append("  the deletion-only probe stopped producing a `+N,0` "
                            "hunk, so it no longer tests what it claims")
        elif not hunk_touched_lines(diff):
            failures.append("  a deletion-only hunk reports no touched lines, so "
                            "removing a focus cue would pass the gate unseen")

    # The real tree must be scannable and the reported line must be the
    # className's own line, so a violation is navigable from CI output.
    with tempfile.TemporaryDirectory() as tmp:
        sample = os.path.join(tmp, "s.tsx")
        with open(sample, "w", encoding="utf-8") as handle:
            handle.write('const a = 1\n\n<input\n  className="outline-none"\n/>\n')
        with open(sample, encoding="utf-8") as handle:
            hits = scan_source("s.tsx", handle.read())
        if len(hits) != 1 or hits[0][0].line != 4:
            got = hits[0][0].line if hits else None
            failures.append(f"  className line number wrong: expected 4, got {got}")

    if failures:
        print(f"::error::focus-cue gate self-test: {len(failures)} probe(s) "
              f"disagree with the rules:")
        for line in failures:
            print(line)
        return 1
    print(f"focus-cue gate self-test: {len(PROBES) + 2} probes agree OK")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return self_test()
    base = os.environ.get("FOCUS_CUE_BASE_REF", "").strip()
    if not base:
        return report_tree()
    # Print the whole-tree backlog FIRST, then enforce on the diff. Both, not
    # either: CI always sets FOCUS_CUE_BASE_REF, so an `enforce if base else
    # report` split meant the non-failing whole-tree notice this gate promises --
    # in its own workflow comment and in the PR that added it -- never actually
    # printed anywhere it would be read. The notice is what makes the backlog
    # visible while the gate stays diff-scoped; a promise no run keeps is worse
    # than no promise, because the number gets quoted as if someone had seen it.
    report_tree()
    return enforce_diff(base)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
