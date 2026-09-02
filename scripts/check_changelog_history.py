#!/usr/bin/env python3
"""check_changelog_history.py — shipped changelog sections are immutable.

``CHANGELOG.md`` is append-only history. A release prepends one
``## [X.Y.Z] — YYYY-MM-DD`` section; every section already in the file describes
software a user has already installed, so a line deleted from one is
unrecoverable from that user's artifact.

## Why this is a deterministic gate and not a review rule

This exact loss already happened. A commit titled ``docs: add 0.3.0-insider.9
changelog`` *replaced* the file instead of prepending to it: 53 insertions and
322 deletions, taking the entire ``[0.2.0]``, ``[0.1.3]`` and ``[0.1.2]``
sections with it. It read as plausible in review, because the insertions are
what a reader looks at and the deletions were buried in the same hunk. Nothing
failed; the loss surfaced only when a user noticed the dashboard's Releases page
had gone nearly empty.

So the judgment half of the rule (is this a commit dump? should this PR be
touching the file at all?) stays in ``AUTOSDE.yaml`` where a reviewer applies it,
and the **mechanically checkable** half lives here, where no reviewer has to
notice anything. Two invariants:

    1. every version section present at the base ref must still be present at
       head, byte-identical
    2. head must contain ONLY shipped sections -- no ``## [Unreleased]``, no
       prerelease heading

with **no exemptions**. A section whose heading is a bare ``X.Y.Z`` describes
shipped software and is immutable, full stop.

Rule 2 is what makes rule 1 sufficient. Together they leave exactly one legal
shape for a changelog diff -- prepend one new section -- because there is no
draft section to append a per-PR line to and every released one is frozen. A
release's notes are written once, when the version is bumped, under the
release's own heading.

A **prerelease** heading (``## [0.3.0-insider.9]``, ``## [0.3.0-rc.2]``) is not a
shipped section -- it is a draft of a release that has not happened -- so
:func:`compare` skips it there exactly like ``## [Unreleased]``, and
:func:`draft_headings` refuses it at head. The two are not in tension: history is
read from refs that may legitimately contain such a heading, while the tree being
proposed may not introduce one. An earlier design used a prerelease heading as a
deliberate in-progress state on a release branch, to be renamed when the entry
was written. That state is what accumulated 0.4.0's 721-line unedited draft, and
it renders as "no notes" against the version the reader is actually running.

An earlier version of this check exempted "the newest section at the base ref" so
a release branch could redraft its own entry. That was wrong in a way worth
recording: on ``main`` the newest section at base is the **last shipped release**,
so the exemption made the most recently shipped section -- the one the most users
are running -- the single editable one. Keying on "newest" confused *position in
the file* with *not yet released*. Prerelease-versus-bare is the property that
actually distinguishes a draft from history, and it needs no tag lookup, so it
works in a shallow CI checkout.

Correcting a genuine factual error inside a shipped section is still possible; it
is simply not silent. The gate flags it, and the author says in the PR body that
this is what they are doing (the AUTOSDE rule's stated allowance) so a human
approves it deliberately.

``release.yml``'s stable gate is the complement of this check: it verifies the
new section *exists*. It does not verify the old ones survived, which is the
half that actually failed.

## One source of truth for folding

``0.3.0-insider.9``, ``0.3.0-rc.2`` and ``0.3.0`` are one release. The renderer
(``src/kiro_crew/changelog.py``) owns that folding, and this gate **loads it from
there** by path rather than re-implementing it. ``changelog.py`` imports only
``re`` and ``typing``, so it loads in a CI job that checked the tree out without
installing the package. A second copy of the regex was the earlier design and it
failed in the dangerous direction: a spelling the renderer folds but the gate does
not would make a legitimate release-branch rewrite read as a deleted section, or
let a real deletion pass.

## Usage

    # enforce against a base ref (exit 1 on any violation)
    CHANGELOG_BASE_REF="$(git merge-base HEAD origin/main)" \
        python3 scripts/check_changelog_history.py

    # self-test: one probe per rule family, assert each verdict
    python3 scripts/check_changelog_history.py --test

With no ``CHANGELOG_BASE_REF`` the check has nothing to compare against and
exits 0 with a note, so running it locally without a base is not a failure.

Pass the MERGE BASE, not the base branch's moving tip. CI passes
``pull_request.base.sha`` and tests the merge ref, so the two agree there; locally
they do not. A branch that is merely BEHIND a base which has since gained a release
reports that release as ``GONE`` -- the section is missing from the head being
compared, which is true and useless. The merge base is the commit the branch
actually departed from, so it answers "did THIS branch remove anything".
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple

CHANGELOG = "CHANGELOG.md"

#: The renderer, loaded by path so this gate cannot drift from it. Kept as a
#: by-path load rather than a package import because CI checks the tree out
#: without installing ``kiro_crew``; ``changelog.py`` has no third-party imports,
#: so the module loads standalone.
RENDERER_PATH = "src/kiro_crew/changelog.py"
_RENDERER = Path(__file__).resolve().parents[1] / RENDERER_PATH


def _load_renderer_source(source: str) -> object:
    """Load ``changelog.py`` source read from a git ref as a throwaway module.

    Routed through the same import machinery as the in-tree renderer rather than
    ``exec``: one loading mechanism instead of two, and the module gets a real
    spec, so a syntax error surfaces with a filename and a line number instead of
    a bare traceback.

    The base ref is the merge target -- the branch this change is asking to land
    on -- so its renderer is trusted here exactly as the checked-out one is.
    ``changelog.py`` imports only ``re`` and ``typing``, so loading it has no side
    effects and needs nothing installed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "changelog_at_base.py"
        path.write_text(source, encoding="utf-8")
        return _load_module_at(path)


