#!/usr/bin/env python3
"""docs-lint — keep the documentation trees navigable and their indexes honest.

Stdlib only, no third-party deps, cross-platform. Run from the repo root::

    python3 scripts/docs_lint.py             # lint
    python3 scripts/docs_lint.py --test      # self-test the checks themselves
    python3 scripts/docs_lint.py --update-baseline   # prune the fact-check baseline

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

The fact checks, and why they need a baseline
--------------------------------------------
The checks above hold at zero, so they fail outright. A second family asks a
harder question -- is this sentence still TRUE of the code? -- and the honest
answer on a repository this size is "mostly, with a recorded backlog":

* ``path-exists`` -- a backticked repo-anchored source path (``src/**.py``,
  ``scripts/*.py|.sh``, ``website/src/**.ts|.tsx``, ``.github/workflows/*.yml``,
  ``docs/**/*.md``) must name a file. Unlike the suffix-matched citation check
  this one is anchored: the path is written from the repo root, so it either
  resolves or it is wrong.
* ``line-ref`` -- ``file.py:NNN`` anywhere in prose. The beyond-EOF check catches
  the citation that has already rotted; this one catches the citation that is
  ABOUT to, because every line number rots on the next refactor. Cite a symbol.
* ``fenced-path`` -- the same path question inside fenced blocks, for the two
  token shapes a reader COPIES rather than reads: a ``docs/task-specs/...md``
  path and the argument of ``kirocrew run``.
* ``table-row-merge`` -- two index rows glued onto one physical line. Both links
  are present, so every link-graph check stays green while the table renders one
  row short and one file loses its entry entirely.
* ``code-coupled-completeness`` -- the inverse of ``CODE_COUPLED_DOCS``: a
  packaged doc named in a string literal under ``website/src`` and absent from
  that table is an unrecorded coupling, so the pair can be broken silently.
* ``dead-identifier`` -- a backticked identifier that appears nowhere in the code
  trees. Report-only unless ``--strict-identifiers``, because the class is
  genuinely noisy: a doc may name a wire field, a vendor symbol or a deliberately
  wrong example.

Every fact-check finding is a ``(check-id, path, token)`` triple, and the triples
already present when the family shipped live in ``.github/docs-lint-baseline.txt``.
A baselined triple passes; a new one fails. Two operations touch that file and they
are deliberately different verbs: ``--update-baseline`` intersects and can therefore
only DELETE lines, and ``--accept-new`` is the one path that adds, which prints every
triple it records so the exemption lands in a diff a reviewer reads. A MISSING
baseline is an error, not an empty set -- read as empty, one ``rm`` plus one refresh
would accept every current violation forever. Same rule, and the same reason, as
``scripts/check_black_formatting.py``.

A triple that no longer fires is reported but does not fail. That is a concession to
this repository's doc trees being consolidated by several changes at once, so an
entry graduates in a file the current change never touched -- not a claim that a
triple is fragile, since ``FactFinding.key`` omits the line number so a reflow keeps
the same identity.
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
#
# ``src/kiro_crew/apps/builtins/`` is the third kind: thousands of lines of
# spec-grade markdown that ship INSIDE a builtin app -- its README, its agent
# briefs and prompts, and its SKILL.md files, which an agent loads verbatim at
# runtime. A stale path or a dead link there misroutes the agent rather than a
# human reader, so it is link- and fact-checked, and it is listed in
# :data:`UNCURATED_PREFIXES` because an app's markdown is curated by its own
# manifest rather than by a documentation index.
DOC_ROOTS: tuple[str, ...] = (
    "docs",
    "src/kiro_crew/docs",
    "website/docs",
    "src/kiro_crew/apps/builtins",
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
    "CLAUDE.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
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
    # A builtin app's markdown is app PAYLOAD: `app.json` names the skills and
    # agent prompts it ships, and the app's own README is the entry a reader
    # opens. Requiring an index in `skills/<name>/` or `agents/context/` would
    # add a file the app never loads, so reachability and the index requirement
    # are off here while every link and every cited path is still checked.
    "src/kiro_crew/apps/builtins/",
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
    "src/kiro_crew/docs/feishu-integration.md": ("website/src/pages/settings/FeishuPanel.tsx",),
    "src/kiro_crew/docs/imessage-integration.md": ("website/src/pages/settings/IMessagePanel.tsx",),
    "src/kiro_crew/docs/slack-integration.md": ("website/src/pages/settings/SlackPanel.tsx",),
    "src/kiro_crew/docs/teams-integration.md": ("website/src/pages/settings/TeamsPanel.tsx",),
    "src/kiro_crew/docs/telegram-integration.md": ("website/src/pages/settings/TelegramPanel.tsx",),
    "src/kiro_crew/docs/webex-integration.md": ("website/src/pages/settings/WebexPanel.tsx",),
    "src/kiro_crew/docs/wecom-integration.md": ("website/src/pages/settings/WeComPanel.tsx",),
    "src/kiro_crew/docs/weixin-integration.md": ("website/src/pages/settings/WeixinPanel.tsx",),
    "src/kiro_crew/docs/whatsapp-integration.md": ("website/src/pages/settings/WhatsAppPanel.tsx",),
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
    # Its unit tests plant the same missing paths as fixture data, for the same
    # reason: a check cannot be proven to fire without a defect to fire on.
    "test/test_docs_lint_fact_checks.py",
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

# How many items one report section prints before it says "and N more". A gate
# whose output is longer than the screen is a gate nobody reads to the end.
_MAX_REPORTED_FINDINGS = 40
# Lower for the two report-only sections: they are a nudge, not the verdict.
_MAX_REPORTED_ADVISORIES = 40
_MAX_REPORTED_STALE = 20


# ── Fact checks: identifiers, shapes and scope ─────────────────────────────────

# The baseline of fact-check triples that were already present when the family
# shipped. Relative to the repo root so a worktree carries its own copy.
DEFAULT_BASELINE = ".github/docs-lint-baseline.txt"

CHECK_PATH_EXISTS = "path-exists"
CHECK_LINE_REF = "line-ref"
CHECK_FENCED_PATH = "fenced-path"
CHECK_TABLE_ROW_MERGE = "table-row-merge"
CHECK_COUPLING_COMPLETENESS = "code-coupled-completeness"
CHECK_DEAD_IDENTIFIER = "dead-identifier"

# Doc genres that name files and symbols BECAUSE they do not exist yet. Same
# judgement as ``_PATH_CITE_DOC_PREFIXES``, applied to the fact checks whose
# subject is existence: a proposal saying "this adds
# ``src/kiro_crew/run_coordinator/sqlite.py``" is correct writing, and 13 of the 20
# path findings measured on the shipping tree were exactly that.
_FORWARD_LOOKING_DOC_PREFIXES: tuple[str, ...] = (
    "docs/request-for-change/",
    "docs/task-specs/",
)

# A source path written from the REPO ROOT, so it resolves or it is wrong. Both
# boundaries are load-bearing. The lookBEHIND keeps the ``docs/`` and ``src/`` arms
# from matching the tail of a longer path: without it
# ``src/kiro_crew/docs/tips.md`` would also be read as a repo-root
# ``docs/tips.md``. The lookAHEAD keeps the extension from being a PREFIX of the
# real one: without it ``scripts/vendor_manifest.sha256`` reads as
# ``scripts/vendor_manifest.sh``, ``types.pyi`` as ``types.py``, ``page.mdx`` as
# ``page.md`` and ``a.tsx.snap`` as ``a.tsx`` -- four correct citations reported as
# rot. ``:`` stays admissible so ``src/x.py:12`` is still seen by the line check.
_REPO_PATH_CITE_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:"
    r"src/[A-Za-z0-9_][A-Za-z0-9_./-]*\.py"
    r"|scripts/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh)"
    r"|website/src/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:tsx|ts)"
    r"|\.github/workflows/[A-Za-z0-9_][A-Za-z0-9_.-]*\.ya?ml"
    r"|docs/[A-Za-z0-9_][A-Za-z0-9_./-]*\.md"
    r")(?![A-Za-z0-9_.-])"
)
# A path followed by ``::`` is a COORDINATE -- ``src/board.py::is_repetition`` is the
# address format the auto-improvement spine hands an EXTERNAL target repository, so
# the path names a module in that repo. The syntax alone cannot say which repo is
# meant: this one uses it for its own code too
# (``src/kiro_crew/mcp_tools/workflows.py::workflow_run``), and skipping on syntax
# would let a rename leave every such local citation stale with the gate green.
# What separates them is the DOCUMENT, the same discriminator
# ``_PATH_CITE_DOC_PREFIXES`` already runs on: these docs describe a run against
# another repository, so a coordinate in them is not a claim about this tree.
_COORDINATE_SUFFIX = "::"
_EXTERNAL_TARGET_COORDINATE_DOCS: frozenset[str] = frozenset(
    {
        "docs/system-specs/modules/auto-improvement.md",
    }
)
# Inline code with its content captured; ``_INLINE_CODE_RE`` above blanks spans
# and deliberately captures nothing.
_INLINE_CODE_SPAN_RE = re.compile(r"`+([^`\n]*)`+")

# The two fenced-block tokens a reader COPIES instead of reading: an archived task
# spec's path, and whatever ``kirocrew run`` is handed.
_FENCED_TASK_SPEC_RE = re.compile(r"(?<![A-Za-z0-9_./-])docs/task-specs/[A-Za-z0-9_./-]*\.md")
_KIROCREW_RUN_RE = re.compile(r"kirocrew\s+run\s+(?:-[^\s]*\s+)*([A-Za-z0-9_][A-Za-z0-9_./-]*)")

# A markdown table's delimiter row, which is what fixes the table's width. Only
# the three characters a delimiter row is made of: admitting ``.`` would let a
# ``| ... | ... |`` continuation row silently redefine the width the glued-row
# check compares against.
_TABLE_DELIMITER_RE = re.compile(r"^\s*\|[\s|:-]+\|?\s*$")
# A cell whose content opens with a link.
_TABLE_LINK_CELL_RE = re.compile(r"\|\s*\[")

# ``CODE_COUPLED_DOCS`` records couplings by hand; this is the scan that finds the
# ones nobody recorded. A packaged doc's filename inside a STRING LITERAL is a
# consumer (a setup-guide URL, a fetch path); the same name in a comment is a
# citation, which ``check_code_citations`` already covers.
_PACKAGED_DOCS_DIR = "src/kiro_crew/docs"
_COUPLING_SCAN_ROOT = "website/src"
_COUPLING_SUFFIXES: frozenset[str] = frozenset({".ts", ".tsx"})
_STRING_LITERAL_RE = re.compile(r"""(['"`])((?:(?!\1).)*)\1""")
_TS_COMMENT_LINE_RE = re.compile(r"^\s*(?://|/\*|\*)")
_MD_NAME_RE = re.compile(r"(?<![A-Za-z0-9._-])([A-Za-z0-9][A-Za-z0-9._-]*\.md)")
# A filename every directory has, so a match cannot be attributed to the packaged
# copy: the hits are a user's project README in the file explorer, not this doc.
_COUPLING_AMBIGUOUS_NAMES: frozenset[str] = frozenset({"README.md"})

# Backticked identifiers, and the trees that decide whether one is alive. Every
# first-party tree is searched, so "dead" means dead repo-wide: restricting the
# corpus to src/, website/src and scripts/ marks `test_all_exports_exact` dead
# (it lives in test/) and `KiroCrewClient` dead (it lives in packages/). The tail
# of the list mirrors `_SOURCE_CITE_TREES`, because a symbol defined only in a
# workflow or in `website/scripts/` is alive and would otherwise read as dead.
_IDENT_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_IDENT_CAMEL_RE = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Shorter than this and the token is a word, not a symbol.
_IDENT_MIN_LEN = 8
_IDENT_CODE_TREES: tuple[str, ...] = (
    "src",
    "website/src",
    "website/scripts",
    "scripts",
    "test",
    "packages",
    "packaging",
    "site",
    ".github",
)
# Markdown and plain text are deliberately ABSENT. Documentation is what this
# check is auditing, so counting a doc as a definition lets one stale page keep a
# renamed symbol "alive" for every other page -- exactly the rot being hunted.
_IDENT_CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".sh",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".css",
        ".scss",
        ".html",
        ".sql",
    }
)
# Names a doc writes to mean "your thing here"; absence is the intent.
_IDENT_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "My",
    "Foo",
    "Bar",
    "Baz",
    "Your",
    "Example",
    "Sample",
    "Some",
    "Acme",
    "Todo",
    "Xyz",
)
# Generic English and product names that happen to match an identifier shape.
_IDENT_STOPWORDS: frozenset[str] = frozenset(
    {
        "anthropic",
        "boolean",
        "changelog",
        "claude",
        "darwin",
        "default",
        "defaults",
        "docker",
        "example",
        "examples",
        "github",
        "gitlab",
        "https",
        "integer",
        "javascript",
        "kirocrew",
        "license",
        "linux",
        "localhost",
        "macos",
        "markdown",
        "none",
        "null",
        "number",
        "openai",
        "optional",
        "postgres",
        "python3",
        "readme",
        "required",
        "sqlite",
        "string",
        "typescript",
        "undefined",
        "windows",
    }
)


