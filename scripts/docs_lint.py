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
4. **Stale line citations.** A doc points at ``session.py:3356`` of a file that
   now has 2520 lines. Source moves every day and the citation does not, so it
   sends the reader to nothing while reading as precise — and when it overshoots
   the end of the file it is usually because the code moved to another module
   entirely, which is the part the doc most needs to say.
5. **Phantom code.** A module spec names a source file that exists nowhere. The
   spec claims to describe code that is there now, so an unresolvable path is
   either a rename it missed or a dependency that left the tree — one of these
   was still explaining why the editor uses Monaco, which had been deleted
   outright. Checked only in ``docs/system-specs/modules/``: an RFC or a
   migration design names files precisely BECAUSE they do not exist yet.

Checks 1-5 are the structural invariants behind the repository rule that a code
change must also update the docs and the indexes. The rule is only real if a
machine enforces it.

The sixth check guards the other direction: some documentation filenames are an
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
_EXTERNAL_REPO_MARKERS: tuple[str, ...] = (
    "KiroCrewPublishCDK",
    "electron.git",
    # The app catalog's publisher. Its distribution contract is documented in
    # that repo, and the client cites it to explain the base URL it fetches.
    "KiroCrewApps",
)

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
# Git conflict markers, as git itself writes them at column 0: the ``<``, ``|``
# and ``>`` forms carry a trailing label, the ``=`` separator stands alone.
_CONFLICT_MARKER_RE = re.compile(r"^(?:[<>|]{7}(?:\s|$)|={7}$)")
# A documentation path cited from source code, e.g. ``docs/system-specs/x.md``.
_CODE_DOC_REF_RE = re.compile(r"(?:website/)?docs/[A-Za-z0-9][A-Za-z0-9/_.-]*\.md")

# A SOURCE path cited from documentation -- the opposite direction from
# ``_CODE_DOC_REF_RE`` -- optionally with a line or line range:
# ``acp/types.py``, ``session.py:300``, ``sandbox.py:2252-2262``, ``kiro.py:80,90``.
# Only inside backticks: a bare path in prose is usually a sentence about a
# directory, and the backticks are what make it a citation.
_SOURCE_CITE_RE = re.compile(
    r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|ts|tsx|js|jsx|mjs|yml|yaml|json|sh|toml|cfg))"
    r"(?::(\d+(?:[-,]\d+)*))?`"
)

# A line number long enough to be nothing but noise. CPython caps int(str) at
# 4300 digits and raises ValueError past it, so an absurd citation in a fork PR's
# doc would abort the whole gate rather than be reported. A real file has fewer
# than ten digits of lines; anything longer is not a citation to adjudicate, so it
# is reported as malformed and never converted.
_MAX_LINE_DIGITS = 9

# Trees a cited source path may live in. Deliberately NOT a list of prefixes to
# join the citation onto: a doc cites a source file relative to whatever root its
# reader is standing in -- the package (``acp/types.py``), the repo
# (``scripts/x.py``), the website bundle (``apps/builtinRegistry.ts``, really under
# ``website/src/``), an app's own root (``providers/pagerduty.py``, really under
# ``apps/builtins/ops_mission_control/backend/``) or a skill's
# (``scripts/reaper.sh``). Those roots cannot be enumerated, so the citation is
# matched as a path SUFFIX against the files that actually exist here, and only a
# path matching NOTHING is reported.
_SOURCE_CITE_TREES: tuple[str, ...] = (
    "src",
    "website",
    "scripts",
    "test",
    "packaging",
    ".github",
    # The docs site's own tree. Its `package-lock.json` is git-tracked and was
    # reported purely because this list did not name the tree -- the tell was its
    # two sibling citations resolving fine under `website`. Adding the tree is the
    # fix; allowlisting the ref would have permanently silenced rot on a live file.
    "site",
    # Docs cite each other's committed assets: `governance.md` names
    # `docs/guides/assets/security-policy.example.json`, which exists and is even a
    # working relative markdown link. Same reasoning as `site` -- a missing tree is
    # a resolver bug, not a citation to excuse.
    "docs",
)