def _load_module_at(path: Path) -> object:
    """Import a standalone ``changelog.py`` from ``path``."""
    spec = importlib.util.spec_from_file_location(f"_kc_changelog_renderer_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in-tree
        raise RuntimeError(f"cannot load the changelog renderer at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_renderer() -> object:
    return _load_module_at(_RENDERER)


_renderer = _load_renderer()
#: ``base_version("0.3.0-insider.9") == "0.3.0"``; an unparseable string returns
#: itself, which is what keeps a malformed heading from folding onto a real
#: release and silently replacing its notes.
fold_version = _renderer.base_version  # type: ignore[attr-defined]
#: The renderer's own list builder. Used ONLY by the self-test, to pin the fold
#: DIRECTION against the renderer rather than against a hardcoded expectation --
#: the one thing a second copy of the logic here could not check.
render_releases = _renderer.build_release_list  # type: ignore[attr-defined]


# The heading grammar is a VALUE, not a module global, because the base ref and the
# head can legitimately disagree about it: a PR is allowed to change the renderer.
# Parsing base history with the HEAD's grammar is what let a narrowed regex shrink
# the protected set -- see :func:`compare`.
class Grammar(NamedTuple):
    """One checkout's changelog grammar: how it finds headings and folds versions."""

    section_re: "re.Pattern[str]"
    h2_re: "re.Pattern[str]"
    fold: Callable[[str], str]


def grammar_of(renderer: object) -> Grammar:
    """Build a :class:`Grammar` from a loaded ``changelog.py`` module.

    Taken from the renderer, never copied. These are private names, and reaching
    for them is the deliberate choice: the alternative is a second copy of the
    heading grammar, and this gate's whole premise is that a spelling the renderer
    folds but the gate does not is a silent hole. A copy that starts byte-identical
    is exactly the failure mode -- it stays correct until someone widens one side.
    """
    return Grammar(
        section_re=renderer._SECTION_RE,  # type: ignore[attr-defined]
        h2_re=renderer._H2_RE,  # type: ignore[attr-defined]
        fold=renderer.base_version,  # type: ignore[attr-defined]
    )


#: The grammar of the checkout being tested. Used for head text, and as the base
#: grammar only when the base's own renderer cannot be read (see :func:`main`).
HEAD_GRAMMAR = grammar_of(_renderer)


def is_shipped_heading(version: str, grammar: Grammar = HEAD_GRAMMAR) -> bool:
    """True when ``version`` is a bare release (``X.Y.Z``), not a draft.

    A prerelease spelling folds onto a release it is only a *draft* of, so a
    prerelease heading documents nothing shipped. A malformed spelling keeps its
    own identity in the renderer, so it is its own fold and reads as shipped --
    the safe direction, since it cannot collide with a real release and the only
    consequence is that it is also protected from silent edits.
    """
    stripped = version.strip()
    if not stripped or not stripped[0].isdigit():
        return False
    return bool(grammar.fold(stripped) == stripped)


def _headings(text: str, grammar: Grammar = HEAD_GRAMMAR) -> list[tuple[str, str, str]]:
    """Return ``[(raw_version, folded_version, section_text)]`` in document order.

    EVERY level-2 version heading, prerelease and malformed included -- the
    filtering happens in :func:`compare`, deliberately. ``section_text`` includes
    the heading line, because the heading carries the release date and a rewritten
    date is a rewritten record.
    """
    out: list[tuple[str, str, str]] = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        heading_line = lines[index]
        heading = grammar.section_re.match(heading_line.rstrip("\n"))
        if not heading:
            index += 1
            continue
        raw = heading.group("version").strip()
        index += 1
        body: list[str] = []
        while index < len(lines) and not grammar.h2_re.match(lines[index].rstrip("\n")):
            body.append(lines[index])
            index += 1
        out.append((raw, grammar.fold(raw), heading_line + "".join(body)))
    return out


def resolve_as_rendered(text: str, grammar: Grammar = HEAD_GRAMMAR) -> dict[str, str]:
    """Return ``{release: the section the RENDERER will display}``.

    This is the load-bearing function, and its fold direction is not a detail.
    ``changelog.py``'s ``build_release_list`` resolves with ``dict.setdefault``, so
    **the FIRST body in document order wins** -- which under keep-a-changelog's
    newest-first ordering means the topmost spelling of a release is what a user
    sees. That direction is itself a fix: last-wins let a ``## [0.2.0-rc.1]``
    draft lower in the file replace the released ``## [0.2.0]`` body and date,
    with the fold being correct so nothing looked wrong.

    Mirror it exactly. Folding the other way here would compare a section the
    renderer never displays, which is the same class of blind spot this gate
    exists to close -- only harder to see, because the gate would look right.
    """
    resolved: dict[str, str] = {}
    for _raw, folded, section in _headings(text, grammar):
        if folded:
            resolved.setdefault(folded, section)
    return resolved


def parse_sections(text: str, grammar: Grammar = HEAD_GRAMMAR) -> list[tuple[str, str]]:
    """Return ``[(version, section_text)]`` for the SHIPPED releases only."""
    return [
        (raw, section)
        for raw, folded, section in _headings(text, grammar)
        if is_shipped_heading(raw, grammar)
    ]


#: Matches a bracketed level-2 heading under ANY plausible grammar. Used only to
#: decide whether a base text that parsed to zero shipped releases is genuinely
#: empty or was mis-parsed -- never to extract a version.
_ANY_BRACKET_H2 = re.compile(r"^##\s+\[", re.MULTILINE)


def draft_headings(text: str, grammar: Grammar = HEAD_GRAMMAR) -> list[str]:
    """Return every level-2 heading that does not name a shipped release.

    ``## [Unreleased]``, ``## [0.4.0-rc.1]``, ``## [0.4.0-insider.3]``, and the
    unbracketed ``## Unreleased`` all qualify: each heads a section describing
    software nobody has installed.

    This is a HEAD-only invariant and deliberately not part of :func:`compare`.
    ``compare`` answers "did this change rewrite history", and a draft heading is
    not a rewrite of anything -- so mixing the two would make the immutability
    rule's verdict depend on whether a draft happened to be present. Two
    invariants, checked separately, in :func:`main`:

    1. every section the base documents as shipped is byte-identical at head
    2. head contains **only** shipped sections

    Together they leave exactly one legal shape for a changelog diff: prepend one
    new ``## [X.Y.Z] — YYYY-MM-DD`` section. There is nowhere to append a per-PR
    line, because there is no draft section to append it to and the released ones
    cannot be edited. That is the enforcement half of AGENTS.md ->
    "Release Changelog"; the file is written when a version is bumped and at no
    other time.

    A draft section is not merely untidy. ``build_release_list`` groups by
    ``base_version``, and ``base_version("Unreleased") == "Unreleased"``, so it
    never folds onto the version actually running -- while ``_sort_key`` ranks it
    ``(-1,)`` and sinks it below every release. A ``## [Unreleased]`` section
    therefore renders on the dashboard's Releases page as the running version
    with **no notes at all**, plus a trailing "Unreleased" row at the bottom of
    the archive carrying the entire body. 0.4.0's draft reached 721 lines that
    way, and nothing reported it.
    """
    drafts: list[str] = []
    for line in text.splitlines():
        if not grammar.h2_re.match(line):
            continue
        heading = grammar.section_re.match(line)
        raw = heading.group("version").strip() if heading else line.lstrip("#").strip()
        if not is_shipped_heading(raw, grammar):
            drafts.append(raw)
    return drafts


def compare(
    base_text: str,
    head_text: str,
    base_grammar: Grammar = HEAD_GRAMMAR,
    head_grammar: Grammar = HEAD_GRAMMAR,
) -> list[str]:
    """Return a violation per shipped release whose rendered notes changed.

    ONE invariant, replacing three separate filters that each leaked a variant of
    the same defect: *for every release the base ref documents as shipped, the
    section the renderer displays at head must be byte-identical to the one it
    displayed at base.* Comparing the RENDERED resolution rather than the sections
    a filter happened to keep is what makes shadowing unreachable -- a duplicate
    bare heading, a rewritten date, and a prerelease heading folding onto a
    shipped release are all just "the resolved section changed".

    Pure, so the self-test can exercise every rule family without git.
    """
    base_shipped = {raw for raw, _section in parse_sections(base_text, base_grammar)}
    if not base_shipped:
        # Fail CLOSED. An empty protected set is indistinguishable, from here,
        # between "the base genuinely shipped nothing" and "the base was mis-parsed
        # and every release just left the protected set" -- and the second reading
        # silently disables the whole gate. A base carrying bracketed level-2
        # headings that yields zero shipped releases is the second case.
        if _ANY_BRACKET_H2.search(base_text):
            return [
                "the base ref's changelog has version headings but NONE parsed as a "
                "shipped release, so there is nothing to protect and every deletion "
                "would pass. This is a parser problem, not a changelog problem: "
                "check what this change does to changelog.py's heading grammar."
            ]
        return []
    base_resolved = resolve_as_rendered(base_text, base_grammar)
    head_resolved = resolve_as_rendered(head_text, head_grammar)
    problems: list[str] = []

    for version in sorted(base_shipped):
        if version not in head_resolved:
            problems.append(
                f"[{version}] was present at the base ref and is GONE at head. "
                f"A shipped changelog section is history a user already "
                f"installed; a release prepends a new section and leaves every "
                f"earlier one untouched."
            )
        elif head_resolved[version] != base_resolved.get(version):
            problems.append(
                f"[{version}] renders DIFFERENT notes than it did at the base "
                f"ref. A shipped section is immutable, heading and body -- and it "
                f"is the section the renderer RESOLVES that matters, so a "
                f"duplicate or prerelease heading folding onto [{version}] counts "
                f"even when the original text is still in the file."
            )

    # Second rule, and it is not redundant with the first: a release must be
    # documented by EXACTLY ONE heading at head. The check above compares only
    # what the renderer RESOLVES, and first-wins means a second copy placed BELOW
    # the original resolves to the original -- clean by that measure, while the
    # file now carries a second, divergent account of shipped history that a human
    # reading the file sees. Counting headings per release catches that copy
    # wherever it sits, so neither rule depends on the other's ordering luck.
    #
    # A draft spelling counts as such a copy: ANY additional heading folding onto
    # a release that also has a bare heading is a violation, whichever order they
    # appear in. Order deciding which body a user sees is precisely the ambiguity
    # worth refusing -- a prerelease heading above the bare one wins the fold and
    # publishes draft notes under a release number.
    #
    # Counted across EVERY release at head, not only the ones the base documents:
    # a release being added by this very change can carry the same ambiguity, and
    # the resolved comparison cannot see it (there is no base section to differ
    # from), so one of its two bodies would silently go unread.
    head_folds: dict[str, list[str]] = {}
    for raw, folded, _section in _headings(head_text, head_grammar):
        if folded:
            head_folds.setdefault(folded, []).append(raw)
    for version in sorted(head_folds):
        raws = head_folds[version]
        bare = [r for r in raws if r == version]
        if len(raws) > 1 and (version in base_shipped or bare):
            problems.append(
                f"[{version}] is documented by {len(raws)} headings at head "
                f"({', '.join(raws)}). A release gets exactly one: the renderer "
                f"keeps a single body per release, so the others are invisible on "
                f"the Releases page while their text stays in the file."
            )
        elif version in base_shipped and raws[0] != version:
            problems.append(
                f"[{raws[0]}] folds onto shipped release [{version}] and has "
                f"replaced its heading. A draft spelling must not stand in for "
                f"shipped history."
            )
    return problems


def _git_show(ref: str, path: str) -> str | None:
    """Return ``path`` at ``ref``, or ``None`` when it does not exist there."""
    try:
        return subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _self_test() -> int:
    """One probe per defect class this gate has actually been shown to miss.

    Five review rounds each found a different way for two headings to map onto
    one rendered release while a check reported the file intact. All of them are
    probed here against the single resolved-section invariant, so a regression in
    any fails before enforcement. The last probe is different in kind: it asks
    ``changelog.py`` which body a release resolves to, because round 5 found the
    gate folding the opposite direction from the renderer with every rule still
    green.
    """
    shipped = "## [0.2.0] \u2014 2026-08-09\n\nold body\n\n"
    latest = "## [0.3.0] \u2014 2026-08-17\n\nreleased body\n\n"
    base = f"# Changelog\n\n{latest}{shipped}"
    cases: list[tuple[str, str, bool]] = [
        ("a deleted shipped section is caught", f"# Changelog\n\n{latest}", True),
        (
            "a modified shipped body is caught",
            f"# Changelog\n\n{latest}## [0.2.0] \u2014 2026-08-09\n\nEDITED\n\n",
            True,
        ),
        (
            "round 1a: a rewritten shipped DATE is caught (heading is the record)",
            f"# Changelog\n\n{latest}## [0.2.0] \u2014 2026-01-01\n\nold body\n\n",
            True,
        ),
        (
            "round 1b: edit-plus-untouched-copy (duplicate bare heading)",
            f"# Changelog\n\n{latest}## [0.2.0] \u2014 2026-08-09\n\nEDITED\n\n{shipped}",
            True,
        ),
        (
            "round 2: editing the NEWEST shipped section (no exemption)",
            f"# Changelog\n\n## [0.3.0] \u2014 2026-08-17\n\nrewritten\n\n{shipped}",
            True,
        ),
        (
            "round 3: a prerelease heading shadowing a shipped release",
            f"# Changelog\n\n{latest}## [0.2.0-rc.2]\n\ndraft notes\n\n{shipped}",
            True,
        ),
        (
            "round 3b: ...and in the order where the draft wins the fold",
            f"# Changelog\n\n{latest}{shipped}## [0.2.0-rc.2]\n\ndraft notes\n\n",
            True,
        ),
        (
            "prepending a new release is allowed",
            f"# Changelog\n\n## [0.4.0] \u2014 2026-09-01\n\nnew\n\n{latest}{shipped}",
            False,
        ),
        (
            "an Unreleased section is ignored, not treated as shipped",
            f"# Changelog\n\n## [Unreleased]\n\npending\n\n{latest}{shipped}",
            False,
        ),
        (
            "a prerelease for a release NOT yet shipped is allowed",
            f"# Changelog\n\n## [0.4.0-rc.1]\n\ndrafting\n\n{latest}{shipped}",
            False,
        ),
        (
            "round 4: a duplicate heading for a NEWLY ADDED release is caught",
            f"# Changelog\n\n## [0.4.0]\n\nfirst\n\n## [0.4.0]\n\nsecond\n\n{latest}{shipped}",
            True,
        ),
        (
            "round 5: a draft heading ABOVE a newly added release wins the fold",
            f"# Changelog\n\n## [0.4.0-rc.2]\n\ndraft\n\n## [0.4.0]\n\nreal\n\n{latest}{shipped}",
            True,
        ),
        (
            "round 5b: ...and below it, where order alone decided which body shows",
            f"# Changelog\n\n## [0.4.0]\n\nreal\n\n## [0.4.0-rc.2]\n\ndraft\n\n{latest}{shipped}",
            True,
        ),
    ]
    failures: list[str] = []
    for name, head_text, expect in cases:
        got = bool(compare(base, head_text))
        if got != expect:
            failures.append(f"{name}: expected violation={expect}, got {got}")

    # A release branch whose base documents no shipped release may rewrite its own
    # in-progress prerelease entry into the written release entry.
    pre_base = f"# Changelog\n\n## [0.3.0-insider.9] \u2014 2026-08-16\n\ngenerated\n\n{shipped}"
    if compare(pre_base, f"# Changelog\n\n{latest}{shipped}"):
        failures.append("replacing a prerelease draft heading is wrongly flagged")
    if not compare(pre_base, f"# Changelog\n\n{latest}"):
        failures.append("deleting a shipped section past a prerelease draft is not caught")

    # The fold DIRECTION is pinned against the renderer itself, not against a
    # hardcoded expectation. Round 5 found the gate folding last-wins while
    # ``build_release_list`` folds first-wins (``dict.setdefault``); every rule
    # was still covering the documents in this suite, so nothing went red and the
    # gate merely compared a section no user is shown. Asking the renderer removes
    # the chance of both sides drifting to the same wrong answer.
    dup = "# Changelog\n\n## [0.5.0]\n\nTOPMOST\n\n## [0.5.0-rc.1]\n\nLOWER\n\n"
    try:
        rendered = {r.version: r.body for r in render_releases(dup, "0.5.0")}
    except Exception as exc:  # pragma: no cover - renderer import is checked above
        failures.append(f"could not consult the renderer for fold direction: {exc}")
    else:
        mine = resolve_as_rendered(dup)
        if "TOPMOST" not in mine.get("0.5.0", ""):
            failures.append("resolve_as_rendered does not fold FIRST-wins like build_release_list")
        if ("TOPMOST" in rendered.get("0.5.0", "")) != ("TOPMOST" in mine.get("0.5.0", "")):
            failures.append(
                "the gate and the renderer disagree on which body a release resolves to"
            )

    # Round 6: the head's grammar must not be able to shrink what the BASE
    # documents. A PR may narrow changelog.py's heading regex; parsing base history
    # with the narrowed grammar drops those releases out of the protected set, and a
    # deletion in the same PR then passes unseen. Both probes fail under the earlier
    # single-grammar code, which is the point.
    narrowed = Grammar(
        # requires a date, so a dateless ``## [0.1.2]`` heading stops being seen
        section_re=re.compile(
            r"^##\s+\[(?P<version>[^\]]+)\]\s*[—–-]\s*(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\s*$"
        ),
        h2_re=HEAD_GRAMMAR.h2_re,
        fold=HEAD_GRAMMAR.fold,
    )
    dateless_base = (
        "# Changelog\n\n## [0.2.0] \u2014 2026-08-09\n\nkept\n\n## [0.1.2]\n\nshipped\n\n"
    )
    dateless_head = "# Changelog\n\n## [0.2.0] \u2014 2026-08-09\n\nkept\n\n"
    if not compare(dateless_base, dateless_head, HEAD_GRAMMAR, narrowed):
        failures.append(
            "round 6: a narrowed HEAD grammar hid the deletion of a section the base "
            "documents as shipped"
        )
    # and the mis-parse guard: a base that yields zero shipped releases while
    # carrying version headings means the parser lost them, not that nothing shipped
    blind = Grammar(
        section_re=re.compile(r"^##\s+NEVER-MATCHES-(?P<version>x)$"),
        h2_re=HEAD_GRAMMAR.h2_re,
        fold=HEAD_GRAMMAR.fold,
    )
    if not compare(dateless_base, dateless_head, blind, HEAD_GRAMMAR):
        failures.append(
            "round 6b: a base that parsed to zero shipped releases despite having "
            "version headings did not fail closed"
        )
    # ...but a base with genuinely no version headings is still a clean no-op
    if compare("# Changelog\n\n## Unreleased\n\npending\n", dateless_head):
        failures.append("a base with no version headings at all is wrongly flagged")

    # The draft-section rule. Probed here as well as in the test suite because
    # ci.yml runs --test immediately before enforcement, so a weakened rule fails
    # in the same job instead of passing green.
    draft_probes: list[tuple[str, str, bool]] = [
        ("an [Unreleased] section is caught", "# Changelog\n\n## [Unreleased]\n\npending\n", True),
        ("an unbracketed ## Unreleased is caught", "# Changelog\n\n## Unreleased\n\np\n", True),
        (
            "a prerelease heading is caught even when dated",
            "# Changelog\n\n## [0.4.0-rc.1] \u2014 2026-08-21\n\ndrafting\n",
            True,
        ),
        (
            "a draft below shipped sections is caught wherever it sits",
            f"# Changelog\n\n{latest}{shipped}## [Unreleased]\n\npending\n",
            True,
        ),
        ("a file of shipped sections only is clean", base, False),
    ]
    for label, probe_text, should_flag in draft_probes:
        if bool(draft_headings(probe_text)) != should_flag:
            failures.append(f"draft-section rule: {label}")

    if failures:
        for line in failures:
            print(f"self-test FAILED: {line}", file=sys.stderr)
        return 1
    print(f"changelog-history self-test: {len(cases) + 2 + len(draft_probes)} probes, all correct")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return _self_test()

    # Read head FIRST: the draft-section rule is a property of the file as it
    # stands and needs no base ref, so it must also hold when this is run locally
    # with nothing to compare against.
    try:
        with open(CHANGELOG, encoding="utf-8") as handle:
            head_text: str | None = handle.read()
    except FileNotFoundError:
        head_text = None

    if head_text is not None:
        drafts = draft_headings(head_text)
        if drafts:
            for heading in drafts:
                print(
                    f"::error::changelog-history: [{heading}] is a draft section. "
                    f"{CHANGELOG} holds shipped releases only -- a section is written "
                    f"when a version is bumped, under the release's own "
                    f"'## [X.Y.Z] — YYYY-MM-DD' heading, and never before.",
                    file=sys.stderr,
                )
            print(
                "\nThere is no Unreleased section and no prerelease heading: a "
                "prerelease is a draft of its base version, and the renderer folds "
                "it there, so its own heading only puts a build stamp in the "
                "reader's archive. An [Unreleased] section is worse -- it folds onto "
                "nothing, so the Releases page shows the running version with no "
                "notes and hangs the whole body off a row at the bottom.\n"
                "To see what is pending instead: git log --oneline <last-tag>..HEAD\n"
                'See AGENTS.md -> "Release Changelog".',
                file=sys.stderr,
            )
            return 1

    base_ref = os.environ.get("CHANGELOG_BASE_REF", "").strip()
    if not base_ref:
        print(
            "changelog-history: no draft sections OK; no CHANGELOG_BASE_REF, so "
            "nothing to compare shipped history against (set it to enforce, e.g. "
            "CHANGELOG_BASE_REF=origin/main)"
        )
        return 0

    base_text = _git_show(base_ref, CHANGELOG)
    if base_text is None:
        print(
            f"changelog-history: {CHANGELOG} does not exist at {base_ref}; "
            f"nothing shipped to protect"
        )
        return 0
    if head_text is None:
        print(
            f"::error::{CHANGELOG} is missing at head but exists at {base_ref}. "
            f"The changelog is append-only history and cannot be deleted.",
            file=sys.stderr,
        )
        return 1

    # Parse base history with the BASE's grammar, not this checkout's. A PR may
    # legitimately change changelog.py; if it narrows the heading regex, parsing
    # base text with the narrowed grammar drops those releases out of the protected
    # set and a deletion in the same PR passes unseen. The base ref is the merge
    # target, so its renderer is the authority on what the base documents.
    base_grammar = HEAD_GRAMMAR
    base_renderer_src = _git_show(base_ref, RENDERER_PATH)
    if base_renderer_src is None:
        print(
            f"changelog-history: note: {RENDERER_PATH} does not exist at {base_ref}; "
            f"parsing base history with this checkout's grammar. The mis-parse guard "
            f"below is what keeps that fail-closed."
        )
    else:
        try:
            base_grammar = grammar_of(_load_renderer_source(base_renderer_src))
        except Exception as exc:
            print(
                f"::error::changelog-history: could not load {RENDERER_PATH} from "
                f"{base_ref} ({exc}). Refusing to parse shipped history with a "
                f"grammar that may not be the one it was written under.",
                file=sys.stderr,
            )
            return 1

    problems = compare(base_text, head_text, base_grammar, HEAD_GRAMMAR)
    if problems:
        for problem in problems:
            print(f"::error::changelog-history: {problem}", file=sys.stderr)
        print(
            "\nIf a shipped section genuinely must change (a factual "
            "correction), say so explicitly in the PR body — but prefer leaving "
            'shipped history alone. See AGENTS.md -> "Release Changelog".',
            file=sys.stderr,
        )
        return 1

    kept = len(parse_sections(base_text, base_grammar))
    print(f"changelog-history: {kept} shipped section(s) at {base_ref} are intact at head OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