@dataclass(frozen=True, order=True)
class FactFinding:
    """One fact-check finding, in the shape the baseline records.

    ``check``/``path``/``token`` are the identity -- what the baseline stores and
    matches on -- and ``detail`` is the human sentence, which may be reworded
    without invalidating a recorded triple. The line number stays out of the
    identity deliberately: a citation that merely moved down the page is the same
    unfixed finding, and putting the line in would make every reflow look new.
    """

    check: str
    path: str
    token: str
    detail: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.check, self.path, self.token)

    def render(self) -> str:
        return f"{self.path} -> {self.token}" + (f"  ({self.detail})" if self.detail else "")


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
    # Fact checks, behind the baseline. ``facts`` fails the run; ``advisories``
    # is the report-only half (dead identifiers, unless --strict-identifiers).
    facts: list[FactFinding] = field(default_factory=list)
    advisories: list[FactFinding] = field(default_factory=list)

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
            + len(self.facts)
        )

    def facts_for(self, check: str) -> list[FactFinding]:
        return [f for f in self.facts if f.check == check]


# ── Helpers ────────────────────────────────────────────────────────────────────


def _rel(path: Path, root: Path) -> str:
    """Repo-relative POSIX path, so findings read the same on every OS."""
    return path.relative_to(root).as_posix()