# Docs where an UNRESOLVABLE source path is a finding. Scoped by GENRE, and that
# is the whole design: what separates a stale citation from a correct
# unresolvable one is not the path's shape but what the document is FOR.
#
# A module spec describes code that exists now, so a path it names and cannot be
# found is rot. An RFC, a plan and a migration design name files precisely
# BECAUSE they do not exist -- `browser/setup.py` sits in a "what is deleted"
# table, `notifications/bridge.py` sits beside the words "zero implementation
# code exists" -- and an app-kit guide names files in the READER's project
# (`app.json`, `ui/src/App.tsx`).
#
# A shape-based allowlist provably cannot draw that line. Silencing those four
# deletion-table entries needs `browser/**`, and that same pattern suppresses
# four of the real findings. Only WHICH DOC separates them.
#
# Measured on the tree this shipped against: unscoped, 38 findings of which 11
# were real (27 false). Scoped here: 19 findings, the same 11 real, and the 8
# residuals are the three refs below.
_PATH_CITE_DOC_PREFIXES: tuple[str, ...] = ("docs/system-specs/modules/",)

# Refs that cannot resolve here and are still correct. EXACT refs, not patterns,
# so a new unresolvable path is reported rather than absorbed by a wildcard --
# each of these was traced to the file it really names before it was listed.
_UNRESOLVABLE_REF_OK: frozenset[str] = frozenset(
    {
        # -- This crew's own RUNTIME data, under ~/.kiro/crew. Written by the code
        #    that resolves each path; never present in a checkout.
        #
        # ops-mission-control's app data dir. `store.py`'s `incidents_dir()` /
        # `index_path()` join `app_data_dir(APP_NAME)` from `apps/manager.py`.
        "data/config.json",
        "incidents/index.json",
        # The same two files spelled from the home root, in security.py's
        # write-protect table (`_CREW_HOME_PREFIXES` + the literal suffix).
        "apps/ops-mission-control/data/rotation.yaml",
        "apps/ops-mission-control/data/incidents/index.json",
        # spec-builder's trust keystone: `config_dir() / "trust" / ...`.
        "trust/spec-builder-decisions.json",
        # Optional dev-only override read from KIROCREW_PROJECT_DIR by
        # `agent.py`'s `_shipped_defaults()`; the shipped file is
        # `config/defaults.json`, which resolves.
        "agents/defaults.json",
        #
        # -- FOREIGN homes. `onboarding_import.py` reads other tools' config dirs
        #    to offer an import, so naming their layout is the point.
        #
        # Antigravity / Gemini (`~/.gemini`, `_GEMINI_CONFIG_RELATIVE_PATHS`).
        # All three spellings are probed, because Antigravity is closed-source and
        # its subpath moved between releases; the first is the live one.
        "config/mcp_config.json",
        "antigravity/mcp_config.json",
        "antigravity-cli/settings.json",
        # Hermes Agent (`~/.hermes`, `_scan_hermes`), OpenClaw
        # (`OPENCLAW_STATE_DIR`), and any registered install descended from
        # Kiro Crew (`_scan_lineage_install`) -- all three name this relative path.
        "cron/jobs.json",
        #
        #
        # -- GENERATED or VENDORED at build/install time. Present in a wheel or a
        #    provisioned install, never in a checkout, and each is gitignored.
        #
        # Stamped by `scripts/stamp-distribution.sh` during packaging; imported
        # through a try/except ImportError precisely because a checkout lacks it.
        "kiro_crew/_build_info.py",
        # Bundle inside the vendored npm package @agentclientprotocol/claude-agent-acp,
        # resolved out of the gitignored node_modules by `acp/client.py`.
        "dist/acp-agent.js",
        # playwright-core's own browser registry, inside the npm dependency.
        "playwright-core/browsers.json",
        # Relative to ENGINE_ROOT -- `engine_root()` is
        # ~/.kiro/crew/apps/pptx-maker/data/vendor/sdpm, a checkout `provision.py`
        # installs at runtime. Two backend comments cite it the same way.
        "skill/sdpm/api.py",
        #
        # -- ANOTHER REPOSITORY.
        #
        # The `@kiro/agent` package's own entry point, and a sibling package in
        # that same repo; the citing doc says "Confirmed against @kiro/agent source".
        "src/index.ts",
        "packages/acp-type-covenant/capabilities/auth/get-access-token.ts",
        # A bug-repro test the auto-improvement spine tells the agent to CREATE in
        # the external target repo under test (github.com/Zedmor/chess_test), which
        # keeps its suite in `tests/` plural. Rewriting it to this repo's `test/`
        # would make the sentence false.
        "tests/test_bug_src_search_py_negamax_root.py",
    }
)


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
    stale_line_cites: list[str] = field(default_factory=list)
    phantom_source_paths: list[str] = field(default_factory=list)
    coupling: list[str] = field(default_factory=list)
    missing_index: list[str] = field(default_factory=list)
    changelog_preamble: list[str] = field(default_factory=list)
    conflict_markers: list[str] = field(default_factory=list)

    def total(self) -> int:
        return (
            len(self.broken_links)
            + len(self.unreachable)
            + len(self.phantom_refs)
            + len(self.stale_line_cites)
            + len(self.phantom_source_paths)
            + len(self.coupling)
            + len(self.missing_index)
            + len(self.changelog_preamble)
            + len(self.conflict_markers)
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


def _is_regular_file(path: Path) -> bool:
    """A real file in THIS tree -- never a symlink, whatever it points at.

    This gate walks a tree a fork PR controls, and every file it lists is a file
    it may later read in full. A symlink is therefore an arbitrary read primitive:
    ``src/kiro_crew/evil.py -> /dev/zero`` plus a citation naming it makes the read
    never finish, and a symlink into a credential path makes it exfiltration. Note
    ``Path.is_file()`` FOLLOWS symlinks, so the symlink test must be explicit.

    Applied at BOTH walks. The markdown walk has the identical hole (a symlinked
    ``docs/evil.md``) and it feeds every other check in this file, so guarding only
    the citation index would leave the same hazard one filename away.
    """
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        # A broken or recursive link raises rather than answering; that is a no.
        return False


def _walk_markdown(root: Path, subdir: str) -> list[Path]:
    """Every ``*.md`` under ``subdir``, skipping vendored and artifact trees."""
    base = root / subdir
    if not base.is_dir():
        return []
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        _prune(root, dirpath, dirnames)
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = Path(dirpath) / name
            if _is_regular_file(path):
                out.append(path)
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


def _source_file_index(root: Path) -> dict[str, list[Path]]:
    """Every source file in the scanned trees, keyed by each of its path suffixes.

    Built once per run and cached on the function, because the citation check asks
    "does any file end with this path" for a few hundred citations and walking the
    trees per citation would make the gate the slowest thing in CI.
    """
    cached = getattr(_source_file_index, "_cache", None)
    if cached is not None and cached[0] == root:
        return cached[1]
    index: dict[str, list[Path]] = {}
    for tree in _SOURCE_CITE_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            _prune(root, dirpath, dirnames)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if not _is_regular_file(path):
                    continue
                parts = _rel(path, root).split("/")
                # Register every suffix, so a citation from any root matches.
                for start in range(len(parts)):
                    index.setdefault("/".join(parts[start:]), []).append(path)
    _source_file_index._cache = (root, index)  # type: ignore[attr-defined]
    return index


def _resolve_source_cite(root: Path, ref: str) -> list[Path]:
    """Files whose path ends with ``ref``. Empty means the citation names nothing.

    More than one match is normal and is not a finding: ``app.json`` is a real
    filename in several builtin apps, and a doc naming it is not wrong just
    because it did not say which one.
    """
    return _source_file_index(root).get(ref.lstrip("./"), [])


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


def check_conflict_markers(root: Path, docs: list[Path], findings: Findings) -> None:
    """No doc may ship a git conflict marker.

    A half-resolved merge is invisible to every other check here, which reads
    documents as a link graph rather than as text, and it survives review for a
    second reason: a bare ``=======`` under a line of prose is a valid setext H1,
    so it renders as a heading instead of erroring. Anchoring at column 0 keeps
    prose that *discusses* markers (they appear mid-line, inside backticks)
    clean, and fenced blocks are exempt so a doc can show a real conflict.

    The ``=`` form is matched only as a bare 7-character line. That is the width
    git writes, and the repo's docs head with ATX ``#`` throughout, so a setext
    underline of exactly that width would be the one false positive; a heading
    wanting an underline should use ``#`` instead.
    """
    # Unlike the style checks, this one does not skip uncurated trees or spare the
    # entry points: a marker is corruption, not a curation question, and the entry
    # points are the files most people edit and therefore the likeliest to conflict.
    for doc in docs:
        rel_doc = _rel(doc, root)
        for lineno, line in enumerate(_strip_fences(_read(doc)).splitlines(), start=1):
            if _CONFLICT_MARKER_RE.match(line):
                findings.conflict_markers.append(f"{rel_doc}:{lineno}  {line.strip()[:40]}")


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


def check_source_citations(root: Path, docs: list[Path], findings: Findings) -> None:
    """A line number a doc cites must be inside the file it names.

    ``check_code_citations`` guards code -> docs. This guards docs -> code, which
    rots faster and more quietly: source moves every day, and a doc that says
    "see ``session.py:3356``" of a 2520-line file sends the reader to nothing while
    reading as precise. Exactly one claim here is decidable without judgement --
    the cited line exists -- and that is the whole check.

    A citation is matched as a path SUFFIX against the files that exist, because a
    doc cites relative to whatever root its reader stands in: the package
    (``acp/types.py``), the repo (``scripts/x.py``), the website bundle
    (``apps/builtinRegistry.ts``, really under ``website/src/``), an app's own root
    (``providers/pagerduty.py``) or a skill's (``scripts/reaper.sh``). Those roots
    cannot be enumerated. Several matches is normal and not a finding: ``app.json``
    is a real filename in two dozen builtin apps, and the citation is wrong only
    when the line is past the end of EVERY candidate.

    Two neighbouring checks were built, measured against the real tree, and left
    OUT -- recorded here so nobody re-adds one believing it was merely forgotten.

    An unresolvable PATH is reported too, but ONLY from a module spec, and that
    scope is the whole design rather than a convenience. Five narrowings were
    measured against the real tree, and each one that keyed on the path's SHAPE
    left a different family of false positives behind:

    ======================================  =======  ====  =====
    rule                                    findings real  false
    ======================================  =======  ====  =====
    raw                                        1960     -      -
    + require the parent directory to exist      48     -      -
    + suffix match from any root                637     -      -
    + require a directory component              78     -      -
    scoped to ``docs/system-specs/modules/``     41    20     21
    ======================================  =======  ====  =====

    What separates a stale citation from a correct unresolvable one is not the
    path's shape but what the DOCUMENT IS FOR. An RFC, a plan and a migration
    design name files precisely because they do not exist -- ``browser/setup.py``
    sits in a "what is deleted" table, ``notifications/bridge.py`` beside the words
    "zero implementation code exists" -- and an app-kit guide names files in the
    READER's project. A module spec describes code that is there now, so a path it
    cannot resolve is rot. A shape-based allowlist provably cannot draw that line:
    silencing those four deletion-table entries needs ``browser/**``, and the same
    pattern suppresses four real findings.

    The 21 residuals inside the scope were each traced to the code that BUILDS the
    path, and they are exact refs in :data:`_UNRESOLVABLE_REF_OK` rather than
    patterns, so a new unresolvable path is reported instead of absorbed. Two of
    them turned out to be resolver bugs rather than exemptions -- ``site/`` and
    ``docs/`` were missing from the scanned trees, and ``site/package-lock.json``
    is git-tracked, so an exact-ref entry there would have silenced rot on a live
    file forever. Those became trees.

    A cited SYMBOL is still not checked, and the reason is structural. A table row
    pairs independent columns -- ``docs/architecture/mcp.md`` lists a server, its
    entry point ``mcp_computer.py``, and its tool names, which really live in
    ``computer_use/cli.py`` -- so same-line adjacency is not a claim about where a
    name is defined. Dropping adjacency to ask only "does this identifier exist
    anywhere" flags every name an RFC proposes: 572 repo-wide, ~100 in one RFC.

    A check whose findings are mostly false trains a maintainer to skim past the
    gate, which costs more than the gap it closes.

    A cited SYMBOL is not checked either. A table row pairs independent columns --
    ``docs/architecture/mcp.md`` lists a server, its entry point
    ``mcp_computer.py``, and its tool names, which really live in
    ``computer_use/cli.py`` -- so same-line adjacency is not a claim about where a
    name is defined. Dropping adjacency to ask only "does this identifier exist
    anywhere" flags every name an RFC proposes: 572 hits repo-wide, ~100 in one
    RFC, nearly all correct writing about code that does not exist yet.

    Both gaps are the same judgement: a check whose findings are mostly false
    trains a maintainer to skim past the gate, which costs more than the gap.

    The line check is honest about its own reach too -- it catches a citation
    pointing PAST the end of a file, never one that has drifted to the wrong line
    inside it. That is the argument for citing a symbol NAME wherever a doc can: a
    name survives the refactor that moves the line.
    """
    for doc in docs:
        rel_doc = _rel(doc, root)
        # NOT gated on `_is_uncurated`. That exemption is about CURATION -- an
        # archive is not required to be indexed or styled -- and it says nothing
        # about whether a citation inside it is true. A reader who opens an
        # archived doc still follows its line numbers. The path class needs no
        # exemption either: its own scope is `docs/system-specs/modules/`, which
        # no uncurated tree is inside.
        paths_must_resolve = rel_doc.startswith(_PATH_CITE_DOC_PREFIXES)
        try:
            text = _read(doc)
        except OSError:
            continue
        # A fenced block is sample code or terminal output, not a citation.
        for lineno, line in enumerate(_strip_fences(text).splitlines(), start=1):
            for match in _SOURCE_CITE_RE.finditer(line):
                ref, lines = match.group(1), match.group(2)
                targets = _resolve_source_cite(root, ref)
                if not targets:
                    if paths_must_resolve and "/" in ref and ref not in _UNRESOLVABLE_REF_OK:
                        findings.phantom_source_paths.append(f"{rel_doc}:{lineno} -> {ref}")
                    continue
                if not lines:
                    continue
                parts = re.split(r"[-,]", lines)
                if any(len(n) > _MAX_LINE_DIGITS for n in parts):
                    # Never converted: `int()` raises past CPython's digit cap, and
                    # an uncaught ValueError here would abort the gate on input a
                    # fork PR controls.
                    findings.stale_line_cites.append(
                        f"{rel_doc}:{lineno} -> {ref}:{lines} names no plausible line"
                    )
                    continue
                cited = max(int(n) for n in parts)
                if cited < 1:
                    # Files are 1-indexed, so `:0` points at nothing. Reported
                    # rather than skipped: it reads as precise and is not.
                    findings.stale_line_cites.append(
                        f"{rel_doc}:{lineno} -> {ref}:{lines} is not a line number "
                        "(files are 1-indexed)"
                    )
                    continue
                # With several candidates the citation is only wrong when the line
                # is past the end of EVERY one of them: any file that is long
                # enough is a reading on which the citation makes sense.
                lengths = {t: len(_read(t).splitlines()) for t in targets}
                if any(total >= cited for total in lengths.values()):
                    continue
                longest = max(lengths, key=lambda t: lengths[t])
                findings.stale_line_cites.append(
                    f"{rel_doc}:{lineno} -> {ref}:{lines} "
                    f"but {_rel(longest, root)} has {lengths[longest]} line(s)"
                )


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
    check_conflict_markers(root, docs + entry_points, findings)
    check_code_citations(root, findings)
    check_source_citations(root, docs, findings)
    check_code_coupled_docs(root, findings)
    return findings


def _report(findings: Findings, doc_count: int) -> int:
    print(f"docs-lint: scanned {doc_count} markdown files under {', '.join(DOC_ROOTS)}")
    _emit(
        "git conflict markers left in docs",
        findings.conflict_markers,
        "finish the merge: keep one side and delete the markers",
    )
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
        "source paths cited from a module spec that do not exist",
        findings.phantom_source_paths,
        "correct the path, or say plainly that the code was deleted",
    )
    _emit(
        "line citations pointing past the end of the file",
        findings.stale_line_cites,
        "cite a symbol name instead: a name survives the refactor that moves the line",
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

    def plant_conflict_marker(root: Path) -> str:
        # The separator alone, which is what a half-finished resolution leaves
        # behind once the labelled <<< and >>> lines have been deleted.
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n- kept bullet\n=======\n- other side\n", encoding="utf-8"
        )
        return "conflict_markers"

    def plant_conflict_marker_head(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
        )
        return "conflict_markers"

    def plant_conflict_marker_uncurated(root: Path) -> str:
        # Archival trees are exempt from the style checks but not from corruption.
        archive = root / "docs" / "archive"
        archive.mkdir()
        (archive / "old.md").write_text("# Old\n\nkept\n=======\nother\n", encoding="utf-8")
        return "conflict_markers"

    def plant_phantom_ref(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "mod.py").write_text(
            '"""Spec: ``docs/system-specs/modules/ghost.md``."""\n', encoding="utf-8"
        )
        return "phantom_refs"

    def plant_phantom_source_path(root: Path) -> str:
        # A module spec naming a file that exists nowhere -- the case the class
        # reports. The doc has to sit in the scoped tree to be checked at all.
        pkg = root / "src" / "kiro_crew" / "acp"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("x = 1\n", encoding="utf-8")
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nThe handler lives in `acp/ghost.py`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def plant_citation_of_a_symlinked_file(root: Path) -> str:
        # A symlink is an arbitrary read primitive in a tree a fork PR controls, so
        # it is never indexed -- which makes a citation naming it UNRESOLVABLE, and
        # in a module spec that is a finding. Pointed at a real file in-tree, so
        # the probe proves the exclusion rather than the hazard.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        try:
            (pkg / "linked.py").symlink_to(pkg / "real.py")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows
            return "phantom_source_paths"
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nSee `kiro_crew/linked.py:900`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def plant_stale_line_cite(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:900`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_absurd_line_number(root: Path) -> str:
        # 4301 digits: past CPython's int(str) cap, so converting it raises and
        # would abort the whole gate on input a fork PR controls.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nSee `small.py:" + "9" * 4301 + "`.\n", encoding="utf-8"
        )
        return "stale_line_cites"

    def plant_line_zero(root: Path) -> str:
        # Files are 1-indexed, so `:0` reads as precise and points at nothing.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:0`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_stale_line_cite_in_an_uncurated_tree(root: Path) -> str:
        # An archive is exempt from CURATION -- indexes, style -- but a reader who
        # opens it still follows its line numbers, so the line check applies.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        archive = root / "docs" / "archive"
        archive.mkdir()
        (archive / "old.md").write_text("# Old\n\nSee `small.py:900`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_stale_line_cite_in_an_rfc(root: Path) -> str:
        # The path exemption above must NOT carry the line check with it: a line
        # number is a claim about code that exists now, even inside a proposal.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        rfc = root / "docs" / "request-for-change"
        rfc.mkdir()
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Rfc](request-for-change/rfc-x.md)\n", encoding="utf-8"
        )
        (rfc / "rfc-x.md").write_text("# Rfc\n\nToday: `small.py:900`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_stale_line_range(root: Path) -> str:
        # The END of a range is what must be inside the file: a range whose start
        # is valid and whose end is past EOF is still a citation into nothing.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:1-40`.\n", encoding="utf-8")
        return "stale_line_cites"

    def plant_coupling(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "tips_allowlist.py").write_text(
            'TIP_DOC_ALLOWLIST = frozenset({"vanished.md"})\n', encoding="utf-8"
        )
        return "coupling"

    def code_immunity_probe(label: str, body: str, field: str = "broken_links") -> None:
        """Assert markup written inside code is NOT reported by ``field``.

        The field is explicit because a probe that watches the wrong one cannot
        fail: it would pass while the check it claims to guard regressed.
        """
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text(body, encoding="utf-8")
            if getattr(run(root), field):
                print(f"  FAIL {label} was flagged")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    def source_cite_immunity_probe(label: str, build) -> None:
        """Assert a legitimate citation shape is NOT reported.

        Each shape here was MEASURED as a false positive on the real tree before
        its exemption existed, so the probe records the evidence rather than a
        hunch -- and pins the exemption so a later widening of the rule fails here
        instead of burying the real findings again.
        """
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            field = build(root)
            if getattr(run(root), field):
                print(f"  FAIL {label} was flagged")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    def allow_unresolvable_path_outside_a_spec(root: Path) -> str:
        # The scope IS the rule: an RFC or a guide names files it proposes or that
        # belong to the reader, so an unresolvable path there is correct writing.
        # Pinned, because widening the class past module specs re-buries the real
        # findings 27:11 (measured).
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nThis adds `acp/planned.py` and reads `cron/jobs.json`.\n",
            encoding="utf-8",
        )
        return "phantom_source_paths"

    def allow_allowlisted_ref_in_a_spec(root: Path) -> str:
        # Inside the scope, an allowlisted ref stays silent -- each entry was
        # traced to the code that builds the path before it was listed.
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nState lives in `cron/jobs.json`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def allow_bare_filename_in_a_spec(root: Path) -> str:
        # A citation with no directory names a runtime or generated file the repo
        # cannot adjudicate; only a path WITH a directory is a checkable claim.
        spec_dir = root / "docs" / "system-specs" / "modules"
        spec_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Spec](system-specs/modules/thing.md)\n",
            encoding="utf-8",
        )
        (spec_dir / "thing.md").write_text(
            "# Thing\n\nThe gateway rewrites `mcp.json`.\n", encoding="utf-8"
        )
        return "phantom_source_paths"

    def allow_symlinked_doc(root: Path) -> str:
        # The markdown walk skips symlinks for the same reason, so a symlinked doc
        # contributes no findings of its own -- it is never opened.
        (root / "docs" / "real-extra.md").write_text(
            "# Extra\n\nSee `nope/ghost.py:900`.\n", encoding="utf-8"
        )
        try:
            (root / "docs" / "linked.md").symlink_to(root / "docs" / "real-extra.md")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows
            pass
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Extra](real-extra.md)\n", encoding="utf-8"
        )
        return "unreachable"

    def allow_fenced_sample(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n```\nsee `small.py:900`\n```\n", encoding="utf-8"
        )
        return "stale_line_cites"

    def allow_in_range_cite(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nSee `small.py:2` and `small.py:1-3`.\n", encoding="utf-8"
        )
        return "stale_line_cites"

    print("Running docs-lint self-test...")
    clean_probe()
    probe("broken link", plant_broken_link)
    probe("broken HTML anchor", plant_broken_html_link)
    probe("unreachable doc", plant_unreachable)
    probe("missing directory index", plant_missing_index)
    probe("changelog preamble", plant_changelog_preamble)
    probe("conflict marker (bare separator)", plant_conflict_marker)
    probe("conflict marker (full three-way)", plant_conflict_marker_head)
    probe("conflict marker (uncurated tree)", plant_conflict_marker_uncurated)
    probe("phantom source path in a module spec", plant_phantom_source_path)
    probe("citation of a symlinked file", plant_citation_of_a_symlinked_file)
    probe("line citation past EOF", plant_stale_line_cite)
    probe("line citation past EOF inside an RFC", plant_stale_line_cite_in_an_rfc)
    probe("line citation in an uncurated tree", plant_stale_line_cite_in_an_uncurated_tree)
    probe("absurd line number (past the int digit cap)", plant_absurd_line_number)
    probe("line zero", plant_line_zero)
    probe("line RANGE ending past EOF", plant_stale_line_range)
    probe("phantom spec citation", plant_phantom_ref)
    probe("code-coupled doc missing", plant_coupling)

    # Code-markup immunity is an inverse assertion (nothing should fire).
    source_cite_immunity_probe(
        "unresolvable path outside a module spec", allow_unresolvable_path_outside_a_spec
    )
    source_cite_immunity_probe("allowlisted ref inside a spec", allow_allowlisted_ref_in_a_spec)
    source_cite_immunity_probe("bare filename inside a spec", allow_bare_filename_in_a_spec)
    source_cite_immunity_probe("symlinked doc is not walked", allow_symlinked_doc)
    source_cite_immunity_probe("fenced sample citation", allow_fenced_sample)
    source_cite_immunity_probe("in-range line citation", allow_in_range_cite)
    code_immunity_probe("fenced example link", "# Ok\n\n```md\n[example](does-not-exist.md)\n```\n")
    code_immunity_probe("inline-code example link", "# Ok\n\nSpoken as `[label](url)` aloud.\n")
    code_immunity_probe(
        "prose discussing conflict markers",
        "# Ok\n\ngit emits `<<<<<<< HEAD` / `=======` /\n`>>>>>>>` around the region.\n",
        "conflict_markers",
    )
    code_immunity_probe(
        "fenced conflict demonstration",
        "# Ok\n\n```diff\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n```\n",
        "conflict_markers",
    )

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
