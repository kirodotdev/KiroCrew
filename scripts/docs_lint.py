#!/usr/bin/env python3
"""docs-lint — keep the documentation trees navigable and their indexes honest.

Stdlib only, no third-party deps, cross-platform. Run from the repo root::

    python3 scripts/docs_lint.py            # lint
    python3 scripts/docs_lint.py --test      # self-test the checks themselves

Exit 0 = clean, exit 1 = findings, exit 2 = usage/environment error.

Why this gate exists
--------------------
Documentation rots in three specific ways that a human reviewer reliably misses
and a machine catches for free:

1. **Dangling links.** A doc is moved or merged and the links pointing at it are
   never updated, so the reader hits a 404 on GitHub.
2. **Unreachable docs.** A file is added but never linked from its directory
   index, so nobody (human or AI) finds it and it silently goes stale.
3. **Phantom specs.** Code and comments cite a spec path that does not exist —
   the reference reads as authoritative while pointing at nothing. This repo
   accumulated several such citations, including a "frozen contract" module
   whose spec and conformance-gate docs were never ported.

Checks 1-3 are the structural invariants behind the repository rule that a code
change must also update the docs and the indexes. The rule is only real if a
machine enforces it.

The fourth check guards the other direction: some documentation filenames are an
API. ``src/kiro_crew/docs/*.md`` is packaged and read at runtime, and specific
filenames are hardcoded in Python and TypeScript. Renaming one of those without
updating its consumers breaks a shipped feature rather than a link.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ── What we scan ────────────────────────────────────────────────────────────────

# Documentation roots, each with the index file that must reach every doc in it.
# ``docs/`` is repo-only contributor/architecture material; ``src/kiro_crew/docs/``
# is PACKAGED end-user material (see MANIFEST.in) and is read at runtime.
DOC_ROOTS: tuple[str, ...] = (
    "docs",
    "src/kiro_crew/docs",
    "website/docs",
)

# Per-directory index filenames, in priority order. A directory is "indexed" by
# whichever of these it contains; README.md is preferred because it is what
# GitHub renders when a reader browses to the directory.
INDEX_NAMES: tuple[str, ...] = ("README.md", "index.md")

# Extra markdown files that participate in link checking but are not themselves
# required to be reachable from a doc index (they ARE the entry points).
ENTRY_POINT_DOCS: tuple[str, ...] = (
    "README.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "GOVERNANCE.md",
    "MAINTAINERS.md",
    "TENETS.md",
    "website/AGENTS.md",
    "website/README.md",
    "skills/README.md",
)

# Trees excluded from reachability: archives and vendored/example material are
# deliberately not curated. They are still link-checked.
#
# ``docs/task-specs/`` is archival by repository convention (AGENTS.md), and
# ``docs/kiro-cli/`` is a vendored copy of upstream documentation.
UNCURATED_PREFIXES: tuple[str, ...] = (
    "docs/task-specs/",
    "docs/archive/",
    # Example app trees are curated by their own app README, and a SKILL.md is a
    # skill definition rather than documentation, so a leaf skill directory gets no
    # index of its own.
    "docs/app-kit/examples/",
)

# Directories that legitimately hold docs without their own index: a vendored
# mirror's leaf pages are indexed by the mirror's top-level README.
_NO_INDEX_REQUIRED: frozenset[str] = frozenset({"docs/reference/kiro-cli/reference"})

# Directories never walked, matched by NAME anywhere in the tree. Every entry here
# is a tool-generated or vendored directory that cannot legitimately hold authored
# documentation, so a name match is safe.
#
# Deliberately NOT listed: "build" and "dist". `docs/build/` is a real
# documentation directory (packaging and release docs), and a name-based skip made
# its four files invisible to every check while the summary still reported success.
# Artifact trees are excluded by path below instead.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "_vendor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
    }
)

# Build-artifact trees, matched by repo-relative PATH so a directory that merely
# shares a name with one is still scanned.
SKIP_DIR_PATHS: frozenset[str] = frozenset(
    {
        "build",
        "dist",
        "website/build",
        "website/dist",
        "src/kiro_crew/static/dist",
    }
)

# Source trees scanned for citations of documentation paths. Broad on purpose: a
# stale pointer is just as misleading in an agent-facing SKILL.md or an Electron
# source file as in the backend, and those trees were where the stale ones hid.
CODE_ROOTS: tuple[str, ...] = (
    "src",
    "website/src",
    "website/electron",
    "website/scripts",
    "scripts",
    "skills",
    "test",
    "transfer",
    "packaging",
    ".github",
)
CODE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".ts", ".tsx", ".js", ".mjs", ".yml", ".yaml", ".sh", ".md", ".cfg", ".toml"}
)

# External repositories whose own docs/ layout is cited from our source. A path
# qualified with one of these names is a correct cross-repo reference, not a dangling
# link into this tree.
_EXTERNAL_REPO_MARKERS: tuple[str, ...] = ("KiroCrewPublishCDK", "electron.git")

# ── Code-coupled documentation filenames ───────────────────────────────────────
#
# Each entry: the packaged doc, and the consumer that hardcodes its name. These
# cannot be renamed or deleted without editing the consumer in the same commit.
# The check is deliberately data-driven rather than a grep, so that adding a
# coupling is a one-line change here and is impossible to forget silently.
CODE_COUPLED_DOCS: dict[str, tuple[str, ...]] = {
    "src/kiro_crew/docs/discord-integration.md": ("website/src/pages/settings/DiscordPanel.tsx",),
    "src/kiro_crew/docs/slack-integration.md": ("website/src/pages/settings/SlackPanel.tsx",),
    "src/kiro_crew/docs/teams-integration.md": ("website/src/pages/settings/TeamsPanel.tsx",),
    "src/kiro_crew/docs/telegram-integration.md": ("website/src/pages/settings/TelegramPanel.tsx",),
    "src/kiro_crew/docs/webex-integration.md": ("website/src/pages/settings/WebexPanel.tsx",),
    "src/kiro_crew/docs/wecom-integration.md": ("website/src/pages/settings/WeComPanel.tsx",),
    "src/kiro_crew/docs/weixin-integration.md": ("website/src/pages/settings/WeixinPanel.tsx",),
    "docs/architecture/security-deep-dive.md": ("website/src/pages/settings/SecurityPanel.tsx",),
    "website/docs/theming-contract.md": ("website/scripts/check-theme-colors.mjs",),
}

# The tips catalog scans ``src/kiro_crew/docs/*.md`` but only surfaces docs named
# in this allowlist module; every allowlisted name must therefore still resolve.
TIPS_ALLOWLIST_MODULE = "src/kiro_crew/tips_allowlist.py"

# Markdown inline/reference links and images: [text](target) and ![alt](target).
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)")
# Raw HTML anchors and images. GitHub renders inline HTML in markdown, and the
# repository README uses <a href="..."> badges for its most prominent links, so a
# markdown-only scan misses exactly the links most readers click first.
_HTML_LINK_RE = re.compile(r"""<(?:a|img)\s[^>]*?(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
# Fenced code blocks and inline code spans are stripped before link extraction:
# a bracket-paren pair inside code is example text, not a link. Real cases in
# this repo are a table documenting how `[label](url)` is spoken aloud, and a
# redaction example rendered as `k[REDACTED: credential](raw)`.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
# A documentation path cited from source code, e.g. ``docs/system-specs/x.md``.
_CODE_DOC_REF_RE = re.compile(r"(?:website/)?docs/[A-Za-z0-9][A-Za-z0-9/_.-]*\.md")

# Citations that look like doc paths but are not references to THIS repo's docs:
# upstream project paths, and test fixture data that merely contains a filename.
_CODE_REF_IGNORE_SUBSTRINGS: tuple[str, ...] = (
    # Electron's own repository layout, cited to explain an accelerator string.
    "docs/api/accelerator.md",
)
_CODE_REF_IGNORE_PATH_PARTS: tuple[str, ...] = (
    # Review-bot fixtures embed arbitrary diff paths as test DATA.
    "code_review_sage/tests/",
    # This linter documents the paths it couples to and plants deliberately
    # missing ones in its self-test; scanning itself would report both as real.
    "scripts/docs_lint.py",
)

# A doc path is a CITATION when it appears in a comment or docstring, and DATA when
# it appears in executable code: a test builds fake filesystem paths and simulated
# `git diff` output, and flagging those would train a maintainer to ignore the gate.
# Requiring a prose marker on the line separates the two without parsing, and errs
# toward silence, which is the right direction for a gate that must stay trusted.
_CITATION_MARKER_RE = re.compile(
    r"(?:^\s*[#*]|//|/\*|\"\"\"|'''|`|\bSee\b|\bSpec\b|\bDesign\b|\bdocs?:)",
)


# Hand-maintained "when did this change" preambles in a doc's PROSE. Git already
# records this, and these drift: one spec claimed a date 70 days older than its last
# real edit, which tells a reader the doc is stale when it is not (or the reverse).
#
# Structured YAML frontmatter is exempt and deliberately so: the RFC tree carries a
# real `status:` lifecycle vocabulary there, which is metadata a reader acts on, not
# a changelog. Only the body is checked.
_CHANGELOG_LINE_RE = re.compile(
    r"^\s*(?:last updated|latest amendment|last amended|revision)\s*:",
    re.I,
)
# How far into a doc a changelog preamble can hide before the first section.
_PREAMBLE_SCAN_LINES = 40


@dataclass
class Findings:
    """Accumulated lint findings, grouped by check."""

    broken_links: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    phantom_refs: list[str] = field(default_factory=list)
    coupling: list[str] = field(default_factory=list)
    missing_index: list[str] = field(default_factory=list)
    changelog_preamble: list[str] = field(default_factory=list)

    def total(self) -> int:
        return (
            len(self.broken_links)
            + len(self.unreachable)
            + len(self.phantom_refs)
            + len(self.coupling)
            + len(self.missing_index)
            + len(self.changelog_preamble)
        )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _rel(path: Path, root: Path) -> str:
    """Repo-relative POSIX path, so findings read the same on every OS."""
    return path.relative_to(root).as_posix()


def _prune(root: Path, dirpath: str, dirnames: list[str]) -> None:
    """Drop vendored and build-artifact directories from an ``os.walk`` in place.

    Names are matched anywhere; artifact trees are matched by repo-relative path so
    a real documentation directory that shares a name with one (``docs/build/``) is
    still walked.
    """
    keep = []
    for d in sorted(dirnames):
        if d in SKIP_DIR_NAMES:
            continue
        if _rel(Path(dirpath) / d, root) in SKIP_DIR_PATHS:
            continue
        keep.append(d)
    dirnames[:] = keep


def _walk_markdown(root: Path, subdir: str) -> list[Path]:
    """Every ``*.md`` under ``subdir``, skipping vendored and artifact trees."""
    base = root / subdir
    if not base.is_dir():
        return []
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        _prune(root, dirpath, dirnames)
        for name in sorted(filenames):
            if name.endswith(".md"):
                out.append(Path(dirpath) / name)
    return out


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks, preserving line numbering."""
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def _iter_links(text: str):
    """Yield ``(line_number, raw_target)`` for every markdown and HTML link."""
    for lineno, line in enumerate(_strip_fences(text).splitlines(), start=1):
        # Blank the inline-code spans in place so column-free line numbers stay
        # correct while code examples stop producing findings.
        line = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)
        for match in _HTML_LINK_RE.finditer(line):
            yield lineno, match.group(1).strip()
        for match in _LINK_RE.finditer(line):
            target = match.group(1).strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            yield lineno, target


def _is_external(target: str) -> bool:
    """True for anything not a repo-relative path we can resolve on disk."""
    if not target:
        return True
    lowered = target.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "tel:", "data:", "#")):
        return True
    # Protocol-relative and template placeholders (e.g. `{{ var }}`, `${x}`).
    return lowered.startswith("//") or "{" in target or "$" in target


def _resolve_link(doc: Path, target: str, root: Path) -> Path | None:
    """Resolve a link target to a filesystem path, or None if unresolvable."""
    # Drop the fragment/query; a link to `x.md#section` resolves to `x.md`.
    clean = target.split("#", 1)[0].split("?", 1)[0]
    if not clean:
        return None  # pure fragment — same-document anchor
    if clean.startswith("/"):
        # Root-relative links are resolved against the repo root.
        return (root / clean.lstrip("/")).resolve()
    return (doc.parent / clean).resolve()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _is_uncurated(rel: str) -> bool:
    return rel.startswith(UNCURATED_PREFIXES)


def _index_for_dir(directory: Path) -> Path | None:
    """The index file governing ``directory``, if it has one."""
    for name in INDEX_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


# ── Checks ─────────────────────────────────────────────────────────────────────


def check_links(root: Path, docs: list[Path], findings: Findings) -> None:
    """Every internal markdown link must resolve to a file that exists."""
    for doc in docs:
        rel_doc = _rel(doc, root)
        for lineno, target in _iter_links(_read(doc)):
            if _is_external(target):
                continue
            resolved = _resolve_link(doc, target, root)
            if resolved is None or resolved.exists():
                continue
            findings.broken_links.append(f"{rel_doc}:{lineno} -> {target}")


def check_reachability(root: Path, docs: list[Path], findings: Findings) -> None:
    """Every curated doc must be linked from an index in its own directory tree.

    A doc is reachable when the index of its own directory links to it, or -- for
    a subdirectory that has no index of its own -- an ancestor index within the
    same documentation root links to it. That keeps flat directories honest
    without forcing an index file into every leaf directory.
    """
    # Map: index file -> set of resolved paths it links to.
    index_targets: dict[Path, set[Path]] = {}
    for doc in docs:
        if doc.name not in INDEX_NAMES:
            continue
        targets: set[Path] = set()
        for _lineno, target in _iter_links(_read(doc)):
            if _is_external(target):
                continue
            resolved = _resolve_link(doc, target, root)
            if resolved is not None:
                targets.add(resolved)
        index_targets[doc.resolve()] = targets

    # An entry-point doc can also confer reachability (the root README is the
    # top of the documentation hierarchy).
    for name in ENTRY_POINT_DOCS:
        entry = root / name
        if not entry.is_file():
            continue
        resolved_entry = entry.resolve()
        if resolved_entry in index_targets:
            continue
        targets = set()
        for _lineno, target in _iter_links(_read(entry)):
            if _is_external(target):
                continue
            resolved = _resolve_link(entry, target, root)
            if resolved is not None:
                targets.add(resolved)
        index_targets[resolved_entry] = targets

    linked: set[Path] = set()
    for targets in index_targets.values():
        linked |= targets

    for doc in docs:
        rel_doc = _rel(doc, root)
        if doc.name in INDEX_NAMES or _is_uncurated(rel_doc):
            continue
        if doc.resolve() in linked:
            continue
        findings.unreachable.append(rel_doc)


def check_directory_indexes(root: Path, docs: list[Path], findings: Findings) -> None:
    """Every directory holding curated docs must carry a human-readable index."""
    dirs_with_docs: set[Path] = set()
    for doc in docs:
        rel_doc = _rel(doc, root)
        if _is_uncurated(rel_doc):
            continue
        dirs_with_docs.add(doc.parent)

    for directory in sorted(dirs_with_docs):
        if _rel(directory, root) in _NO_INDEX_REQUIRED:
            continue
        # A directory whose only markdown IS its index needs nothing more.
        if _index_for_dir(directory) is None:
            findings.missing_index.append(
                f"{_rel(directory, root)}/ has no {' or '.join(INDEX_NAMES)}"
            )


def check_changelog_preambles(root: Path, docs: list[Path], findings: Findings) -> None:
    """No doc may open with a hand-maintained "Last Updated" style changelog.

    Git is the changelog. A date maintained by hand goes stale silently and then
    misrepresents how fresh the document is.
    """
    for doc in docs:
        rel_doc = _rel(doc, root)
        if _is_uncurated(rel_doc):
            continue
        lines = _read(doc).splitlines()
        body_start = 0
        if lines and lines[0].strip() == "---":
            # Skip YAML frontmatter; its keys are metadata, not prose.
            for i, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    body_start = i + 1
                    break
        window = lines[body_start : body_start + _PREAMBLE_SCAN_LINES]
        for offset, line in enumerate(window, start=body_start + 1):
            lineno = offset
            if _CHANGELOG_LINE_RE.match(line):
                findings.changelog_preamble.append(f"{rel_doc}:{lineno}  {line.strip()[:60]}")
                break


def check_code_citations(root: Path, findings: Findings) -> None:
    """A documentation path cited from source code must exist ("phantom spec").

    A comment or docstring that names a spec is a promise to the reader. When the
    file does not exist the citation is worse than absent: it looks authoritative
    while pointing at nothing.
    """
    for code_root in CODE_ROOTS:
        base = root / code_root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            _prune(root, dirpath, dirnames)
            for name in sorted(filenames):
                if Path(name).suffix not in CODE_SUFFIXES:
                    continue
                path = Path(dirpath) / name
                rel_path = _rel(path, root)
                if any(part in rel_path for part in _CODE_REF_IGNORE_PATH_PARTS):
                    continue
                try:
                    text = _read(path)
                except OSError:
                    continue
                if path.suffix == ".md":
                    # In a markdown file a fenced block is sample output, not a
                    # citation (e.g. a doc showing what a source listing looks like).
                    text = _strip_fences(text)
                for lineno, line in enumerate(text.splitlines(), start=1):
                    # A line naming another repository is citing that repo's layout.
                    if any(m in line for m in _EXTERNAL_REPO_MARKERS):
                        continue
                    # Only prose (comment/docstring) lines carry citations.
                    if not _CITATION_MARKER_RE.search(line):
                        continue
                    for match in _CODE_DOC_REF_RE.finditer(line):
                        ref = match.group(0)
                        if any(ig in ref for ig in _CODE_REF_IGNORE_SUBSTRINGS):
                            continue
                        # A citation may be written relative to the repo root or,
                        # inside the package, relative to the package itself.
                        if (
                            (root / ref).exists()
                            or (root / "src" / "kiro_crew" / ref).exists()
                            or (root / "website" / ref).exists()
                        ):
                            continue
                        findings.phantom_refs.append(f"{rel_path}:{lineno} -> {ref}")


def check_code_coupled_docs(root: Path, findings: Findings) -> None:
    """Docs whose filenames are hardcoded in code must still exist.

    ``src/kiro_crew/docs/`` is packaged and read at runtime; specific filenames
    are baked into TypeScript URL constants and into the tips allowlist. Renaming
    one is a code change, not a docs change.
    """
    for doc, consumers in sorted(CODE_COUPLED_DOCS.items()):
        if (root / doc).is_file():
            continue
        # The coupling only binds while a consumer is still there to cite the
        # doc. If the consumer itself was removed, the pair retired together and
        # the absent doc is not a finding.
        live = [c for c in consumers if (root / c).is_file()]
        if not live:
            continue
        findings.coupling.append(f"{doc} is missing but hardcoded in: {', '.join(live)}")

    allowlist = root / TIPS_ALLOWLIST_MODULE
    if allowlist.is_file():
        packaged = root / "src" / "kiro_crew" / "docs"
        for match in re.finditer(r'"([A-Za-z0-9][A-Za-z0-9._-]*\.md)"', _read(allowlist)):
            name = match.group(1)
            if not (packaged / name).is_file():
                findings.coupling.append(
                    f"src/kiro_crew/docs/{name} is missing but listed in "
                    f"{TIPS_ALLOWLIST_MODULE} (TIP_DOC_ALLOWLIST)"
                )


# ── Reporting ──────────────────────────────────────────────────────────────────


def _emit(title: str, items: list[str], hint: str) -> None:
    if not items:
        return
    print(f"\nFAIL: {title} ({len(items)})")
    for item in items[:40]:
        print(f"  - {item}")
    if len(items) > 40:
        print(f"  ... and {len(items) - 40} more")
    print(f"  -> {hint}")


def run(root: Path) -> Findings:
    """Run every check against ``root`` and return the accumulated findings."""
    docs: list[Path] = []
    for doc_root in DOC_ROOTS:
        docs.extend(_walk_markdown(root, doc_root))

    findings = Findings()
    # Entry points are link-checked too, and they matter most: AGENTS.md is the
    # router every session loads, so a dead pointer there misroutes the reader
    # before any doc gets a chance to.
    entry_points = [root / name for name in ENTRY_POINT_DOCS if (root / name).is_file()]
    check_links(root, docs + entry_points, findings)
    check_reachability(root, docs, findings)
    check_directory_indexes(root, docs, findings)
    check_changelog_preambles(root, docs, findings)
    check_code_citations(root, findings)
    check_code_coupled_docs(root, findings)
    return findings


def _report(findings: Findings, doc_count: int) -> int:
    print(f"docs-lint: scanned {doc_count} markdown files under {', '.join(DOC_ROOTS)}")
    _emit(
        "broken internal links",
        findings.broken_links,
        "fix the link, or restore/redirect the target",
    )
    _emit(
        "docs not reachable from any index",
        findings.unreachable,
        "link the doc from its directory README.md, or delete the doc",
    )
    _emit(
        "directories with docs but no index",
        findings.missing_index,
        "add a README.md that indexes the directory",
    )
    _emit(
        "hand-maintained changelog preambles",
        findings.changelog_preamble,
        "delete the line; git records when a doc changed",
    )
    _emit(
        "documentation paths cited from code that do not exist",
        findings.phantom_refs,
        "write the missing doc, or correct the citation",
    )
    _emit(
        "code-coupled docs missing",
        findings.coupling,
        "restore the file, or update its consumer in the same commit",
    )
    if findings.total() == 0:
        print("\nAll documentation checks passed")
        return 0
    print(f"\n{findings.total()} finding(s) — see docs/README.md for the docs rules")
    return 1


# ── Self-test ──────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Plant a defect per check and assert the check catches it.

    A gate nobody has proven can fail is a gate that silently passes forever.
    """
    failures = 0

    def probe(label: str, build) -> None:
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            # A minimal healthy tree: an index that links its one doc.
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            expected = build(root)
            findings = run(root)
            got = getattr(findings, expected)
            if got:
                print(f"  ok  {label} detected")
            else:
                print(f"  FAIL {label} NOT detected")
                failures += 1

    def clean_probe() -> None:
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            findings = run(root)
            if findings.total() == 0:
                print("  ok  healthy tree reports clean")
            else:
                print(f"  FAIL healthy tree reported {findings.total()} finding(s)")
                failures += 1

    def plant_broken_link(root: Path) -> str:
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee [gone](nope.md).\n", encoding="utf-8")
        return "broken_links"

    def plant_broken_html_link(root: Path) -> str:
        # GitHub renders inline HTML, and the repo README uses <a href> badges for
        # its most prominent links, so these must be checked too.
        (root / "docs" / "ok.md").write_text(
            '# Ok\n\n<a href="nope.md"><img src="x.svg" alt="badge"></a>\n', encoding="utf-8"
        )
        return "broken_links"

    def plant_unreachable(root: Path) -> str:
        (root / "docs" / "orphan.md").write_text("# Orphan\n\nBody.\n", encoding="utf-8")
        return "unreachable"

    def plant_missing_index(root: Path) -> str:
        sub = root / "docs" / "sub"
        sub.mkdir()
        (sub / "page.md").write_text("# Page\n\nBody.\n", encoding="utf-8")
        # Link it so the finding is specifically the absent index.
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Page](sub/page.md)\n", encoding="utf-8"
        )
        return "missing_index"

    def plant_changelog_preamble(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nLast Updated: 2026-01-01\n\nBody.\n", encoding="utf-8"
        )
        return "changelog_preamble"

    def plant_phantom_ref(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "mod.py").write_text(
            '"""Spec: ``docs/system-specs/modules/ghost.md``."""\n', encoding="utf-8"
        )
        return "phantom_refs"

    def plant_coupling(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "tips_allowlist.py").write_text(
            'TIP_DOC_ALLOWLIST = frozenset({"vanished.md"})\n', encoding="utf-8"
        )
        return "coupling"

    def code_immunity_probe(label: str, body: str) -> None:
        """Assert a link written inside code markup is NOT reported."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text(body, encoding="utf-8")
            if run(root).broken_links:
                print(f"  FAIL {label} was flagged")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    print("Running docs-lint self-test...")
    clean_probe()
    probe("broken link", plant_broken_link)
    probe("broken HTML anchor", plant_broken_html_link)
    probe("unreachable doc", plant_unreachable)
    probe("missing directory index", plant_missing_index)
    probe("changelog preamble", plant_changelog_preamble)
    probe("phantom spec citation", plant_phantom_ref)
    probe("code-coupled doc missing", plant_coupling)

    # Code-markup immunity is an inverse assertion (nothing should fire).
    code_immunity_probe("fenced example link", "# Ok\n\n```md\n[example](does-not-exist.md)\n```\n")
    code_immunity_probe("inline-code example link", "# Ok\n\nSpoken as `[label](url)` aloud.\n")

    if failures:
        print(f"\nSELF-TEST FAILED: {failures} check(s) do not fire")
        return 1
    print("\nSelf-test passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint the Kiro Crew documentation trees and their indexes."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="self-test the checks (plant a defect per check, assert it fires)",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="repo root to lint (default: the parent of this script's directory)",
    )
    args = parser.parse_args(argv)

    if args.test:
        return _self_test()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if not (root / "docs").is_dir():
        print(f"docs-lint: no docs/ directory under {root}", file=sys.stderr)
        return 2

    findings = run(root)
    doc_count = sum(len(_walk_markdown(root, r)) for r in DOC_ROOTS)
    return _report(findings, doc_count)


if __name__ == "__main__":
    sys.exit(main())