def _prune(root: Path, dirpath: str, dirnames: list[str]) -> None:
    """Drop vendored and build-artifact directories from an ``os.walk`` in place.

    Names are matched anywhere; artifact trees are matched by repo-relative path so
    a real documentation directory that shares a name with one (``docs/build/``) is
    still walked.

    A directory LINK is dropped too, for the reason ``_is_regular_file`` drops a file
    link: it is a path into a tree the fork PR does not own. ``os.walk`` already
    stays out of a directory symlink, but a Windows junction is a reparse point it
    descends without asking, so the junction test has to be explicit.
    """
    keep = []
    for d in sorted(dirnames):
        if d in SKIP_DIR_NAMES:
            continue
        full = Path(dirpath) / d
        if _rel(full, root) in SKIP_DIR_PATHS:
            continue
        if _is_dir_link(full):
            continue
        keep.append(d)
    dirnames[:] = keep


def _is_dir_link(path: Path) -> bool:
    """A directory symlink or a Windows junction, answering ``False`` on error."""
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))
    except OSError:
        return False


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


def _is_forward_looking(rel: str) -> bool:
    """True for a doc whose genre names things that do not exist yet."""
    return rel.startswith(_FORWARD_LOOKING_DOC_PREFIXES)


def _iter_code_spans(text: str):
    """Yield ``(line_number, inline-code content)`` for prose, fenced blocks aside.

    Backticks are what turn a path or a name into a citation: a bare word in a
    sentence is usually prose about a directory, and a fenced block is a sample.
    """
    for lineno, line in enumerate(_strip_fences(text).splitlines(), start=1):
        for match in _INLINE_CODE_SPAN_RE.finditer(line):
            yield lineno, match.group(1)


def _iter_fenced_lines(text: str):
    """Yield ``(line_number, line)`` for lines INSIDE fenced blocks.

    The complement of ``_strip_fences``, so a token is examined by exactly one of
    the two path checks and never reported twice.
    """
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield lineno, line


def _identifier_index(root: Path) -> frozenset[str]:
    """Every identifier-shaped token in the first-party code trees, built ONCE.

    Cached on the function because the dead-identifier check asks "does this name
    appear anywhere" a few hundred times, and re-reading ~7,000 files per question
    would make this gate the slowest thing in CI. Filenames count as tokens: a doc
    naming a module by its file name is citing something real.
    """
    cached = getattr(_identifier_index, "_cache", None)
    if cached is not None and cached[0] == root:
        return cached[1]
    tokens: set[str] = set()
    for tree in _IDENT_CODE_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            _prune(root, dirpath, dirnames)
            for name in sorted(filenames):
                tokens.update(_IDENT_TOKEN_RE.findall(name))
                if Path(name).suffix not in _IDENT_CODE_SUFFIXES:
                    continue
                path = Path(dirpath) / name
                if not _is_regular_file(path):
                    continue
                try:
                    tokens.update(_IDENT_TOKEN_RE.findall(_read(path)))
                except OSError:
                    continue
    frozen = frozenset(tokens)
    _identifier_index._cache = (root, frozen)  # type: ignore[attr-defined]
    return frozen


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


# ── Fact checks ────────────────────────────────────────────────────────────────


def check_cited_paths(root: Path, docs: list[Path], findings: Findings) -> None:
    """A backticked repo-anchored path must name a file (``path-exists``).

    ``check_source_citations`` matches a citation as a path SUFFIX, because a doc
    cites relative to whatever root its reader stands in. This one takes the
    opposite case: a path written from the repo root is unambiguous, so it
    resolves or the doc is wrong, and no genre exemption can rescue it.

    The suffix index is still consulted as a fallback. A skill's own
    ``scripts/reaper.sh`` reads as repo-anchored and is not: it exists, one root
    down. Honouring that dropped 33 of 53 raw findings on the shipping tree, all
    of them correct writing, and the ones left are rot.
    """
    for doc in docs:
        rel_doc = _rel(doc, root)
        if _is_forward_looking(rel_doc):
            continue
        try:
            text = _read(doc)
        except OSError:
            continue
        external_coordinates = rel_doc in _EXTERNAL_TARGET_COORDINATE_DOCS
        for _lineno, code in _iter_code_spans(text):
            for match in _REPO_PATH_CITE_RE.finditer(code):
                ref = match.group(0)
                if (root / ref).exists() or _resolve_source_cite(root, ref):
                    continue
                if (
                    external_coordinates
                    and code[match.end() : match.end() + 2] == _COORDINATE_SUFFIX
                ):
                    # A coordinate in a doc about a run against ANOTHER repository
                    # addresses a module there. Elsewhere the same syntax names local
                    # code, so the path stays checked and a rename still reddens.
                    continue
                findings.facts.append(FactFinding(CHECK_PATH_EXISTS, rel_doc, ref))


def check_fenced_paths(root: Path, docs: list[Path], findings: Findings) -> None:
    """Two fenced-block tokens a reader copies must resolve (``fenced-path``).

    A fenced block is a sample and is exempt everywhere else, which is right for a
    listing a reader only reads. It is wrong for the two shapes a reader PASTES: a
    ``docs/task-specs/...`` path and the argument of ``kirocrew run``. Both go
    straight into a command, so a dead one fails in the reader's terminal.

    Three exemptions, each because the path names a file that is not this
    repository's to have. A bare filename is the reader's own
    (``kirocrew run TASK.md``), so only a path WITH a directory component is a
    checkable claim. A forward-looking genre names its own planned output, the same
    judgement ``check_cited_paths`` runs on. And the PACKAGED user docs teach a
    reader to write their OWN task spec, so ``docs/task-specs/my-task/spec.md``
    there is a template for their project rather than a file in this checkout --
    "correct the path" is not even an available fix for it.
    """
    for doc in docs:
        rel_doc = _rel(doc, root)
        if _is_forward_looking(rel_doc) or rel_doc.startswith(f"{_PACKAGED_DOCS_DIR}/"):
            continue
        try:
            text = _read(doc)
        except OSError:
            continue
        for _lineno, line in _iter_fenced_lines(text):
            tokens = [m.group(0) for m in _FENCED_TASK_SPEC_RE.finditer(line)]
            tokens += [m.group(1) for m in _KIROCREW_RUN_RE.finditer(line)]
            # Both patterns match a `kirocrew run docs/task-specs/...` line, and one
            # pasted command is one finding.
            for token in dict.fromkeys(tokens):
                if "/" not in token or (root / token).exists():
                    continue
                findings.facts.append(FactFinding(CHECK_FENCED_PATH, rel_doc, token))


def check_line_refs(root: Path, docs: list[Path], findings: Findings) -> None:
    """A ``file.py:NNN`` citation in prose is a finding (``line-ref``).

    ``check_source_citations`` reports the citation that has ALREADY rotted -- the
    line is past the end of the file. This one reports the shape itself, because
    the other half rots on the next refactor and nothing goes red when it does: the
    number still lands inside the file, just on a different statement. A symbol
    name survives that move, which is why the fix is to cite one.
    """
    for doc in docs:
        rel_doc = _rel(doc, root)
        try:
            text = _read(doc)
        except OSError:
            continue
        for _lineno, line in enumerate(_strip_fences(text).splitlines(), start=1):
            for match in _SOURCE_CITE_RE.finditer(line):
                if not match.group(2):
                    continue
                token = f"{match.group(1)}:{match.group(2)}"
                findings.facts.append(FactFinding(CHECK_LINE_REF, rel_doc, token))


def check_table_rows(root: Path, docs: list[Path], findings: Findings) -> None:
    """Two index rows glued onto one line (``table-row-merge``).

    Both links survive the accident, so every link-graph check here stays green
    while the table renders one row short and one file loses its entry entirely --
    which is how a 20-file directory shipped a 19-row index.

    Two signals together, because either alone is noisy: a second cell opening with
    a link, AND more cells than the delimiter row declares. A row legitimately
    carrying two link cells (a stable and a nightly download) has the first signal
    and not the second; measured on the shipping tree the pair reports one row, the
    real one, and nothing else.
    """
    for doc in docs:
        rel_doc = _rel(doc, root)
        try:
            text = _read(doc)
        except OSError:
            continue
        width: int | None = None
        for lineno, line in enumerate(_strip_fences(text).splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("|"):
                width = None
                continue
            # Escaped pipes and pipes inside code spans are content, not borders.
            countable = _INLINE_CODE_RE.sub(
                lambda m: " " * len(m.group(0)), stripped.replace(r"\|", "  ")
            )
            cells = len(countable.strip().strip("|").split("|"))
            if _TABLE_DELIMITER_RE.match(stripped):
                width = cells
                continue
            if width is None or cells <= width:
                continue
            if len(_TABLE_LINK_CELL_RE.findall(countable)) < 2:
                continue
            # The SECOND link is the row that got glued on, so its target names the
            # entry that lost its own row -- a stable identity for the baseline,
            # where a cell count alone would collapse two distinct accidents.
            targets = [m.group(1).strip() for m in _LINK_RE.finditer(countable)]
            token = targets[1] if len(targets) > 1 else f"row:{cells}-cells"
            findings.facts.append(
                FactFinding(
                    CHECK_TABLE_ROW_MERGE,
                    rel_doc,
                    token,
                    f"line {lineno} has {cells} cells, the table declares {width}",
                )
            )


def check_coupling_completeness(root: Path, findings: Findings) -> None:
    """A packaged doc a consumer names must be recorded (``code-coupled-completeness``).

    ``CODE_COUPLED_DOCS`` is hand-maintained, so it is only as good as whoever
    remembered to add a row. This is the scan that finds the rows nobody added: a
    packaged doc's filename inside a STRING LITERAL under ``website/src`` is a live
    consumer -- a setup-guide URL, a fetch path -- and an unrecorded one can be
    renamed out from under it with every check here still green.

    A comment is not a consumer (``check_code_citations`` owns that direction), and
    a test is not either: its ``.md`` strings are fixture data.
    """
    packaged_dir = root / _PACKAGED_DOCS_DIR
    if not packaged_dir.is_dir():
        return
    packaged = {p.name for p in packaged_dir.glob("*.md") if _is_regular_file(p)}
    recorded = {
        Path(doc).name for doc in CODE_COUPLED_DOCS if doc.startswith(f"{_PACKAGED_DOCS_DIR}/")
    }
    base = root / _COUPLING_SCAN_ROOT
    if not base.is_dir():
        return
    consumers: dict[str, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        _prune(root, dirpath, dirnames)
        dirnames[:] = [d for d in dirnames if d != "test"]
        for name in sorted(filenames):
            if Path(name).suffix not in _COUPLING_SUFFIXES or ".test." in name:
                continue
            path = Path(dirpath) / name
            if not _is_regular_file(path):
                continue
            try:
                text = _read(path)
            except OSError:
                continue
            for line in text.splitlines():
                if _TS_COMMENT_LINE_RE.match(line):
                    continue
                for literal in _STRING_LITERAL_RE.finditer(line):
                    for match in _MD_NAME_RE.finditer(literal.group(2)):
                        doc_name = match.group(1)
                        if doc_name in _COUPLING_AMBIGUOUS_NAMES:
                            continue
                        if doc_name not in packaged or doc_name in recorded:
                            continue
                        consumers.setdefault(doc_name, set()).add(_rel(path, root))
    for doc_name, users in sorted(consumers.items()):
        findings.facts.append(
            FactFinding(
                CHECK_COUPLING_COMPLETENESS,
                f"{_PACKAGED_DOCS_DIR}/{doc_name}",
                doc_name,
                f"named in {', '.join(sorted(users))}",
            )
        )


def check_dead_identifiers(root: Path, docs: list[Path], findings: Findings) -> None:
    """A backticked identifier that appears in no code tree (``dead-identifier``).

    Report-only unless ``--strict-identifiers``, and that is a measured decision
    rather than timidity: 416 tokens fire on the shipping tree and the class mixes
    real rot (a renamed handler) with names the repo cannot adjudicate -- a wire
    field, a vendor symbol, an identifier a doc names precisely because it is
    WRONG (``MockClient`` in the flake8 N806 row). A report a maintainer reads is
    worth more here than a gate they learn to skip.

    Forward-looking genres are skipped for the same reason as the path check, and
    documentation placeholders (``MyPage``) are meant to be absent.
    """
    live = _identifier_index(root)
    for doc in docs:
        rel_doc = _rel(doc, root)
        if _is_forward_looking(rel_doc):
            continue
        try:
            text = _read(doc)
        except OSError:
            continue
        for _lineno, code in _iter_code_spans(text):
            for pattern in (_IDENT_SNAKE_RE, _IDENT_CAMEL_RE):
                for token in pattern.findall(code):
                    if len(token) < _IDENT_MIN_LEN:
                        continue
                    if token.lower() in _IDENT_STOPWORDS:
                        continue
                    if token.startswith(_IDENT_PLACEHOLDER_PREFIXES):
                        continue
                    if token in live:
                        continue
                    findings.advisories.append(FactFinding(CHECK_DEAD_IDENTIFIER, rel_doc, token))


# ── Baseline ───────────────────────────────────────────────────────────────────

_BASELINE_HEADER = """\
# docs-lint fact-check baseline: the (check-id, path, token) triples that were
# already present when each fact check shipped. A triple listed here passes; a
# triple that is NOT listed fails the gate.
#
# Do NOT hand-edit a line in to make a red gate green. A new triple means the doc
# needs the fix the check names -- cite a symbol instead of a line, correct the
# path, split the glued row, record the coupling.
#
# Refresh (after fixing something listed here). Prune-only: it intersects, so it
# cannot record a triple, and it refuses to run if this file is missing.
#   python3 scripts/docs_lint.py --update-baseline
#
# Adding is a separate, louder verb. It prints every triple it accepts, because
# each one is an exemption a reviewer has to agree with:
#   python3 scripts/docs_lint.py --accept-new
#
# Format: <check-id>\\t<path>\\t<token>
"""


def _read_baseline(path: Path) -> set[tuple[str, str, str]]:
    """The recorded triples. A MISSING file is an error, never an empty set.

    An absent baseline read as "nothing is exempt" sounds strict and is the
    opposite: the refresh would then have nothing to intersect against, so one
    ``rm`` plus one refresh would record every current violation as permanently
    accepted. Failing here is what makes the file's own "do NOT add a line"
    a rule rather than prose, and it is the posture
    ``scripts/check_black_formatting.py`` takes for the same reason.

    A SYMLINKED baseline is refused for the same reason ``_is_regular_file``
    refuses one anywhere else in this gate: the tree being linted is a tree a fork
    PR controls, so a committed symlink here aims both the read and the documented
    refresh at a file of the fork's choosing.
    """
    if not _is_regular_file(path):
        raise SystemExit(
            f"docs-lint: baseline {path} is missing or is not a regular file; restore "
            "it from git rather than regenerating it. A regenerated baseline would "
            "silently accept every violation added since it was recorded, and a "
            "symlink here would aim the refresh at whatever it points to."
        )
    recorded: set[tuple[str, str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3:
            continue
        recorded.add((parts[0], parts[1], parts[2]))
    return recorded


def _write_baseline(path: Path, triples: set[tuple[str, str, str]]) -> None:
    """Publish the baseline, refusing a symlinked destination.

    ``Path.write_text`` FOLLOWS a symlink, so a committed link at the baseline path
    turns the documented ``--update-baseline`` into an overwrite of whatever it
    points at, run by a maintainer on their own machine. Two guards, because the
    first alone is a check-then-write race: the ``lstat`` refusal gives the
    actionable message, and ``O_NOFOLLOW`` on the staged temp plus a rename is what
    the kernel enforces. Staging and renaming also means an interrupted refresh
    leaves the recorded backlog intact rather than truncated.

    ``kiro_crew.atomic_write`` is the repo's helper for this shape and is not used
    here on purpose: this gate is stdlib-only so it runs before the package is
    installed, which is how CI invokes it.
    """
    if path.is_symlink():
        raise SystemExit(
            f"docs-lint: refusing to write the baseline through the symlink {path}; "
            "the write would land at the link's target. Replace it with a real file."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join("\t".join(triple) + "\n" for triple in sorted(triples))
    data = (_BASELINE_HEADER + body).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        # A buffered writer loops until every byte is down and raises on a short
        # write, so the rename below never publishes a truncated baseline.
        with os.fdopen(fd, "wb") as staged:
            staged.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def apply_baseline(
    findings: Findings, recorded: set[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """Drop baselined triples from ``findings``; return the ones that no longer fire.

    Mutates in place so the caller keeps one ``Findings``. The return value is the
    prune list, and it is REPORTED rather than fatal for one reason only: this
    repository's doc trees are being consolidated by several changes at once, so a
    triple graduates in a file the current change never touched. It is not because
    a triple is fragile -- ``FactFinding.key`` excludes the line number precisely so
    a reflow keeps the same identity.

    That leaves one window worth naming: a triple fixed without pruning its entry
    can be reintroduced silently. ``--update-baseline`` closes it, and the file
    cannot grow without ``--accept-new``, so the window narrows every time anyone
    refreshes.
    """
    seen = {f.key for f in findings.facts} | {f.key for f in findings.advisories}
    findings.facts = [f for f in findings.facts if f.key not in recorded]
    findings.advisories = [f for f in findings.advisories if f.key not in recorded]
    return sorted(recorded - seen)


# ── Reporting ──────────────────────────────────────────────────────────────────


def _emit(title: str, items: list[str], hint: str) -> None:
    if not items:
        return
    print(f"\nFAIL: {title} ({len(items)})")
    for item in items[:_MAX_REPORTED_FINDINGS]:
        print(f"  - {item}")
    if len(items) > _MAX_REPORTED_FINDINGS:
        print(f"  ... and {len(items) - _MAX_REPORTED_FINDINGS} more")
    print(f"  -> {hint}")


def run(root: Path, *, strict_identifiers: bool = False) -> Findings:
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
    # The fact checks cover the entry points as well: README.md and AGENTS.md cite
    # more paths than most specs do, and a reader trusts them more.
    everything = docs + entry_points
    check_cited_paths(root, everything, findings)
    check_fenced_paths(root, everything, findings)
    check_line_refs(root, everything, findings)
    check_table_rows(root, everything, findings)
    check_coupling_completeness(root, findings)
    check_dead_identifiers(root, everything, findings)
    if strict_identifiers:
        findings.facts.extend(findings.advisories)
        findings.advisories = []
    return findings


def _emit_facts(findings: Findings, check: str, title: str, hint: str) -> None:
    _emit(title, [f.render() for f in findings.facts_for(check)], hint)


def _report(findings: Findings, doc_count: int, stale_baseline: list[tuple[str, str, str]]) -> int:
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
    _emit_facts(
        findings,
        CHECK_PATH_EXISTS,
        "backticked repo paths that name no file",
        "correct the path, or say plainly that the code was deleted",
    )
    _emit_facts(
        findings,
        CHECK_FENCED_PATH,
        "pasteable paths in fenced blocks that name no file",
        "correct the path: a reader copies this line into a command",
    )
    _emit_facts(
        findings,
        CHECK_LINE_REF,
        "line citations in prose",
        "cite a symbol name instead: a name survives the refactor that moves the line",
    )
    _emit_facts(
        findings,
        CHECK_TABLE_ROW_MERGE,
        "table rows glued onto one line",
        "split the row: the table renders one row short and a file loses its entry",
    )
    _emit_facts(
        findings,
        CHECK_COUPLING_COMPLETENESS,
        "packaged docs with an unrecorded consumer",
        f"add the doc and its consumer to CODE_COUPLED_DOCS in {Path(__file__).name}",
    )
    _emit_facts(
        findings,
        CHECK_DEAD_IDENTIFIER,
        "backticked identifiers that appear in no code tree",
        "rename to the live symbol, or drop the claim",
    )
    if findings.advisories:
        print(f"\nreport-only: {len(findings.advisories)} dead identifier(s) outside the baseline")
        for item in findings.advisories[:_MAX_REPORTED_ADVISORIES]:
            print(f"  - {item.render()}")
        if len(findings.advisories) > _MAX_REPORTED_ADVISORIES:
            print(f"  ... and {len(findings.advisories) - _MAX_REPORTED_ADVISORIES} more")
        print("  -> rename to the live symbol, or run with --strict-identifiers to fail on these")
    if stale_baseline:
        print(f"\nreport-only: {len(stale_baseline)} baseline entr(y/ies) no longer fire")
        for check, path, token in stale_baseline[:_MAX_REPORTED_STALE]:
            print(f"  - {check}\t{path}\t{token}")
        if len(stale_baseline) > _MAX_REPORTED_STALE:
            print(f"  ... and {len(stale_baseline) - _MAX_REPORTED_STALE} more")
        print("  -> prune them: python3 scripts/docs_lint.py --update-baseline")
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
            if expected is None:
                # The planter could not create its defect on this host (an
                # unprivileged Windows shell cannot make a symlink). That is a
                # gap in what THIS run proved, said out loud -- not a check
                # that failed to fire, which is what a FAIL here would claim.
                print(f"  skip {label}: host cannot plant this defect")
                return
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

    def plant_citation_of_a_symlinked_file(root: Path) -> str | None:
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
            # No link, no defect: returning the check name here would make
            # the probe report a check that "did not fire" on a tree that
            # never contained what it looks for.
            return None
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

    def fact_probe(label: str, check: str, build) -> None:
        """Assert a fact check fires, and that its triple names the right token."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            expected_token = build(root)
            got = run(root).facts_for(check) + [f for f in run(root).advisories if f.check == check]
            tokens = {f.token for f in got}
            if expected_token in tokens:
                print(f"  ok  {label} detected")
            else:
                print(f"  FAIL {label} NOT detected (got {sorted(tokens)})")
                failures += 1

    def fact_immunity_probe(label: str, check: str, build) -> None:
        """Assert a legitimate shape produces NO triple for ``check``."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
            build(root)
            findings = run(root)
            hits = findings.facts_for(check) + [f for f in findings.advisories if f.check == check]
            if hits:
                print(f"  FAIL {label} was flagged ({sorted(f.token for f in hits)})")
                failures += 1
            else:
                print(f"  ok  {label} ignored")

    def plant_missing_repo_path(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nThe entry point is `src/kiro_crew/ghost.py`.\n", encoding="utf-8"
        )
        return "src/kiro_crew/ghost.py"

    def allow_repo_path_that_exists(root: Path) -> None:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nThe entry point is `src/kiro_crew/real.py`.\n", encoding="utf-8"
        )

    def allow_repo_path_resolvable_one_root_down(root: Path) -> None:
        # A skill's own `scripts/reaper.sh` reads as repo-anchored and is not; the
        # suffix index is what tells the two apart, and dropping this exemption
        # re-reports 33 correct citations (measured).
        skill = root / "src" / "kiro_crew" / "builtin_skills" / "demo" / "scripts"
        skill.mkdir(parents=True)
        (skill / "reaper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nRun `scripts/reaper.sh` from the skill.\n", encoding="utf-8"
        )

    def allow_repo_path_in_a_proposal(root: Path) -> None:
        # An RFC names files BECAUSE they do not exist; that is the genre's job.
        rfc = root / "docs" / "request-for-change"
        rfc.mkdir()
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n- [Ok](ok.md)\n- [Rfc](request-for-change/rfc-x.md)\n", encoding="utf-8"
        )
        (rfc / "rfc-x.md").write_text(
            "# Rfc\n\nThis adds `src/kiro_crew/planned.py`.\n", encoding="utf-8"
        )

    def plant_line_ref_inside_the_file(root: Path) -> str:
        # Inside the file, so the beyond-EOF check stays silent and only the shape
        # check fires -- which is the citation that is about to rot unnoticed.
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text("# Ok\n\nSee `small.py:2`.\n", encoding="utf-8")
        return "small.py:2"

    def allow_symbol_cite_without_a_line(root: Path) -> None:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "small.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nSee `resolve_usable_model` in `small.py`.\n", encoding="utf-8"
        )

    def plant_dead_fenced_task_spec(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n```bash\nkirocrew run docs/task-specs/2026/01/gone/spec.md\n```\n",
            encoding="utf-8",
        )
        return "docs/task-specs/2026/01/gone/spec.md"

    def allow_bare_filename_after_kirocrew_run(root: Path) -> None:
        # `TASK.md` is a file the READER creates; only a path with a directory
        # component is a claim about this tree.
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n```bash\nkirocrew run TASK.md\n```\n", encoding="utf-8"
        )

    def plant_glued_table_row(root: Path) -> str:
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n| Doc | What |\n|---|---|\n"
            "| [a.md](a.md) | First. || [b.md](b.md) | Second. |\n",
            encoding="utf-8",
        )
        return "b.md"

    def allow_two_link_cells_in_one_row(root: Path) -> None:
        # A stable and a nightly download in one row: two link cells, correct width.
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\n| Kind | Stable | Nightly |\n|---|---|---|\n"
            "| deb | [Stable](https://x/a.deb) | [Nightly](https://x/b.deb) |\n",
            encoding="utf-8",
        )

    def plant_unrecorded_coupling(root: Path) -> str:
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "ghost-integration.md").write_text("# Ghost\n", encoding="utf-8")
        panel = root / "website" / "src" / "pages" / "settings"
        panel.mkdir(parents=True)
        (panel / "GhostPanel.tsx").write_text(
            "const SETUP_GUIDE = 'https://example.invalid/"
            "src/kiro_crew/docs/ghost-integration.md'\n",
            encoding="utf-8",
        )
        return "ghost-integration.md"

    def allow_coupling_named_only_in_a_comment(root: Path) -> None:
        # A comment is a CITATION, which check_code_citations already owns.
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "iframe-hosts.md").write_text("# Hosts\n", encoding="utf-8")
        page = root / "website" / "src" / "pages"
        page.mkdir(parents=True)
        (page / "ChatPage.tsx").write_text(
            "// See `src/kiro_crew/docs/iframe-hosts.md` for the host list.\n"
            "export const x = 1\n",
            encoding="utf-8",
        )

    def allow_coupling_named_only_in_a_test(root: Path) -> None:
        packaged = root / "src" / "kiro_crew" / "docs"
        packaged.mkdir(parents=True)
        (packaged / "fixture-doc.md").write_text("# Fixture\n", encoding="utf-8")
        tests = root / "website" / "src" / "test"
        tests.mkdir(parents=True)
        (tests / "Panel.test.tsx").write_text("const f = 'fixture-doc.md'\n", encoding="utf-8")

    def plant_dead_identifier(root: Path) -> str:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("def live_handler():\n    return 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nRouted through `vanished_handler`.\n", encoding="utf-8"
        )
        return "vanished_handler"

    def allow_live_identifier(root: Path) -> None:
        pkg = root / "src" / "kiro_crew"
        pkg.mkdir(parents=True)
        (pkg / "real.py").write_text("def live_handler_name():\n    return 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nRouted through `live_handler_name`.\n", encoding="utf-8"
        )

    def allow_placeholder_identifier(root: Path) -> None:
        (root / "src" / "kiro_crew").mkdir(parents=True)
        (root / "src" / "kiro_crew" / "real.py").write_text("a = 1\n", encoding="utf-8")
        (root / "docs" / "ok.md").write_text(
            "# Ok\n\nName it `MyOwnWidget` in your app.\n", encoding="utf-8"
        )

    def baseline_probe() -> None:
        """A recorded triple passes; the same triple unrecorded fails."""
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
            (root / "docs" / "ok.md").write_text(
                "# Ok\n\nThe entry point is `src/kiro_crew/ghost.py`.\n", encoding="utf-8"
            )
            unfiltered = run(root)
            if not unfiltered.facts_for(CHECK_PATH_EXISTS):
                print("  FAIL baseline probe could not produce a finding")
                failures += 1
                return
            triple = unfiltered.facts_for(CHECK_PATH_EXISTS)[0].key
            filtered = run(root)
            stale = apply_baseline(filtered, {triple})
            if filtered.facts_for(CHECK_PATH_EXISTS) or stale:
                print("  FAIL a baselined triple still failed the run")
                failures += 1
                return
            other = run(root)
            stale = apply_baseline(other, {(CHECK_PATH_EXISTS, "docs/ok.md", "src/other.py")})
            if not other.facts_for(CHECK_PATH_EXISTS) or not stale:
                print("  FAIL an unrecorded triple was absorbed, or a stale entry went unreported")
                failures += 1
                return
            print("  ok  baseline admits recorded triples and reports stale ones")

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

    # Fact checks: each one fires on a planted defect, and each documented
    # exemption is pinned so widening the rule fails here instead of in review.
    fact_probe("missing repo-anchored path", CHECK_PATH_EXISTS, plant_missing_repo_path)
    fact_immunity_probe("repo path that exists", CHECK_PATH_EXISTS, allow_repo_path_that_exists)
    fact_immunity_probe(
        "repo path resolvable one root down",
        CHECK_PATH_EXISTS,
        allow_repo_path_resolvable_one_root_down,
    )
    fact_immunity_probe(
        "repo path inside a proposal", CHECK_PATH_EXISTS, allow_repo_path_in_a_proposal
    )
    fact_probe("line citation inside the file", CHECK_LINE_REF, plant_line_ref_inside_the_file)
    fact_immunity_probe(
        "symbol citation without a line", CHECK_LINE_REF, allow_symbol_cite_without_a_line
    )
    fact_probe("dead pasteable path in a fence", CHECK_FENCED_PATH, plant_dead_fenced_task_spec)
    fact_immunity_probe(
        "bare filename after `kirocrew run`",
        CHECK_FENCED_PATH,
        allow_bare_filename_after_kirocrew_run,
    )
    fact_probe("glued table row", CHECK_TABLE_ROW_MERGE, plant_glued_table_row)
    fact_immunity_probe(
        "two link cells in one row", CHECK_TABLE_ROW_MERGE, allow_two_link_cells_in_one_row
    )
    fact_probe(
        "unrecorded packaged-doc coupling",
        CHECK_COUPLING_COMPLETENESS,
        plant_unrecorded_coupling,
    )
    fact_immunity_probe(
        "packaged doc named only in a comment",
        CHECK_COUPLING_COMPLETENESS,
        allow_coupling_named_only_in_a_comment,
    )
    fact_immunity_probe(
        "packaged doc named only in a test",
        CHECK_COUPLING_COMPLETENESS,
        allow_coupling_named_only_in_a_test,
    )
    fact_probe("dead identifier", CHECK_DEAD_IDENTIFIER, plant_dead_identifier)
    fact_immunity_probe("live identifier", CHECK_DEAD_IDENTIFIER, allow_live_identifier)
    fact_immunity_probe(
        "documentation placeholder identifier",
        CHECK_DEAD_IDENTIFIER,
        allow_placeholder_identifier,
    )
    baseline_probe()

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
    parser.add_argument(
        "--baseline",
        default=None,
        help=f"fact-check baseline file (default: {DEFAULT_BASELINE} under the root)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="prune triples that no longer fire; it can never add one",
    )
    parser.add_argument(
        "--accept-new",
        action="store_true",
        help="ADD the triples firing now to the baseline, printing each one (needs a reason)",
    )
    parser.add_argument(
        "--strict-identifiers",
        action="store_true",
        help="fail on dead identifiers instead of reporting them",
    )
    args = parser.parse_args(argv)

    if args.test:
        return _self_test()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if not (root / "docs").is_dir():
        print(f"docs-lint: no docs/ directory under {root}", file=sys.stderr)
        return 2
    baseline_path = Path(args.baseline) if args.baseline else root / DEFAULT_BASELINE
    if args.update_baseline and args.accept_new:
        print(
            "docs-lint: --update-baseline and --accept-new are opposites; run one at a time",
            file=sys.stderr,
        )
        return 2

    findings = run(root, strict_identifiers=args.strict_identifiers)
    current = {f.key for f in findings.facts} | {f.key for f in findings.advisories}

    if args.update_baseline:
        # Prune-only, unconditionally: the intersection can only shrink the file,
        # and `_read_baseline` refuses an absent one, so there is no path here that
        # records a triple. Adding is `--accept-new`, which announces itself.
        recorded = _read_baseline(baseline_path)
        survivors = recorded & current
        _write_baseline(baseline_path, survivors)
        print(f"pruned {len(recorded) - len(survivors)} triple(s); {len(survivors)} remain")
        return 0

    if args.accept_new:
        # The one operation that grows the file, kept separate and LOUD. A triple
        # accepted here is a decision someone has to defend in review, so it is
        # printed rather than silently folded in, and the count is reported.
        recorded = _read_baseline(baseline_path)
        added = sorted(current - recorded)
        if not added:
            print(f"nothing new to accept; {len(recorded)} triple(s) remain")
            return 0
        for check, path, token in added:
            print(f"accepting {check}\t{path}\t{token}")
        _write_baseline(baseline_path, recorded | current)
        print(
            f"accepted {len(added)} NEW triple(s) into {baseline_path}; "
            "each one is an exemption a reviewer must agree with"
        )
        return 0

    stale = apply_baseline(findings, _read_baseline(baseline_path))
    doc_count = sum(len(_walk_markdown(root, r)) for r in DOC_ROOTS)
    return _report(findings, doc_count, stale)


if __name__ == "__main__":
    sys.exit(main())
